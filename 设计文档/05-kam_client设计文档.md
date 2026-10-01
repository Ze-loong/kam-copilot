# kam_client 模块设计文档

> 状态：开发实现（软件工程五步精简里程碑 · 第3步，`kam_client` 是这一步第一个开工的组件）
> 输入：`01-需求规格说明书.md`、`02-架构设计文档.md`（服务边界2.1/2.2节、Store数据设计四、接口清单五）
> 定稿日期：

> 连接方式、接口粒度与门面类的取舍见第二节。

---

## 一、kam_client 是什么、为什么需要它

回顾02号文档2.1节对四个组成部分的定位：

| 组成部分 | 类型 | 职责 |
|---|---|---|
| kam_agent | 独立部署服务 | 核心AI引擎，**唯一**持有PostgreSQL/Redis连接权限 |
| kam_admin | 独立部署服务 | 管理后台，不直连数据库 |
| kam_sidebar | 独立部署服务 | 侧边栏展示层，不直连数据库 |
| **kam_client** | **共享Python包（非独立进程）** | **封装Store底层API调用细节，仅被kam_admin引用** |

kam_client 不是一个独立跑起来的服务，是一段被 import 的 Python 代码。它存在的意义是把"kam_admin 要读画像/订单/标签数据，但自己不能直连数据库"这件事，从"每个 Flask 路由函数里散落地拼 HTTP 请求" 变成 "调一个类方法"。对应02号文档的表述，这是**适配器/门面模式**：kam_admin 面对的是干净的业务方法（`get_customer_profile(external_id)`），不需要关心底层是拼 JSON 发 HTTP 请求给 kam_agent。

kam_sidebar 为什么不需要 kam_client（02号文档2.2节已有结论，这里不重复推导）：sidebar 场景单一（发起画像生成任务/展示流式结果/提交确认），走的是 `/tasks/*` 任务型接口，不需要 kam_client 封装的 Store 底层读写能力。这条边界在这次设计里保持不变。

---

## 二、关键决策

### 决策1：连接方式 —— 自定义REST接口，不用 LangGraph SDK

**问题**：kam_client 怎么拿到 kam_agent 手里的数据？02号文档5.1节列了 `/store/get`、`/store/put`、`/store/search` 三个接口名，但当时只写了"用途"没有实现，也没定具体走法。

**SDK 连接方案**：LangGraph SDK 的 Store HTTP API 依赖 LangGraph Server 部署；本项目的 FastAPI 服务自行暴露 Store 接口，因此采用薄 REST 客户端。

**为什么不采纳**：本项目的 `kam_agent` 已经走通并端到端验证过的部署形态是**纯 FastAPI 应用**（`uvicorn` 直接跑 `webapp.py`），`/tasks/generate_profile`、`/tasks/stream/{thread_id}`、`/tasks/confirm` 三个核心接口都是在这套形态下验证通过的。如果为了 kam_client 改成 LangGraph Server 模式，代价是要把这三个已验证接口的部署方式一起推翻重来，风险和工作量都远大于收益。

**决策**：在现有 FastAPI 架构里，给 `kam_agent/webapp.py` 补三个通用 REST 接口（`/store/get`、`/store/put`、`/store/search`），`kam_client` 用标准 `requests` 库调用它们。不引入 `langgraph_sdk` 依赖，不改变 kam_agent 现有部署方式。

### 决策2：接口粒度 —— 通用泛型接口，不做逐命名空间拆分

**两个备选方案**：

| 方案 | 接口数量 | 优点 | 代价 |
|---|---|---|---|
| **通用泛型接口**（采纳） | 3个（get/put/search） | 代码量小，与 `store_client.py` 现有的 `store=None` 依赖注入模式自然契合 | 接口层不做命名空间白名单校验，信任调用方 |
| 逐命名空间语义化接口 | 21个（7命名空间 × 3方法） | 语义明确，可单独加校验逻辑 | 开发工作量明显更大 |

