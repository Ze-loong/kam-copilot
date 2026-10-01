# kam_admin 模块设计文档

> 状态：开发实现（软件工程五步精简里程碑 · 第3步，`kam_admin` 是继 `kam_client` 之后第二个开工的组件）
> 输入：`01-需求规格说明书.md`（3.5节管理后台需求、4.2节权限模型字段）、`02-架构设计文档.md`（服务边界2.1/2.2节、5.2节路由表）、`05-kam_client设计文档.md`（kam_admin 唯一的数据访问入口）
> 定稿日期：
> 更新：——客户/订单/标签模块从占位改为本轮实现，回填联调中发现的两处设计留白，见第六节。

> 功能权限与数据范围权限分别校验，见第二节。

---

## 一、kam_admin 是什么、这一轮做到什么程度

回顾02号文档2.1节：`kam_admin` 是独立部署服务，Flask（服务端渲染），管理后台职责——员工/客户/订单/标签管理、数据看板、AI采纳率统计。不直连数据库，通过 `kam_client` 转手读写数据。

01号需求文档3.5节给的功能模块清单有6个：员工管理、客户管理、订单管理、标签管理、数据看板、AI采纳率统计。**这一轮骨架搭建的范围**：先把权限模型（本文档的核心难点）和整体路由骨架设计定案并真实验证跑通一个完整模块（员工管理），其余5个模块先占位（路由注册但函数体简单返回，或直接留 TODO），下一轮再逐个填。这个顺序的判断依据：权限模型是贯穿所有模块的公共机制，如果先写5个模块再回头加权限，等于要把已经写好的每个路由函数都改一遍；先用一个模块把机制验证通过，后面模块套用同一个装饰器和过滤模式，改动量小很多。

---

## 二、关键决策

### 决策1：登录鉴权 —— 真实查 `employee` 表，不用环境变量写死账号

**单账号方案的限制**：`ADMIN_USER_ID`/`ADMIN_PASSWORD` 写在 `.env` 里，登录时直接字符串比较，成功后 `session['logged_in'] = True`。这种方式登录后系统不知道"这个人是谁、是什么角色、属于哪个区域"，因为它假设只有一个管理员账号。

**为什么不采纳**：01号文档3.5节的两维度权限模型要求"普通顾问只看自己客户、区域主管看区域内、超管看全部"，这意味着**登录后必须知道当前用户的 `user_id`/`role`/`region`**，才能在后续每个请求里做过滤判断。这种单账号模式满足不了多角色场景。

**决策**：登录时用户输入 `user_id` + 密码，后端调 `kam_client.get_employee(user_id)` 查真实的员工记录，校验密码通过后把 `user_id`/`role`/`region` 一起存进 `session`。后续所有路由函数都从 `session` 读这三个字段做权限判断，不用每次都重新查一次 `employee` 表（换取请求效率，session 是登录时一次性查好缓存住的）。

**密码怎么存**：`employee` 命名空间当前的字段设计（02号文档4.2节）只有 `user_id`/`name`/`role`/`region`，没有密码字段。这一轮先给 `employee` 的 value 结构追加一个 `password` 字段（明文，仅限当前 mock/开发阶段，生产化前必须换成哈希存储，记入待确认事项）。这是对 02 号文档 4.2 节数据结构的一处小扩展，记入下方"对02号文档的补充"。

### 决策2：功能模块权限 —— 装饰器 + 显式声明所需角色

**问题**：01号文档3.5节要求"普通顾问仅可进客户管理、订单管理；区域主管/超管全部模块可进"。怎么在代码里表达"这个路由允许哪些角色访问"？

**决策**：写一个 `require_role(*allowed_roles)` 装饰器，用在每个路由函数上，显式声明这个页面/接口允许哪些角色访问，例如：

```
@app.route('/employees')
@login_required
@require_role('regional_manager', 'super_admin')   # 普通顾问不能进员工管理
def employees(): ...

@app.route('/customers')
@login_required
@require_role('consultant', 'regional_manager', 'super_admin')  # 三种角色都能进
def customers(): ...
```

