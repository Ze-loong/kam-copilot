# kam_agent 接口实现说明（从零基础复查版）

> 对应代码：`kam_agent/src/webapp/webapp.py`
>
> 目的：说明 kam_agent 怎样把“页面或管理后台的请求”接到 LangGraph，再把结果安全地返回。本文讲接口层的实现，不重复讲 LLM prompt 和画像业务规则。
>
> 最后核对代码：

---

## 一、先建立整体概念

### 1.1 `webapp.py` 是什么

可以把整个系统想成一家机构：

- `kam_sidebar`：顾问使用的侧边栏页面；
- `kam_admin`：员工、客户、订单等管理后台；
- `kam_agent`：负责分析客户画像的“AI 大脑”；
- `webapp.py`：AI 大脑的前台接待员。

页面不能直接调用 Python 函数或直接操作 LangGraph。它只能通过 HTTP 请求访问一个 URL；`webapp.py` 用 FastAPI 把这些 URL 注册成接口，再在接口函数里调用图和 Store。

**接口**就是双方约好的“窗口”：调用方按约定传数据，服务端按约定处理并返回数据。比如 `POST /tasks/generate_profile` 的意思是“向服务器提交一次生成画像任务”。

### 1.2 本文件的接口分组

| 分组 | 接口 | 谁会调用 | 作用 |
|---|---|---|---|
| AI 任务 | `POST /tasks/generate_profile` | sidebar | 创建一次画像生成任务编号 |
| AI 任务 | `GET /tasks/stream/{thread_id}` | sidebar | 建立 SSE 连接并接收各字段草稿 |
| AI 任务 | `POST /tasks/confirm` | sidebar | 确认、放弃或重新生成一个字段 |
| 数据访问 | `POST /store/get` | kam_client / admin | 读取一条 Store 数据 |
| 数据访问 | `POST /store/put` | kam_client / admin | 写入或更新一条 Store 数据 |
| 数据访问 | `POST /store/search` | kam_client / admin | 搜索多条 Store 数据 |

`POST` 一般表示“提交一个会改变任务或数据的动作”；`GET` 一般表示“建立读取连接”。本项目把 SSE 放在 GET，是因为浏览器的 `EventSource` 规范就是用 GET 建立长连接。

---

## 一之五、FastAPI 零基础入门：一个接口是怎么拼出来的

> 这一节补在“整体概念”和“完整时序”之间，专门讲清楚 FastAPI 本身的语法机制——上面已经在讲“接口是窗口”，这里往下一层，讲“这扇窗口的四块牌子分别是什么”。看懂这一节之后，第四、五、六节的每一段代码都能对号入座。

### 1.5.1 为什么需要 FastAPI 这样一个框架

Python 代码本身不会主动“听”网络上有没有人发请求过来——监听端口、解析 HTTP 报文、把请求内容变成 Python 能用的数据、再把 Python 的返回值拼成合法的 HTTP 响应发回去，这一整套是很繁琐的底层工作。FastAPI 就是把这些脏活全包了：你只管写“收到请求之后要做什么”，剩下的“怎么收、怎么发”交给它。

### 1.5.2 一个最小接口，拆成四块牌子

用文档里最简单的那个接口做例子（对应第五节 5.1 的代码）：

```python
class GenerateProfileRequest(BaseModel):
    follow_user_id: str
    external_id: str


class GenerateProfileResponse(BaseModel):
    thread_id: str


@app.post("/tasks/generate_profile", response_model=GenerateProfileResponse)
def generate_profile(payload: GenerateProfileRequest):
    thread_id = f"thr_{uuid.uuid4().hex}"
    return GenerateProfileResponse(thread_id=thread_id)
```

可以把这四块想成挂在“接待窗口”上的四块牌子：

**牌子①：请求表单长什么样（`GenerateProfileRequest`）**

`class GenerateProfileRequest(BaseModel):` 不是面向对象编程里那种复杂的类，可以先简单当成“一张表格的表头”——规定了对方交上来的表格必须有哪几栏、每一栏是什么类型。这里规定了：必须有 `follow_user_id`（字符串），必须有 `external_id`（字符串）。`BaseModel` 是 Pydantic 这个库提供的，专门干“定义数据长什么样 + 自动检查对不对”这件事。如果调用方少填了一栏，或者把 `external_id` 填成数字而不是字符串，FastAPI 会在请求进入函数体之前就自动挡下来、报错，你完全不用在函数里手写 `if "external_id" not in data` 这种检查代码。