**决策依据**：这三个接口只给内网的 `kam_admin`（通过 `kam_client`）调用，不对外部网络暴露，"谁能读写哪个命名空间"这类权限判断本来就应该在 `kam_admin` 的两维度权限模型（02号文档4.2节 `role`+`region`）里做，不是 Store 接口层的职责。接口层做成通用透传，权限判断留给上层，职责边界更清晰，也符合"仓储模式思路，数据库操作收口在一处"的既有原则（02号文档2.2节）。

**已知代价，明确接受**：如果未来 kam_sidebar 或其他未授权组件拿到这三个接口的地址，理论上可以绕过业务层直接读写任意命名空间。当前项目形态下（内网部署、组件数量少）这个风险可控，暂不引入接口级鉴权；如果项目往生产化演进，这里需要补一层服务间认证（如内网 API Key），记入待办。

### 决策3：namespace 的传输格式 —— JSON 数组，接口内部转元组

`store_client.py` 里所有函数要求 `namespace` 必须是 Python `tuple`（这是 LangGraph Store 的硬性要求，之前 `store_client.py` 开发阶段已经踩过"元组语法拆开传"的坑，见 `实战记录.md`）。但 HTTP/JSON 没有元组类型，只有数组。所以约定：**kam_client 发请求时把 namespace 序列化成 JSON 数组，kam_agent 接口内部收到后 `tuple(payload.namespace)` 转回元组**，两端各自在自己的语言边界内保持类型正确。

### 决策4：KamStoreAPI 类的方法设计 —— 语义化方法名，内部薄封装

`KamStoreAPI` 通过语义化方法隐藏 namespace 和 HTTP 细节，调用方不必重复拼接存储键。

**决策**：kam_client 的 `KamStoreAPI` 类保留这层语义化封装，但内部实现换成 `requests.post` 调自己的 `/store/*` 接口（不是 `langgraph_sdk`）。每个方法内部做的事情很薄——拼 namespace、发请求、把响应里的 `item.value` 拆出来返回给调用方，不在这一层做业务逻辑判断（业务判断留给 `kam_admin` 或已经在 `store_client.py` 里实现好的合并规则，比如 `external_user_profile` 的置信度合并逻辑已经在 kam_agent 侧的 `upsert_external_user_profile` 里做完，kam_client 只是转发写请求，不重复实现这段逻辑）。

---

## 三、目录结构与文件职责

```
kam_client/
├── .python-version
├── pyproject.toml
├── .env.example              # KAM_AGENT_BASE_URL 配置项样例
└── store_client/
    ├── __init__.py            # 对外只暴露 KamStoreAPI / create_kam_store_api / KamStoreAPIError
    ├── _http.py                # StoreHTTPClient：纯网络层，不含任何业务语义
    ├── kam_store_api.py        # KamStoreAPI：门面类，7命名空间的语义化方法 + 聚合查询
    └── kam_client_probe.py     # 真实联调验证脚本（不是 unittest）
```

**为什么拆成 `_http.py` 和 `kam_store_api.py` 两个文件，而不是一个类里全写完**：对照 `kam_agent` 侧 `store_client.py` 的既有分层习惯——`store_client.py` 里每个函数只管"怎么调用 LangGraph Store 的 get/put/search"，不掺业务判断；画像合并这类真正有业务含义的规则，单独收在 `upsert_external_user_profile` 一个函数里、注释写清楚"为什么这么合并"。这里复用同一个分层原则：

- **`_http.py`（`StoreHTTPClient`）** 只管"怎么把一次请求发给 kam_agent、怎么处理网络层失败"，不知道 `employee`、`external_user` 这些命名空间的存在，也不知道 `sorted_from_to_key` 这种业务拼接规则。它的 `get`/`put`/`search` 三个方法直接对应 kam_agent 的三个 HTTP 接口，是纯粹的协议转换层。
- **`kam_store_api.py`（`KamStoreAPI`）** 只管"每个命名空间该传什么 namespace、该起什么方法名"，不知道 HTTP 请求具体怎么发、失败了要不要重试——这些细节全部委托给 `StoreHTTPClient`。