**为什么不用"角色白名单配置表"这种更"数据驱动"的方式**（比如一个 dict 统一定义 `{路由: [允许角色列表]}`）：当前只有3个角色、6个功能模块，路由和权限的对应关系不复杂，装饰器直接写在路由函数上下文最清晰、改一个模块的权限时改动范围最小（只改这一个函数上面那一行），配置表的价值要等到角色/模块数量显著增长、且权限关系需要运营人员动态配置时才体现，当前不做这个复杂度。

### 决策3：数据范围权限 —— 应用层过滤，不下沉到查询层

**两个备选方案的差异**（详细对比过程见本轮对话，这里记录结论和理由）：

| 方案 | 做法 | 改动范围 | 性能 |
|---|---|---|---|
| **应用层过滤**（采纳） | kam_admin 拿到 kam_client 返回的数据后，在 Flask 路由函数内部用 Python 按 `region`/`follow_user_id` 筛选 | 只改 kam_admin，kam_client/kam_agent 不用动 | 数据量大时效率较低（可能拉多次、筛多次） |
| 查询层过滤 | 给 `external_user` 表加冗余 `region` 字段，或改 `/store/search` 支持跨命名空间过滤 | 需要改 kam_agent 的数据结构或接口，改动范围大 | 一次查询拿到最终结果，效率更高 |

**决策依据**：LangGraph Store 是简单的 namespace/key/value 键值存储，不支持关系型 JOIN，"查询层过滤"本质上也得先查 `employee` 表拿到某区域的顾问列表、再逐个关联 `external_user`，并没有真正省掉应用层的关联逻辑，只是把这部分逻辑挪到了更底层、且需要改动数据结构或接口契约。当前项目数据量小（mock数据规模），多次简单查询的性能代价可忽略。判断依据与 `kam_client` 设计文档"N+1查询暂不优化"是同一条原则——不做过早优化，等真实感知到性能问题再回头设计。

**应用层过滤的具体实现方式**（以客户列表为例）：

```
def _get_visible_follow_user_ids(current_user) -> list[str] | None:
    """返回当前用户能看到哪些顾问名下的客户。返回 None 表示不限制（超管）。"""
    if current_user["role"] == "super_admin":
        return None  # 无限制
    if current_user["role"] == "regional_manager":
        all_employees = kam_client.list_employees()
        return [e["user_id"] for e in all_employees if e["region"] == current_user["region"]]
    # consultant：只能看自己
    return [current_user["user_id"]]
```

客户列表路由函数拿到这个 `follow_user_id` 列表后，对每个 id 调 `kam_client.list_external_users(follow_user_id)`，Python 里 `+=` 拼起来返回（`super_admin` 情况需要一个"查全部顾问"的路径，因为 `external_user` 命名空间必须带 `follow_user_id` 才能 search，没有"不限定顾问查全部客户"的原生方式——这一点在下方"对02号文档的补充"里记录）。

### 决策4：Session 存什么、怎么在路由函数间共享当前用户信息

Flask 的 `session` 存 `user_id`/`role`/`region`/`name` 四个字段（登录时一次性从 `employee` 表查出来存入，避免每个请求都重新查库）。写一个 `get_current_user()` 辅助函数统一从 `session` 读取并组装成 dict，所有路由函数通过它拿当前用户信息，不直接操作 `session[...]`——这样如果以后 `session` 存储结构调整（比如从 Flask 内置 session 换成更严格的 JWT），只需要改 `get_current_user()` 一处。

---

## 三、目录结构与文件职责

```
kam_admin/
├── .python-version
├── pyproject.toml
├── .env.example
├── kam_admin_app.py         # Flask 入口：页面路由 + API路由 + 登录鉴权 + 权限装饰器
├── auth.py                   # login_required / require_role 装饰器 + get_current_user()
├── permissions.py             # _get_visible_follow_user_ids 等数据范围过滤逻辑
├── static/                   # 复用静态资源和页面结构的 Bootstrap/jQuery/字体资源
└── templates/                # 复用静态资源和页面结构的页面结构，登录页/员工列表页先落地，其余页面下一轮补
```

