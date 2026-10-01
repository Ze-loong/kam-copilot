# kam_sidebar 模块设计文档

> 状态：开发实现（软件工程五步精简里程碑 · 第3步，`kam_sidebar` 是四组件里最后一个开工的）
> 输入：`01-需求规格说明书.md`（3.2节侧边栏需求）、`02-架构设计文档.md`（服务边界2.1/2.3节、5.1节接口契约、5.3节路由表）、`04-kam_agent接口实现说明.md`（`/tasks/*` 三接口的真实行为）
> 定稿日期：

> 本文档的设计判断均从 01/02 号文档的既有约束推导。既有方案的 SSE 接收/画像展示交互结构值得对照理解，但**登录鉴权这部分既有方案假设有一套现成的企业微信/JWT体系，跟本项目 `kam_admin` 已经落地的"查 employee 表校验密码"模式不是一回事**，需要单独决策（见第二节）。

---

## 一、kam_sidebar 是什么、这一轮做到什么程度

回顾 02 号文档 2.1 节：`kam_sidebar` 是独立部署服务，FastAPI，企微侧边栏——纯展示+交互层，不持久化任何业务数据，通过 HTTP 发起任务、SSE 接收流式结果。

01 号需求文档 3.2 节的 MVP 范围：画像展示 + 逐字段确认交互（ok/discard/recreate）。本文档定稿时（），回复建议、标签推荐、日程提醒三个二三期功能 `kam_agent` 都还只有画像子图真实实现了（04号文档已确认），这三块当时只做 UI 占位，不接真实接口。

> ** 更新**：标签推荐（F04）、回复建议（F02/F03）已在第6阶段官方P0补齐时补做完成并真实联调通过——标签推荐整批勾选交互（`app.js` `tagState` 批量确认逻辑）、回复建议表单+场景切换交互（客户ID+销售/客服场景下拉框）均已接入真实接口，详见 `08-F04标签推荐设计文档.md`、`09-F02F03回复建议设计文档.md`。**日程提醒（F05）仍是 UI 占位**，尚未开工，是当前唯一还符合本节原始表述的一块。

**关键架构约束（02号文档2.2节，写代码时反复要对照）**：
- `kam_sidebar` **不直连 PostgreSQL/Redis**，也**不直接调用大模型**
- `kam_sidebar` **不引用 `kam_client`**——它的数据需求走 `kam_agent` 包装过的任务型接口（`/tasks/generate_profile`、`/tasks/stream/{thread_id}`、`/tasks/confirm`），场景单一，用不上 `kam_client` 封装的 Store 底层读写能力（那是 `kam_admin` 做多表聚合查询时才需要的）
- 所以 `kam_sidebar` 后端本质是一个"转发层"：接顾问的操作 → 转成对 `kam_agent` 的 HTTP/SSE 调用 → 把结果转发/渲染给前端

---

## 二、关键决策

### 决策1：登录鉴权方案（已定案，见下方"决策：方案A"）

**问题背景**：02 号文档 5.1 节接口清单里写了 `/auth/token`（POST，"用户名密码换Token"，标注调用方是 `kam_admin, kam_sidebar`），但实际开发 `kam_admin` 时（06号文档决策1），走的是完全不同的路径——`kam_admin` 是 Flask 服务端渲染应用，直接用 Flask **session** 存登录态，登录校验是自己调 `kam_client.get_employee(user_id)` 查 `employee` 表比对密码，全程没有涉及 `/auth/token` 这个接口。检查 `kam_agent/src/webapp/webapp.py` 源码（04号文档描述的三个核心接口 + `/store/*` 三个通用接口）确认：**`/auth/token` 从未被实现，是 02 号文档里一处只停留在纸面、没有被后续开发同步的设计**。

这是本文档必须先处理的分歧，不能假装 `/auth/token` 存在就直接调它。