**牌子②：回复表单长什么样（`GenerateProfileResponse`）**

同样的道理，规定了“我回复给对方的表格，只有一栏 `thread_id`，字符串类型”。

**牌子③：这扇窗口开在哪、收什么方式的信（`@app.post(...)`）**

`@app.post("/tasks/generate_profile", response_model=GenerateProfileResponse)` 这一整行叫“装饰器”，贴在函数正上方。意思是：“把下面这个函数注册成一个接口，网址是 `/tasks/generate_profile`，只收 POST 方式的请求；处理完之后，请按牌子②的表头去检查、包装成 JSON 发回去。”POST 一般表示“提交一个会改变状态的动作”，跟本文档 1.2 节的说明是一致的。

**牌子④：窗口后面真正干活的人（函数体）**

```python
def generate_profile(payload: GenerateProfileRequest):
    thread_id = f"thr_{uuid.uuid4().hex}"
    return GenerateProfileResponse(thread_id=thread_id)
```

`payload: GenerateProfileRequest` 这个参数，是 FastAPI 已经按牌子①的表头，把对方发来的 JSON 自动解析、检查好之后，包装成一个 Python 对象直接递给你的——函数体里可以直接写 `payload.follow_user_id` 取值，不用自己解析 JSON 字符串。函数体自己只干“业务逻辑”这一件事（这里是生成一个随机字符串），最后把结果按牌子②的格式 `return` 出去，FastAPI 接手剩下的转 JSON、拼 HTTP 响应的工作。

### 1.5.3 串起来看一次完整的一问一答

前端发一个 POST 请求到 `/tasks/generate_profile`，body 是 `{"follow_user_id": "follow_001", "external_id": "external_001"}` → FastAPI 先按牌子①检查这份 JSON 合不合规矩，合规矩就转成 `payload` 对象 → 调用 `generate_profile(payload)` 这个函数体 → 函数体 `return` 出一个 `GenerateProfileResponse` 对象 → FastAPI 按牌子②把它转成 JSON `{"thread_id": "thr_xxxx"}` → 发回给前端。

这四块牌子的组合，是本项目里**所有**接口的通用套路——第五、六节讲的 `/tasks/stream`、`/tasks/confirm`、`/store/get` 等等，都是同一个模式，只是牌子①②里的字段不一样、牌子④的函数体干的事更复杂（比如真的去调用图、做 SSE 推送）。读后面几节时，先在脑子里把代码按这四块牌子归类，会比逐行硬看轻松很多。

### 1.5.4 `lifespan`、全局字典，为什么这套跟“四块牌子”是两回事

看到这里可能会有个疑问：第三节讲的 `lifespan`、`_graph_registry` 这些，跟“四块牌子”是什么关系？答案是——它们解决的是完全不同的问题，不要混在一起理解：

- “四块牌子”解决的是**单次请求**内的事：这一次请求进来，数据长什么样、处理完返回什么样。
- `lifespan` + `_graph_registry` 解决的是**服务启动到关闭这段时间**的事：数据库连接、编译好的图这些“很贵、不该每次请求都重新造”的东西，只在服务启动那一刻准备一次，之后所有请求的“牌子④函数体”都从同一个地方去取用（`get_kam_graph()` 这些函数干的就是“去取用”这件事）。

可以类比成：“四块牌子”是餐厅每一次接待客人的标准流程（点单表、上菜表、几号窗口、传菜员），`lifespan` 是开店前把水电厨房准备好这件事——两者都必要，但一个只发生一次（开店时），一个每次客人来都要走一遍。

---

## 二、一次画像生成的完整时序

```text
顾问点击“生成画像”
  │
  ├─ 1. POST /tasks/generate_profile
  │       返回主任务 thread_id；此时不调用 LLM、不运行图
  │
  ├─ 2. GET /tasks/stream/{thread_id}?follow_user_id=...&external_id=...
  │       浏览器先把 SSE 长连接建好
  │
  ├─ 3. webapp.py 调 kam_graph.invoke(...)
  │       主图加载数据 → 一次 LLM 生成所有字段草稿
  │       → 每个字段进入自己的画像子图并停在 interrupt()
  │
  ├─ 4. 服务端经 SSE 按字段推送 profile_field_update
  │       页面逐条展示草稿，最后收到 done
  │
  └─ 5. 顾问点击“确认 / 放弃 / 重生成”
          POST /tasks/confirm
          → profile_graph.invoke(Command(resume=...))
          → 从该字段的 interrupt 精确继续执行
          → 确认/放弃结束，或返回新的草稿
```