**为什么把 `auth.py` 和 `permissions.py` 拆成两个文件**：`auth.py` 管"你是谁、你能不能进这个页面"（登录态校验、角色白名单校验），是访问控制层；`permissions.py` 管"你能看到哪些数据"（数据范围过滤的具体计算逻辑），是数据可见性层。这是决策2和决策3两类不同性质的权限判断（前者是"页面级"的二元允许/拒绝，后者是"数据级"的过滤计算），拆开便于以后单独测试和修改，跟 `kam_client` 里 `_http.py`/`kam_store_api.py` 按职责拆分是同一个原则。

`kam_admin_app.py` 本身不直接调用 `kam_client` 的底层方法做权限判断，而是调用 `permissions.py` 暴露的函数——路由函数只管"调哪个过滤函数、把结果传给模板/JSON"，不掺权限计算细节。

---

## 四、路由表（对齐02号文档5.2节，本轮实现范围已标注）

### 4.1 页面路由

| 路由 | 权限 | 本轮状态 |
|---|---|---|
| `/login` | 无需登录 | **本轮实现** |
| `/logout` | 需登录 | **本轮实现** |
| `/` | 需登录，重定向到首页 | **本轮实现**（重定向到 `/employees` 或 `/customers`，按角色区分首页，见下方说明） |
| `/employees` | `regional_manager`/`super_admin` | **本轮实现** |
| `/customers` | 全角色 | **本轮实现**（第二轮） |
| `/employee_customers/<user_id>` | `regional_manager`/`super_admin`（区域主管仅限本区域员工） | **已实现**（回归修复，按所选员工筛选） |
| `/customers/<external_id>` | 全角色（数据范围过滤） | **本轮实现**（第二轮） |
| `/orders/<union_id>` | 全角色 | **本轮实现**（第二轮） |
| `/tags` | `regional_manager`/`super_admin` | **本轮实现**（第二轮，中途曾漏接`render_template`导致页面仍为占位文本，已修复，见第六节） |
| `/dashboard` | `regional_manager`/`super_admin` | 占位 |
| `/ai_adoption_rate` | `regional_manager`/`super_admin` | 占位 |

**首页按角色区分的原因**：普通顾问没有员工管理权限，如果首页固定重定向到 `/employees` 会立刻撞上权限拒绝，体验不好。逻辑：`consultant` 重定向到 `/customers`，`regional_manager`/`super_admin` 重定向到 `/employees`。

### 4.2 API 路由

| 路由 | 方法 | 权限 | 本轮状态 |
|---|---|---|---|
| `/api/employees` | GET | `regional_manager`/`super_admin` | **本轮实现** |
| `/api/employees` | POST | `super_admin` only（新增员工是更敏感操作，比"查看"权限更收紧，见下方说明） | **本轮实现** |
| `/api/customers` | GET | 全角色（数据范围过滤生效） | **本轮实现**（第二轮） |
| `/api/employee_customers/<user_id>` | GET | `regional_manager`/`super_admin`（服务端校验区域范围，越权返回403） | **已实现**（回归修复） |
| `/api/customers/<external_id>` | GET | 全角色（数据范围过滤，越权返回403） | **本轮实现**（第二轮） |
| `/api/orders/<union_id>` | GET | 全角色 | **本轮实现**（第二轮） |
| `/api/tags` | GET/POST | `regional_manager`/`super_admin` | **本轮实现**（第二轮） |
| `/api/tags/suggestions/<external_id>` | GET | 全角色 | **本轮实现**（，`kam_agent` F04标签推荐子图落地后由占位改为真实实现，复用`get_customer_detail`聚合逻辑，不触发生成；详见`08-F04标签推荐设计文档.md`） |
| `/api/dashboard` | GET | `regional_manager`/`super_admin` | 占位 |
| `/api/ai_adoption_rate` | GET | `regional_manager`/`super_admin` | 占位 |