**候选方案**：

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A：与 kam_admin 同款，走 `/store/get` 直查 employee | `kam_sidebar` 自己维护登录态（session 或简单 JWT），登录校验时调 `kam_agent` 已有的 `/store/get` 通用接口查 `("employee",)` 命名空间比对密码 | 复用已验证通过的 `/store/get` 接口，不需要改 `kam_agent`；与 `kam_admin` 登录逻辑一致，两处认知模型统一 | `kam_sidebar` 要自己实现一遍"查 employee+校验密码+发登录态"的逻辑，和 `kam_admin` 里的对应代码不共享（两个独立服务，没有共享代码层，这点和 `kam_client` 只服务 `kam_admin` 的架构选择是一致的） |
| B：补齐 `/auth/token`，两边都改成走它 | 在 `kam_agent` 补一个真正的 `/auth/token` 接口，`kam_admin` 现有登录逻辑也改成调它 | 落地 02 号文档原始设计，登录鉴权收口到 `kam_agent` 一处 | 改动范围最大——要动 `kam_agent`（新增接口）+ 已经端到端联调通过的 `kam_admin`（改造登录路径，有回归风险），且要重新设计 Token 机制（JWT？有效期？） |
| C：MVP 先免鉴权 | `kam_sidebar` 不做真实登录，URL 参数直接传 `follow_user_id` 模拟当前顾问身份 | 开发最快，先把核心画像交互链路跑通 | 演示/交付时鉴权是"假的"，属于明确的技术债，且和 01 号需求文档"非功能需求"里对登录鉴权的要求有落差 |

**决策：方案A**——`kam_sidebar` 自己维护登录态，登录校验时调 `kam_agent` 已有的 `/store/get` 通用接口查 `("employee",)` 命名空间比对密码，与 `kam_admin` 的登录逻辑保持同一套认知模型（都是"查 employee 表校验密码"），但代码各自独立实现（两个独立服务，本来就没有共享代码层，这点和 `kam_client` 只服务 `kam_admin` 的架构选择是一致的）。

**为什么不选B（补齐 `/auth/token`）**：`kam_admin` 的登录逻辑已经端到端联调验证通过（06号文档），如果为了"完全对齐02号文档原始设计"去改造它，属于给已验证代码引入不必要的回归风险；且 `/auth/token` 本身还要重新设计 Token 机制（JWT有效期、刷新策略等），工作量与当前"先出可跑通成果"的阶段目标不匹配，可以留作后续技术债。

**为什么不选C（免鉴权）**：01号需求文档非功能需求对登录鉴权有明确要求，`kam_admin` 已经证明"查 employee 表校验密码"这条路径在当前 Store 数据结构下是可行的，`kam_sidebar` 复用同一条路径成本很低，没有理由跳过。

**具体实现方式**：
- `kam_sidebar` 后端有一个登录路由，接收 `user_id`/密码，调 `kam_agent` 的 `POST /store/get`（`namespace=["employee"], key=user_id`）拿到员工记录，Python 侧比对 `password` 字段
- 校验通过后，用 FastAPI 自带的 `SessionMiddleware`（基于签名 Cookie，不需要额外的 Redis/数据库存 session）记住 `user_id`/`name`/`role`/`region`，后续请求从 session 读，不用每次都重新查一遍 `kam_agent`
- 这是"登录鉴权，不是数据范围权限过滤"——`kam_sidebar` 场景单一（顾问操作自己名下的客户画像），不像 `kam_admin` 那样要按 `role`/`region` 做多层数据可见范围过滤，登录校验通过、拿到 `user_id` 即可，不需要复用 `kam_admin` 的 `permissions.py`

---

### 决策2：kam_sidebar 后端怎么转发 SSE

**问题**：`kam_agent` 的 `/tasks/stream/{thread_id}` 直接返回 `text/event-stream`。`kam_sidebar` 是要"原样透传"这条 SSE 流给浏览器，还是要"消费完 kam_agent 的 SSE、自己再重新生成一条新的 SSE 给前端"？

**两种做法的差异**：
- **透传（proxy）**：`kam_sidebar` 后端用 `httpx` 以流式方式请求 `kam_agent` 的 SSE 端点，读到一行就原样 `yield` 给自己的响应流，不解析内容。前端连的是 `kam_sidebar` 的 SSE 端点，但数据格式和 `kam_agent` 发的完全一致。
- **消费再转发**：`kam_sidebar` 后端把 `kam_agent` 的 SSE 完整解析成 Python 对象，可以按需要重新组织字段（比如给前端补充一些 `kam_agent` 不知道的展示用信息），再自己格式化成 SSE 发给前端。