**这么拆的价值**：如果以后网络层要改（比如决策2提到的"生产化前要补服务间鉴权"，或者要加重试/超时退避策略），只需要改 `_http.py`，`KamStoreAPI` 的业务方法一行都不用动；反过来如果要新增一个命名空间的方法（比如未来画像开放编辑权限，要新加一个绕过直通写、走专属合并接口的方法），只需要改 `kam_store_api.py`。两类改动的触发原因完全不同（网络层是运维/安全考量，方法层是业务需求变化），拆开后互不干扰。

**为什么单独抽一个 `_sorted_from_to_key` 函数、放在 `kam_store_api.py` 顶层而不是塞进 `_http.py`**：这个拼接规则是 `wxqy_msg` 命名空间的业务约定（"顾问ID+客户ID排序后拼接"这件事本身有业务含义），跟"怎么发HTTP请求"无关，所以放在业务方法所在的 `kam_store_api.py`，不下沉到不该知道命名空间语义的 `_http.py`。

**`__init__.py` 只暴露三个名字**：`KamStoreAPI`、`create_kam_store_api`（工厂函数）、`KamStoreAPIError`（异常类型，供 kam_admin 侧 `except` 捕获）。`StoreHTTPClient` 不导出——kam_admin 不应该直接碰网络层，只应该通过 `KamStoreAPI` 门面调用，这是"门面模式"这个决策（决策4）在文件组织上的直接体现，不是随手漏写。

**`kam_client_probe.py` 为什么放进 `store_client/` 包内部、而不是像 kam_agent 那样单独建一个 `experiments/` 目录**：kam_agent 的 `experiments/` 独立存放 LangGraph 最小复现脚本，与正式图和节点代码分开。kam_client 目前只有一个包、代码量小，这个探针脚本用于验证 KamStoreAPI 的方法，放在包内用 `-m` 方式运行更简单。如果验证脚本增多，再拆出独立目录。

---

## 五、依赖的接口契约（kam_agent 侧）

> **验证状态（）**：以下三个接口已在本机真实启动 FastAPI、连接 `kam-postgres` 后验证通过：`/store/put` 成功写入一条 employee probe 数据，`/store/get` 完整读回 key 与 value，`/store/search` 命中该记录。`store.get()` / `store.search()` 的 `Item` 对象已按本节契约转为普通 dict 后再返回。探针数据 `probe_store_api_001` 按现有 `probe_` 前缀惯例暂留数据库。

### 5.1 `POST /store/get`

请求体：
```json
{
  "namespace": ["employee"],
  "key": "u001"
}
```
响应体：
```json
{
  "item": {
    "key": "u001",
    "value": {"user_id": "u001", "name": "小张", "role": "consultant", "region": "华东"}
  }
}
```
未命中时 `item` 为 `null`。

### 5.2 `POST /store/put`

请求体：
```json
{
  "namespace": ["employee"],
  "key": "u001",
  "value": {"user_id": "u001", "name": "小张", "role": "consultant", "region": "华东"}
}
```
响应体：
```json
{"success": true}
```

**注意**：`/store/put` 是**直通写入**，不经过 `store_client.py` 里各命名空间专属的合并规则（比如 `external_user_profile` 的"已确认字段不被覆盖"逻辑，只存在于 `upsert_external_user_profile` 这个函数内部）。这意味着如果 kam_client 直接对 `external_user_profile` 命名空间调 `/store/put`，会绕过合并规则、造成覆盖已确认字段的风险。**当前 kam_admin 的功能范围（02号文档5.2节路由表）不涉及画像字段的写入**（画像生成/确认全部在 kam_sidebar 侧走 `/tasks/*` 完成），所以这个风险目前不会被触发；但如果未来 kam_admin 要新增"人工编辑画像"这类功能，需要另外补一个专走 `upsert_external_user_profile` 合并逻辑的接口，不能直接用这个通用 `/store/put`。这条记入下方"待确认"事项。

### 5.3 `POST /store/search`