**"新增员工"权限比"查看员工列表"更收紧的判断**：01号文档3.5节写的是"区域主管/超管全部模块可进入"，字面上只区分了"能不能进员工管理模块"，没有细化"进去之后的增删操作"这一层。这里做了一个合理的收紧判断——区域主管如果能新增员工，理论上可以新增一个属于其他区域的员工账号，这超出了"区域主管只管理自己区域"的直觉边界，所以新增操作收紧到 `super_admin` only。这是本文档在需求文档字面之外做的一次合理推断，记入下方"对01号文档的补充说明"，供后续如果要做正式验收时对照。

---

## 五、权限校验失败时的响应设计

- 页面路由：未登录 → 重定向到 `/login`；已登录但角色不足 → 返回 403 页面（复用 `base.html` 布局渲染一个简单的"无权限访问"提示，不是裸 HTTP 403）。
- API 路由：未登录 → `{"success": false, "error": "未登录"}` + HTTP 401；已登录但角色不足 → `{"success": false, "error": "无权限"}` + HTTP 403；数据范围越权（比如普通顾问试图查询不属于自己的客户详情）→ 同样返回 403，不区分"无权限"和"数据不存在"（避免信息泄露——如果分开返回"数据不存在"和"无权限"，攻击者能通过响应差异探测出某个 `external_id` 是否真实存在）。

这条"越权和不存在返回同一种错误"的设计，是本文档主动补的一条安全细节，01/02号文档都没有提到，记入下方"对01号文档的补充说明"。

---

## 六、对已有文档的补充说明（需要回填的偏离点）

- **`employee` 命名空间新增 `password` 字段**（决策1）：02号文档4.2节 `employee` 表结构目前是 `user_id`/`name`/`role`/`region` 四个字段，需要补充 `password`（当前明文存储，仅限开发阶段，生产化前必须替换为哈希）。
- **"新增员工"权限收紧到 `super_admin` only**（4.2节说明）：01号文档3.5节字面只到"能否进入模块"层级，这是在此基础上做的合理细化。
- **越权与数据不存在统一返回403**（第五节）：01/02号文档都未提及这条安全细节，是本文档主动补充。
- **`super_admin` 查全部客户没有原生"不限定顾问"的查询方式**（决策3）：`external_user` 命名空间必须带 `follow_user_id` 才能 `search`，超管要查全公司客户，需要先查全部 `employee` 拿到所有 `user_id`，再逐个 `list_external_users` 拼起来——这不是 bug，是 Store 数据结构的既有设计（`follow_user_id` 作为命名空间层级本身就是员工隔离机制，02号文档4.3节），但意味着"超管查看全部客户"这个操作在当前设计下，请求量是"顾问总数"次，需要记录在案，如果未来顾问规模变大（比如300+顾问，对应01号文档背景里提到的真实规模），这里会成为明显的性能瓶颈，需要重新设计（比如给 kam_agent 加一个"跨命名空间批量查询"的专用接口）。当前 mock 数据规模下可接受。
- **客户详情/订单越权判断的具体实现方式（决策3遗留的设计留白，第二轮补齐）**：决策3只给出了"怎么拿到当前用户可见的顾问id列表"（`get_visible_follow_user_ids`），没有回答"给定一个具体的 `external_id`/`union_id`，怎么判断它是否在这个可见范围内"。实现时发现 `external_id`/`union_id` 本身不携带 `follow_user_id`，需要反查——`kam_admin_app.py` 新增 `_find_customer_and_owner(external_id)`：遍历当前用户可见的顾问列表，逐个 `list_external_users` 查找是否有匹配的 `external_id`，命中即视为"客户属于这个顾问"且当前用户有权限查看；订单场景同理，遍历可见顾问名下客户匹配 `union_id`。这个函数同时完成了"越权判断"和"找到 `follow_user_id`"两件事——`get_customer_detail(follow_user_id, external_id)` 这类聚合查询本来就需要 `follow_user_id` 参数，顺带解决了。**性能提示**：这是又一处 N+1 遍历（比"超管查全部客户"更重——客户详情/订单查询在可见顾问规模大时每次请求都要遍历），当前 mock 数据规模可接受，判断依据与"超管查全部客户"那条已知限制一致，未来顾问规模变大需一并重新设计。
- **`kam_client.search()` 补充 `_key` 字段（写代码时才发现的真实缺口，第二轮）**：03号 `kam_client` 设计文档和 `_http.py` 原实现里，`search()` 只把 Store 返回的 `item.value` 取出来，`item.key` 被丢弃。写客户/订单/标签模块时才发现 `wxxd_order`（订单）和 `tags_setting`（标签）两个命名空间的业务主键（`order_id`/`tag_id`）本来就是 Store 的 key，不在 value 内容里（见 kam_agent 侧 `store_client.py` 的 `upsert_wxxd_order`/`upsert_tag` 写法）——`employee`/`external_user` 因为 value 里自带 `user_id`/`external_id` 冗余字段，之前一直没暴露这个问题。修复：`_http.py` 的 `search()` 给每条返回的 dict 补一个 `"_key"` 字段存 `item.key`，`kam_store_api.py` 的 `list_orders`/`list_tags` 方法文档同步说明。这是 `kam_client` 包的改动，不是 `kam_admin` 本身，但是在 `kam_admin` 这一轮开工时发现并修的，记录在这里方便以后查。