这里刻意不在第 1 步直接运行 `graph.invoke()`：如果 AI 先跑完、浏览器后建立 SSE 连接，那么“运行过程中产生的事件”可能已经错过。先拿到任务号、再建立流、最后执行图，可以避免这一竞态。

### 2.1 两种 `thread_id`，不要混淆

| 名称 | 示例 | 作用 |
|---|---|---|
| 主任务 thread_id | `thr_abc123` | 标识“这一次生成画像任务”，供主图执行和 SSE `done` 使用 |
| 字段子 thread_id | `thr_abc123:project_stage` | 标识“这次任务中的技改阶段字段子图”，供 `/tasks/confirm` 恢复 |

现在每个字段有自己的子 thread。原因是一次画像可能有多个字段同时停在 `interrupt()`；让它们共享一个图执行实例时，某字段的 `recreate` 会碰到 LangGraph 的“多 pending interrupt + 动态 Send”边界。独立 thread 后，每个子图实例只等待一个字段，确认或重生成不会干扰其他字段。

---

## 三、程序启动：先把基础设施准备好

对应 `lifespan()`（`webapp.py` 第 85 行）。FastAPI 启动应用时先进入这个函数，关闭时再退出它。

### 3.1 为什么不能每个请求都重新连接数据库

启动时代码创建一次 `PostgresSaver` 和 `PostgresStore`，在应用运行期间复用：

```python
with PostgresSaver.from_conn_string(connection_uri) as checkpointer, \
     PostgresStore.from_conn_string(connection_uri) as store:
```

这相当于餐厅开门时先把水、电和厨房准备好，而不是每来一位客人就重新装修厨房。`with` 的好处是服务关闭时连接池会被正确关闭。

### 3.2 `checkpointer` 与 `store` 的区别

| 对象 | 保存什么 | 在本项目中的作用 |
|---|---|---|
| `PostgresSaver`，变量名 `checkpointer` | 图执行进度、暂停点、可恢复状态 | 让 `interrupt()` 后的 `Command(resume=...)` 知道从哪里继续 |
| `PostgresStore`，变量名 `store` | 员工、客户、聊天、订单、画像等业务数据 | 让节点读取上下文、确认后写入正式画像 |

两者都使用 PostgreSQL，但不是同一件事。只传 `checkpointer`，图可以暂停却读不到业务数据；只传 `store`，能读写业务数据却不能可靠恢复暂停任务。因此 `build_profile_graph(checkpointer, store)` 和 `build_kam_graph(checkpointer, store)` 都要传两个对象。

### 3.3 为什么有 `_graph_registry`

```python
_graph_registry = {"kam_graph": None, "profile_graph": None, "store": None}
```

这是一个简单的“共享保管盒”。启动完成后，里面放入主图、画像子图、Store；路由函数通过 `get_kam_graph()`、`get_profile_graph()`、`get_store_instance()` 取出已经准备好的对象。若应用还没初始化就被调用，辅助函数会主动抛错，而不是让后续出现难懂的 `NoneType` 错误。

要注意：图节点运行时可通过 LangGraph 的 `get_store()` 取得 Store；FastAPI 路由函数不在这个运行时上下文中，所以 `/store/*` 路由必须直接使用 registry 里的 `store` 实例。

---

## 四、请求和响应模型：接口的“表单规则”

> 这一节是“一之五”里“牌子①②”的展开版，讲每个具体接口的表单字段；如果对 `BaseModel`/装饰器这些语法本身还不熟，先回看 1.5 节。

对应 `GenerateProfileRequest`、`ConfirmRequest` 等继承自 `BaseModel` 的类。

Pydantic 会把收到的 JSON 解析并校验成 Python 对象。例如：

```python
class GenerateProfileRequest(BaseModel):
    follow_user_id: str
    external_id: str
```

调用方必须传：

```json
{
  "follow_user_id": "follow_001",
  "external_id": "external_001"
}
```

少传必填字段或传入明显错误的类型时，FastAPI 会自动返回校验错误，而不需要接口函数手动逐项判断。

`response_model=GenerateProfileResponse` 则反过来约束“后端承诺返回什么”。这让前端能稳定地取 `thread_id`，也让接口文档和实际代码保持一致。

---

## 五、三个 AI 任务接口

### 5.1 `POST /tasks/generate_profile`：只创建任务号