请求体：
```json
{
  "namespace": ["external_user", "u001"],
  "filter": null,
  "limit": null
}
```
响应体：
```json
{
  "items": [
    {"key": "ext_8891", "value": {"external_id": "ext_8891", "name": "王女士", "...": "..."}}
  ]
}
```

---

## 六、KamStoreAPI 方法清单（按命名空间分组）

对应02号文档4.3节确认的7个命名空间。每组给出方法签名和对应的底层调用，不含完整实现代码（详细度对齐本文档定位——设计文档不是代码骨架）。

### 6.1 `employee`（员工）

| 方法 | 底层调用 |
|---|---|
| `get_employee(user_id) -> dict \| None` | `/store/get`，namespace=`["employee"]` |
| `list_employees() -> list[dict]` | `/store/search`，namespace=`["employee"]` |

kam_admin 的员工管理模块（02号文档5.2节）用这两个方法。写入（新增员工）走 `/store/put` 直通即可，`employee` 命名空间没有合并逻辑，不存在决策4提到的那类风险。

### 6.2 `external_user`（客户基础信息）

| 方法 | 底层调用 |
|---|---|
| `get_external_user(follow_user_id, external_id) -> dict \| None` | `/store/get`，namespace=`["external_user", follow_user_id]` |
| `list_external_users(follow_user_id) -> list[dict]` | `/store/search`，namespace=`["external_user", follow_user_id]` |

kam_admin 客户列表页（`/api/customers`）用 `list_external_users`，需要结合 `employee` 表的 `role`/`region` 做权限过滤（过滤逻辑在 kam_admin 侧，不在 kam_client）。

### 6.3 `external_user_profile`（客户画像）

| 方法 | 底层调用 |
|---|---|
| `get_customer_profile(follow_user_id, external_id) -> dict \| None` | `/store/get`，namespace=`["external_user_profile", follow_user_id]` |

只读方法。客户详情页（`/api/customers/<external_id>`）展示画像用。**不提供写方法**——理由见决策4的风险说明，画像写入始终只走 kam_agent 内部的 `upsert_external_user_profile`（经由 `/tasks/confirm` 触发），kam_client 这层不重复暴露这条写路径，避免调用方误用直通 `/store/put` 绕过合并规则。

### 6.4 `tags_setting`（标签体系）

| 方法 | 底层调用 |
|---|---|
| `get_tag(tag_id) -> dict \| None` | `/store/get`，namespace=`["tags_setting"]` |
| `list_tags() -> list[dict]` | `/store/search`，namespace=`["tags_setting"]` |
| `upsert_tag(tag_id, ...) -> None` | `/store/put`，namespace=`["tags_setting"]` |

标签管理模块（02号文档5.2节 `/api/tags`）用。`upsert_tag` 走直通写入没有风险——`tags_setting` 是纯配置数据，没有草稿态/合并逻辑。

### 6.5 `wxqy_msg`（企微聊天消息）

| 方法 | 底层调用 |
|---|---|
| `list_chat_messages(follow_user_id, external_id, after_yyyymmdd=None) -> list[dict]` | `/store/search`，namespace=`["wxqy_msg", sorted_key]`，`sorted_key` 由 kam_client 内部按 `store_client.py` 同款规则拼接（`"".join(sorted([follow_user_id, external_id]))`） |

**注意**：`_sorted_from_to_key` 拼接逻辑在 kam_agent 侧的 `store_client.py` 里，kam_client 这边要**重新实现一份同样的拼接函数**（不是共享代码，两个进程之间没有代码复用关系，只能各自实现、保证算法一致）。这是"进程边界"这个架构选择的直接代价，记入下方技术备忘。

### 6.6 `wxkf_msg`（微信客服消息）

| 方法 | 底层调用 |
|---|---|
| `list_kf_messages(external_id, after_yyyymmdd=None) -> list[dict]` | `/store/search`，namespace=`["wxkf_msg", external_id]` |

客服记录接口供客户详情页查询；界面以实际模板和路由为准。

### 6.7 `wxxd_order`（客户订单）