---

## 六.5、数据看板 + AI采纳率 设计定案（第三轮）

**背景**：现有数据能计算客户、订单和画像状态，但尚不足以计算完整商机漏斗、维保续约率或顾问人效。回复与标签功能已有实现，未记录采纳结果；日程提醒尚未实现。

### 数据看板指标（4项，均为当前mock数据真实可算）

1. **客户/订单总数概览**：当前用户可见范围内的客户总数、订单总数——直接复用 `/api/customers`/`/api/orders` 已有的应用层过滤逻辑，不是新的数据访问方式。
2. **有订单客户占比**：`有订单客户数 / 客户总数`。这是一个刻意选择的"最粗约"指标——01号文档要的"转化漏斗"需要多阶段状态跟踪（比如"咨询→意向→成交"分层漏斗），当前数据结构里客户和订单是扁平关联，没有阶段状态字段，做不出真正的漏斗，所以退而求其次，只做"有没有转化成订单"这个二元占比，如实体现数据局限。
3. **画像字段确认进度**：遍历可见范围内客户的画像，按 `status`（`draft`/`need_verify`/`confirmed`/`discarded`/`need_regenerate`）分类计数，展示各状态字段数量——直接反映AI画像草稿被顾问处理的整体进度。
4. **按顾问分组的客户数**：可见范围内每个顾问名下客户数量的柱状/列表展示。**明确不是"人效分析"**——01号文档要的人效应该包含转化率、响应时长等多维指标，这里只是最基础的"客户分布"，命名和展示都要避免让人误以为是完整的人效分析。

### AI采纳率统计（只统计画像字段终态）

**范围判断**：只对画像字段终态计算采纳率。回复与标签尚未记录采纳结果，页面说明暂不统计；日程提醒尚未实现。

**计算口径**：遍历可见范围内客户的画像字段，统计 `status=confirmed` 和 `status=discarded` 的数量，采纳率 = `confirmed 数量 / (confirmed 数量 + discarded 数量)`。**不把 `draft`/`need_verify`/`need_regenerate` 计入分母**——这些状态代表"顾问还没做终态决定"，混进分母会让采纳率被这些未决状态稀释，语义上不准确；只统计顾问真正做出"接受"或"拒绝"决定的字段，才是"采纳率"这个词本身该表达的意思。可以额外展示"待处理字段数"（`draft`+`need_verify`+`need_regenerate`之和）作为辅助信息，但不参与采纳率计算。

**权限与数据来源**：与看板一样走应用层过滤（`get_visible_follow_user_ids`）；画像数据通过 `kam_client.get_customer_profiles_batch(customers)` 批量读取，减少跨服务请求。

### 路由表更新