对应 `generate_profile()`（第 184 行）。

请求：

```json
{
  "follow_user_id": "follow_001",
  "external_id": "external_001"
}
```

响应：

```json
{
  "thread_id": "thr_0a1b2c..."
}
```

核心实现是：

```python
thread_id = f"thr_{uuid.uuid4().hex}"
```

`uuid.uuid4().hex` 会生成几乎不会重复的随机标识。这个接口不查数据库、不调 LLM、不执行图；它只负责“发号”。因此它很快，并且为下一步 SSE 建连留下时间。

### 5.2 `GET /tasks/stream/{thread_id}`：建立 SSE 并真正执行图

对应 `stream_profile()`（第 267 行）和 `_stream_profile_events()`（第 228 行）。调用示例：

```text
GET /tasks/stream/thr_0a1b2c?follow_user_id=follow_001&external_id=external_001
```

SSE（Server-Sent Events，服务器发送事件）是一条保持开启的 HTTP 连接。普通 HTTP 是“问一次、答一次”；SSE 是“连一次，服务器连续推多条消息”。

接口用：

```python
return StreamingResponse(..., media_type="text/event-stream")
```

告诉浏览器响应不是一整段 JSON，而是事件流。辅助函数 `_format_sse_event()` 把数据拼成 SSE 规定的文本格式：

```text
event: profile_field_update
data: {"field":"project_stage", "value":"已立项", "confidence":8, ...}

```

`_stream_profile_events()` 里做了三件关键事：

1. 用主任务 `thread_id` 组成 `config`，让主图的 checkpoint 有自己的身份；
2. 组装初始 State，并调用 `graph.invoke(initial_state, config=config)`；
3. 从主图返回的 `profile_field_interrupts` 取出每个字段草稿，逐条 `yield` 为 `profile_field_update`，最后 `yield done`。

这里使用同步生成器的 `yield`：每 `yield` 一次，`StreamingResponse` 就可以向浏览器推一次，而不是等所有字段都拼完后一起返回。

当前架构下，不再从主图结果的 `__interrupt__` 读数据。字段子图里的 interrupt 已经由主图节点内部逐个收集并整理为普通字段 `profile_field_interrupts`；每条 SSE 事件还会带字段自己的子 `thread_id`，前端确认时必须回传它。

### 5.3 `POST /tasks/confirm`：恢复准确的暂停字段

对应 `confirm_field()`（第 284 行）。请求示例：

```json
{
  "thread_id": "thr_0a1b2c:project_stage",
  "follow_user_id": "follow_001",
  "external_id": "external_001",
  "field": "project_stage",
  "interrupt_id": "1234567890",
  "action": "ok"
}
```

字段说明：

| 字段 | 为什么需要 |
|---|---|
| `thread_id` | 定位这个字段所在的子图执行实例 |
| `interrupt_id` | 定位该实例中“这一次”精确暂停点；不能只凭字段名猜 |
| `action` | 用户选择：`ok`、`discard` 或 `recreate` |
| `field` | 用于后端二次校验和拼装响应，不是恢复的唯一凭据 |

先检查 action 是否在允许集合中；不合法时抛 `HTTPException(status_code=400)`，返回“客户端请求有问题”的 400 错误。

真正恢复图的是：

```python
graph.invoke(
    Command(resume={payload.interrupt_id: payload.action}),
    config={"configurable": {"thread_id": payload.thread_id}},
)
```

`Command(resume=...)` 可以理解成“把顾问的选择送回暂停在 `interrupt()` 的那一行”。它不是让整个任务从头再跑。

三种结果：

| action | 子图行为 | 接口响应 |
|---|---|---|
| `ok` | 写入确认后的画像字段 | `status: "confirmed"` |
| `discard` | 不采用该草稿 | `status: "discarded"` |
| `recreate` | 再生成该字段，并再次停在 interrupt | `status: "draft"`，同时返回新的草稿和新的 `interrupt_id` |

`recreate` 比前两种多一步：`_select_recreated_interrupt()` 会从返回结果的 `__interrupt__` 中找到同一字段的新中断。独立字段子图按设计应当只匹配一条；若不是恰好一条，代码返回 500 错误而不是悄悄选第一条，避免把错误草稿交给顾问。

---

## 六、三个通用 Store 接口

这三条接口对应第 388 行起的实现。它们不是直接给浏览器公开的业务页面接口，而是给内部 `kam_client` / `kam_admin` 使用的轻量数据通道。

### 6.1 为什么是通用接口