| 方法 | 底层调用 |
|---|---|
| `list_orders(union_id) -> list[dict]` | `/store/search`，namespace=`["wxxd_order", union_id]` |

订单管理模块（02号文档5.2节 `/api/orders/<union_id>`）用。

---

## 七、聚合查询：客户详情页怎么组装

02号文档4.3节提到 kam_client 的核心价值场景是"客户详情页联合查画像+订单+标签"。这不是 Store 底层的单一命名空间能力，是 kam_client 在**方法组合层面**做的事：

```
get_customer_detail(follow_user_id, external_id):
    1. external_user   = get_external_user(follow_user_id, external_id)   # 基础信息+tags[]
    2. profile         = get_customer_profile(follow_user_id, external_id)
    3. union_id         = external_user["union_id"]
    4. orders           = list_orders(union_id)
    5. tag_details      = [get_tag(tid) for tid in external_user["tags"]]  # 展开tag_id为完整标签对象
    6. 组装成一个dict返回给 kam_admin 路由函数
```

这一步涉及 4 次独立的 HTTP 请求（get_external_user + get_customer_profile + list_orders + N次get_tag），**没有做成一次批量接口**——决策依据同"确认接口设计为单字段接口"那条既有原则（02号文档5.1.3节）：接口层保持单一职责，组合逻辑留给调用方，避免为了一个页面场景设计一个专用的"大而全"接口，牺牲通用性。如果未来发现性能是瓶颈（比如标签数量多导致 N+1 次请求明显变慢），可以再考虑给 `tags_setting` 加一个"批量按 tag_id 列表查"的方法优化，当前不做过早优化。

---

## 八、错误处理原则

kam_client 是纯粹的网络调用层，对 HTTP 层面的失败（连接超时、kam_agent 未启动、5xx错误）统一让异常往上抛，不在 kam_client 内部吞掉或转成静默返回 `None`——原因是"kam_agent 挂了"和"数据确实不存在（返回 `item: null`）"是两种完全不同的情况，前者是系统性故障需要让 kam_admin 感知到（比如显示"服务暂时不可用"），后者是正常的业务分支（比如"这个客户还没有画像数据"）。两者如果被混在一起用同一个 `None` 表示，调用方没法区分，容易掩盖真实故障。

具体实现上，`requests` 库对连接失败/超时会抛异常，对 4xx/5xx 状态码需要主动调用 `response.raise_for_status()` 才会抛，kam_client 内部统一做这个检查。

---

## 九、待确认 / 已知限制

- **画像写入路径**：如决策4/6.3节所述，kam_client 目前不提供画像写方法。如果后续 kam_admin 需要"顾问在后台手动编辑画像字段"这个功能（当前01/02号文档都没有这条需求），需要另外设计一个走 `upsert_external_user_profile` 合并逻辑的专用接口，不能复用通用 `/store/put`。
- **接口级鉴权缺失**：`/store/*` 三个接口目前没有做任何身份校验，任何能访问到 kam_agent 网络地址的调用方都能读写任意命名空间。当前内网部署场景下风险可控，生产化前需要补一层服务间认证。
- **N+1 查询**：客户详情页聚合查询（第七节）目前是多次独立请求，标签多的客户会有多次 `get_tag` 调用，暂不优化，留意实际使用中是否成为瓶颈。
- **`KamStoreAPI` 真实联调已通过（）**：启动本机 `kam-postgres` 与 kam_agent 后运行 `uv run python -m store_client.kam_client_probe`，employee 写读查、tags 写读、客户不存在时的 `get_customer_detail` 容错共4项全部 PASS。`probe_kam_client_*` 数据按既有 probe 前缀惯例暂留数据库。验证中曾遇到一次 500，根因是前一轮测试遗留的 uvicorn 进程持有 PostgreSQL 重启前失效的连接；停止遗留进程并启动干净服务后复测通过，非 kam_client 代码缺陷。

---

## 下一步

1. `kam_client` 已完成，进入 `kam_admin` 骨架搭建，按02号文档5.2节路由表逐个接入。