| 路由 | 权限 | 本轮状态 |
|---|---|---|
| `/dashboard` | `regional_manager`/`super_admin` | **本轮实现**（第三轮） |
| `/api/dashboard` | `regional_manager`/`super_admin` | **本轮实现**（第三轮） |
| `/ai_adoption_rate` | `regional_manager`/`super_admin` | **本轮实现**（第三轮，仅画像维度） |
| `/api/ai_adoption_rate` | `regional_manager`/`super_admin` | **本轮实现**（第三轮，仅画像维度） |
| `/system_config` | `super_admin` | **本轮实现**（第四轮，二期F17全局关停开关，见02号架构文档7.5节） |
| `/api/system_config` | `super_admin` | **本轮实现**（第四轮，GET查状态/POST切换，真实验证跨服务生效） |

---

## 七、待确认 / 已知限制

- **密码明文存储**：决策1提到的临时方案，生产化前必须换成哈希（如 bcrypt），当前仅为跑通鉴权流程。
- **超管查全部客户的性能问题**：见第六节最后一条，当前规模可接受，顾问规模增长后需要重新设计。
- **本轮范围（第一轮）**：只做通权限机制 + 员工管理模块，客户/订单/标签/看板/AI采纳率5个模块留待下一轮实现，当前只占位路由。
- **客户/订单/标签模块状态（第二轮，已完成）**：三个模块均已实现并真实联调通过（顾问仅见本人客户、越权返回403、区域主管见区域内客户、标签新增可回读）。
- **数据看板+AI采纳率状态（第三轮，设计已定案）**：指标范围见第六.5节，数据看板做客户/订单概览+有订单客户占比+画像确认进度+按顾问分组客户数，AI采纳率只做画像维度（回复/标签/日程三个子图尚未实现，页面如实标注"暂未实现"，不编造数据）。
- **前端复用静态资源和页面结构资源**：`static/`（Bootstrap/jQuery/字体）和部分 `templates/`（`base.html`/`login.html`/`employees.html`）直接复用静态资源和页面结构的现成实现并按需要小改（比如登录表单要接真实鉴权逻辑），不重新设计UI——这不是需要独立判断的架构决策，是纯前端资源复用，判断依据是"这部分没有业务逻辑含量，复用能节省时间且效果已验证可用"。
- **全局关停开关状态（第四轮，已完成）**：`/system_config`+`/api/system_config`已实现并真实验证（含跨服务状态核对、regional_manager权限边界测试），见上方路由表。~~**架构文档7.4节的"资料库管理"入口（`/kb/upload`/`/kb/list`）仍未实现**，当前知识库内容通过`kam_agent`侧一次性脚本手动摄入，不是通过本组件的管理界面，是本组件相对二期架构设计唯一的剩余缺口。~~——**本条已过时，见下一条**。
- **资料库管理入口状态（当天再续，已完成；补充"查看"功能）**：上一条写的时候还没做，同一天稍晚就已实现——`/kb`页面（列表+上传合并一页，权限比照标签体系）+ `/api/kb/list`+`/api/kb/upload`两个接口，转发到kam_agent的真实接口是`POST /kb/documents`+`GET /kb/documents`（不是设计阶段写的`/kb/upload`/`/kb/list`这两个名字，02号架构文档7.4节已同步订正）。又加了"查看资料内容"功能（`/api/kb/detail`，点"查看"把内容回填进已有上传框、改完直接复用"同名替换"逻辑重传，不单独做编辑接口），过程中发现并修复一个真实bug——浏览器textarea提交的`\r\n`换行会让kam_agent那边的资料切分逻辑整体失效，详见`实战记录.md`"资料库查看内容功能"条目。

---

## 下一步

1. 按本文档搭建目录结构（`kam_admin_app.py`/`auth.py`/`permissions.py`），复用静态资源和页面结构前端资源。
2. 实现登录鉴权 + 员工管理模块，真实起服务联调验证（`kam-postgres` + `kam_agent` + `kam_admin` 三者一起跑）。
3. 验证通过后，下一轮补齐客户/订单/标签/看板/AI采纳率5个模块。