**决策：消费再转发**。理由：
1. 02 号文档 5.3 节 `kam_sidebar` 对外接口是 `/api/stream/{thread_id}`，与 `kam_agent` 的 `/tasks/stream/{thread_id}` 是两个独立的契约，`kam_sidebar` 作为"企微侧边栏"这一层，理论上未来可能需要在事件里补充侧边栏专属的展示字段（比如加一个客户姓名做标题），如果做透传代理，这个扩展点从架构上就被锁死了。
2. `kam_sidebar` 目前唯一拿到 `follow_user_id`/`external_id` 的地方是发起请求时，`kam_agent` 的 SSE 事件本身不带这两个字段（04号文档事件格式：`field/value/confidence/source/status/interrupt_id/thread_id`），消费再转发的模式下 `kam_sidebar` 可以顺手把这两个字段也拼进转发给前端的事件里，前端不需要自己再维护一份"当前是哪个客户"的状态。
3. 代价可接受：消费再转发意味着 `kam_sidebar` 后端要能正确解析 SSE 协议（`event:`/`data:` 两行一组），这部分是标准的字符串处理，工程性代码，不涉及设计判断。

---

### 决策3：逐字段确认交互怎么在前端管理状态

**问题**：`/tasks/stream/{thread_id}` 一次性推送多条 `profile_field_update` 事件（04号文档已确认：不是token级实时流，是"一次性生成完、循环推送"），每条事件都带各自独立的 `thread_id`（子线程ID）和 `interrupt_id`。前端收到这些事件后，怎么让顾问对着页面逐字段点 ok/discard/recreate，并且点击时能把**正确的那一对** `thread_id`/`interrupt_id` 传回 `/tasks/confirm`？

**决策**：前端维护一个以 `field` 为 key 的内存对象（比如 `fieldsState = {project_stage: {value, confidence, source, status, thread_id, interrupt_id}, ...}`），SSE 每收到一条 `profile_field_update` 就往这个对象里写一条。页面对每个字段渲染一张卡片（值/置信度/来源 + 三个按钮），按钮的点击事件闭包里绑定的是这个字段自己的 `thread_id`/`interrupt_id`，不依赖用户重新输入或者去别处查。点 `recreate` 后，`/tasks/confirm` 响应体里会直接带回新草稿（04号文档 5.1.3 节已确认这个行为），前端用响应体里的新值**原地更新**这张卡片（`interrupt_id` 也要更新成 `new_interrupt_id`，否则下次点确认会用旧的、已经失效的 `interrupt_id`）。

**容易出错的点（写代码时要小心）**：`recreate` 之后卡片的 `interrupt_id` 变了，如果前端状态更新时漏改这个字段，下一次点 ok/discard 会拿旧 `interrupt_id` 去调 `/tasks/confirm`，后端会报错（`Command(resume=...)` 定位不到这个 `interrupt_id`）——这是本模块最容易复现"忘了更新状态导致的bug"的地方，测试时要专门验证"recreate 一次之后再 ok 一次"这条路径。

---

## 三、目录结构

```
kam_sidebar/
├── main.py                    # FastAPI 应用入口 + 路由
├── kam_agent_client.py        # 封装对 kam_agent 的 HTTP/SSE 调用（同款风格于 kam_client 的 _http.py，
│                               #   但这里不是"共享包"，是 kam_sidebar 内部的一个模块）
├── static/
│   ├── index.html             # 主页面：客户信息 + 画像卡片列表 + 二三期占位区
│   ├── login.html             # 登录页
│   ├── app.js                 # 页面逻辑：发起生成、监听SSE、渲染卡片、绑定确认按钮
│   └── styles.css
├── .env.example                # KAM_AGENT_BASE_URL 等配置
└── pyproject.toml
```

**为什么不套用 `kam_admin` 的 Flask 服务端渲染模式**：02 号文档技术选型已经定了 `kam_sidebar` 用 FastAPI 是因为 SSE 需要异步长连接支持（04号文档同样引用了这条判断）。页面本身用最朴素的 FastAPI 挂静态文件 + 原生 `EventSource` API 消费 SSE，不引入前端框架——MVP 阶段页面复杂度低（一个登录页+一个主页面），既有方案 `kam_sidebar/kam_web/` 也是纯 HTML+JS+CSS 无框架的路子，这部分对照理解后判断沿用是合理的（不是设计难点，是工程选型）。

---

## 四、接口清单（kam_sidebar 对外，02号文档5.3节落地）