LangGraph Store 的基本操作就是：

```python
store.get(namespace, key)
store.put(namespace, key, value)
store.search(namespace, filter=None)
```

如果每个命名空间都单独写 get/put/search 路由，7 个命名空间至少会扩展成 21 条接口。本项目选择三条泛型接口，让调用方传 `namespace`、`key`、`value`、`filter`。

代价是接口层不逐个限制哪个命名空间可访问；这个取舍成立的前提是：它们只给内网的受控组件调用，不对公网开放。正式生产环境仍应补认证、授权和命名空间白名单。

### 6.2 `POST /store/get`

请求：

```json
{
  "namespace": ["external_user"],
  "key": "external_001"
}
```

响应（命中）：

```json
{
  "item": {
    "key": "external_001",
    "value": {"name": "陈女士"}
  }
}
```

未命中时返回 `{"item": null}`，这是正常业务结果，不应当把它当成服务器故障。

### 6.3 `POST /store/put`

请求：

```json
{
  "namespace": ["tags_setting"],
  "key": "tag_001",
  "value": {"name": "重点跟进", "category": "意向"}
}
```

成功响应：

```json
{"success": true}
```

### 6.4 `POST /store/search`

请求：

```json
{
  "namespace": ["employee"],
  "filter": {"region": "华东"},
  "limit": 20
}
```

响应：

```json
{
  "items": [
    {"key": "emp_001", "value": {"name": "小张", "region": "华东"}}
  ]
}
```

HTTP JSON 没有 Python 元组类型，所以前端传 `namespace` 数组；路由内部用 `tuple(payload.namespace)` 转换，因为 LangGraph Store 要求 namespace 是元组。

另外，Store 返回的是 `Item` Python 对象，不能直接作为 JSON 响应。`_item_to_dict()` 把它转换成只包含 `key` 和 `value` 的普通字典，再交给 FastAPI 序列化。

---

## 七、排查和复查清单

1. 服务启动即报“未设置 `KAM_POSTGRES_URL`”：先检查环境变量和连接串，不要先怀疑接口代码。
2. 图节点中 `get_store()` 得到 `None`：检查两个图的 `compile()` 是否都传入 `store`；FastAPI 路由本身不要调用 `get_store()`。
3. 点击生成后页面没有实时草稿：确认前端顺序是先 `generate_profile` 拿主 thread，再连 `/tasks/stream`；检查 SSE 响应的 `Content-Type` 是否为 `text/event-stream`。
4. 点击确认没有恢复正确字段：检查回传的是 SSE 事件里的**字段子 thread_id**和 `interrupt_id`，而不是主任务 thread_id 或字段名。
5. `recreate` 后没有新草稿：检查响应是否携带新的 `new_interrupt_id`；前端必须用新的 id 发下一次 confirm，旧 id 已经过期。
6. Store API 返回 JSON 序列化错误：检查是否遗漏 `_item_to_dict()`，不要直接返回 LangGraph 的 Item 对象。
7. Windows PowerShell 测试 POST：`curl` 在 PowerShell 中是别名，推荐使用 `Invoke-RestMethod -Method Post -ContentType "application/json" -Body '...'`。

---

## 八、面试时的一分钟讲法

“`webapp.py` 是 kam_agent 的 FastAPI 接口层，不把业务推理写在路由里，而是负责 HTTP、SSE 和 LangGraph 的衔接。启动时我创建并复用 PostgresSaver 和 PostgresStore：前者保存 interrupt 的可恢复执行状态，后者保存客户画像等业务数据。画像生成采用两阶段：POST 先创建 thread_id，前端连好 SSE 后 GET 流接口才真正 invoke 主图，避免 AI 先完成而前端还没订阅导致事件漏掉。图把每个字段拆成独立的子 thread，前端确认时带回该字段的 thread_id 和 interrupt_id，后端用 Command(resume=...) 精确恢复；重生成会返回新的草稿和新的 interrupt_id。除此之外，我提供了通用 Store get/put/search 接口供管理后台通过 kam_client 访问业务数据。”

---

## 九、相关文档与代码

- 宏观架构和接口契约：`设计文档/02-架构设计文档.md`
- kam_agent 目录、数据流和图节点说明：`设计文档/03-kam_agent目录结构与数据流.md`
- 实际接口实现：`kam_agent/src/webapp/webapp.py`
- 画像子图实现：`kam_agent/src/graphs/kam_graph/kam_sub_graph_profile/`