> 下表为定稿时的MVP范围（仅F01）。**（已订正，见下方说明）**：曾记录"完整实际接口以 `kam_sidebar/src/app.py` 源码为准"，这是一处错误——`src/app.py`是当时刚起步、从未真正接入运行链路的一套平行实现（跟`kam_admin`/`kam_client`踩过的同类问题一样），真正在跑的入口从始至终是`main.py`+`static/`，两者接口命名不完全一样。**订正**：核对`main.py`源码重新写了下表，`src/`已确认没有任何独有价值并删除。

| 接口 | 方法 | 用途 | 转发到 kam_agent 的哪个接口 |
|---|---|---|---|
| `/login` | POST | 登录校验（页面本身是`/static/login.html`静态文件，登录态用签名Cookie） | 见决策1 |
| `/logout` | POST | 登出 | — |
| `/api/me` | GET | 取当前登录人信息（姓名展示、拼follow_user_id用） | — |
| `/` | GET | 主页面（登录后，未登录跳转`/static/login.html`） | — |
| `/api/generate_profile` | POST | F01发起画像生成（与定稿时命名一致，未改名） | `POST /tasks/generate_profile` |
| `/api/stream/{thread_id}` | GET（SSE） | 订阅画像生成实时展示 | `GET /tasks/stream/{thread_id}`（消费再转发，见决策2） |
| `/api/confirm` | POST | 顾问逐字段确认 | `POST /tasks/confirm`（透传请求体） |
| `/api/generate_tags` | POST | F04标签推荐发起（接入，此前MVP范围表遗漏未记） | `POST /tasks/generate_tags` |
| `/api/stream_tags/{thread_id}` | GET（SSE） | F04标签推荐实时展示 | `GET /tasks/stream_tags/{thread_id}` |
| `/api/confirm_tags` | POST | F04标签批量确认 | `POST /tasks/confirm_tags` |
| `/api/generate_chat_suggestion` | POST | F02销售聊天建议（接入） | `POST /tasks/generate_chat_suggestion` |
| `/api/generate_kf_chat_suggestion` | POST | F03客服聊天建议（接入） | `POST /tasks/generate_kf_chat_suggestion` |
| `/api/customers` | GET | 登录顾问名下客户列表（接入，"沟通记录"tab用） | `GET /tasks/customers` |
| `/api/chat_history` | GET | 指定客户+渠道的聊天记录 | `GET /tasks/chat_history` |
| `/api/kb_answer` | POST | F16知识库问答（**从`src/`移植进`main.py`才真正生效**，不再要求客户ID，见下方说明） | `POST /tasks/generate_kb_answer` |
| `/api/generate_reasoning` | POST | F17跨模块推理，两段式第一阶段，只发号不执行（**从`src/`移植进`main.py`才真正生效**；命名与定稿设想的`/api/reasoning`不同） | `POST /tasks/generate_reasoning` |
| `/api/stream_reasoning/{thread_id}` | GET（SSE） | F17步骤透明实时推送（**从`src/`移植进`main.py`才真正生效**；命名与定稿设想的`/api/reasoning/stream/{thread_id}`不同） | `GET /tasks/stream_reasoning/{thread_id}` |
| `/api/health` | GET | 健康检查 | — |

**补充：F16不要求客户ID**——`/api/kb_answer`的请求体只有`question`一个字段，不接受也不需要`external_id`。查证`kam_agent`的`load_data`节点对不存在的客户已有优雅兜底、且`kam_agent`自己的`知识库问答实验`测试脚本一直用假`external_id`验证通过后确认，客户ID对F16只是历史遗留的接口必填校验、不是真实功能依赖；`main.py`内部用固定占位值`_KB_NO_CUSTOMER_EXTERNAL_ID`代替，转发给kam_agent时这个字段依然会传（kam_agent自己的接口契约没有变），只是sidebar这一层不再要求顾问手动输入。

---

## 五、下一步（原计划，已全部执行完毕）

1. 写 `main.py`/`kam_agent_client.py`（登录路由按决策1、SSE转发按决策2）
2. `login.html`/`index.html`/`app.js` 按第二节决策3的状态管理方式实现
3. 端到端联调：启动 `kam_agent` + `kam_sidebar`，走一遍"登录 → 发起生成 → SSE接收 → 逐字段确认（含至少一次recreate）"完整链路

**后续**：F02/F03/F16/F17四个能力的UI已在此基础上补齐并真实浏览器验证通过，完整过程见 `实战记录.md` 各条目。
