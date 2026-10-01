# kam_agent — 目录结构与画像生成数据流

> 状态：开发实现（软件工程五步精简里程碑 · 第3步）
> 定稿日期：
> 用途：`kam_agent`组件动手写代码前的结构规划，配合 `02-架构设计文档.md` 使用（02号是宏观架构，本文档是`kam_agent`内部的具体组织方式）

---

## 一、画像生成数据流（手绘图文字版）

```
请求（哪个员工、哪个客户）
  │
  ▼
Store 加载数据（公用：加载7类数据——客户信息/画像/企微聊天/客服聊天/订单/标签定义/员工信息）
  │
  ▼
意图识别（公用：判断这次请求要走哪个任务）
  │
  ├──▶ 标签子图 ──▶ LLM（标签专属prompt）──▶ ...（二三期占位）
  │
  ├──▶ 回复建议子图 ──▶ LLM（回复建议专属prompt）──▶ ...（二三期占位）
  │
  └──▶ 画像子图（MVP核心）
         │
         ▼
       LLM（画像专属prompt，生成置信度五元组草稿）
         │
         ▼
       草稿存入 Store（status: draft / need_verify，confidence≤3自动标need_verify）
         │
         ▼
       SSE 返回用户（推给 kam_sidebar 展示）
         │
         ▼
       用户逐字段确认（ok / discard / recreate）
         │
         ▼
       修改 Store 中对应字段的 status（confirmed），已确认字段不被后续新草稿覆盖
```

**关键判断依据**：
- "Store加载数据"和"意图识别"两步是**三个子图共同的前置步骤**，不属于任何一个具体任务，所以代码位置是公用/入口层，不是画像子图的一部分。
- "LLM怎么连接"（客户端配置）是公用的，只有一份；但"问LLM什么内容"（prompt设计）是每个子图专属的业务逻辑，因为画像、标签、回复建议要问的问题完全不同。

---

## 二、目录结构

```
kam_agent/
├── main.py                      # 项目启动入口
│
├── src/
│   ├── store/                   # 【公用】Store连接封装——唯一连PostgreSQL/Redis的地方
│   │   └── store_client.py      #   get/put/search 三个基础方法，7个命名空间的读写都走这里
│   │
│   ├── llm/                     # 【公用底层 + 各子图专属】LLM调用
│   │   ├── llm_setting.py       #   公用：LLM客户端怎么连（API key/模型名/超时配置），只有一份
│   │   ├── llm_suggest_profile.py   #   画像子图专属：prompt怎么写、怎么问LLM生成画像草稿
│   │   ├── llm_suggest_tag.py       #   标签子图专属：prompt怎么写、怎么问LLM推荐标签（二三期占位）
│   │   └── llm_suggest_chat.py      #   回复建议子图专属（二三期占位）
│   │
│   ├── models/                  # 【公用】数据结构定义——比如画像五元组、员工/客户的字段结构
│   │   └── kam_models.py
│   │
│   ├── graphs/                  # 主图 + 各子图
│   │   └── kam_graph/
│   │       ├── kam_graph.py         # 主图定义：Load阶段 → 意图识别 → 分发
│   │       ├── kam_node.py          # 主图节点函数（意图识别的具体实现）
│   │       ├── kam_node_load_data.py  # "加载7类数据"这个动作单独一个文件
│   │       │
│   │       ├── kam_sub_graph_profile/   # 【画像子图专属，MVP核心】
│   │       │   ├── sub_profile_graph.py   # 子图定义：LLM生成草稿→interrupt等确认→写回Store
│   │       │   ├── sub_profile_node.py    # 子图节点函数
│   │       │   └── sub_profile_state.py   # 这个子图自己的状态（State）
│   │       │
│   │       ├── kam_sub_graph_tag/        # 【标签子图专属，二三期占位】
│   │       └── kam_sub_graph_chat_suggestion/  # 【回复建议子图专属，二三期占位】
│   │
│   ├── webapp/                  # 三个HTTP接口的入口层：generate_profile / stream / confirm
│   │   └── webapp.py            #   负责接请求、解析参数、调主图，不写业务逻辑本身
│   │
│   └── utils/                   # 【公用】日志、时间格式转换等零散工具函数
│
├── pyproject.toml               # 组件依赖清单
└── docker-compose.yml           # 最后部署阶段才需要，当前先本地跑，暂不写
```

## 三、判断一段代码该放哪个文件夹的原则

核心问题只有一个：**这段代码会被几个子任务用到？**

- 被所有子任务用到 → 放公用文件夹，只写一份，谁都能调用（如 `store/`、`llm_setting.py`、`models/`、Load数据、意图识别）
- 只被一个子任务用到 → 放这个子任务自己的文件夹（如 `kam_sub_graph_profile/` 整个文件夹只服务画像生成）

---

## 四、下一步

- 目录按主图、子图、模型和存储职责组织。
- Store 访问统一封装在 `store/store_client.py`。
- 主图负责业务路由，子图封装各能力，LLM 调用集中在 `llm/`。

---

## 五、代码落地后的真实调用链路（ 补，MVP核心链路已端到端验证通过）

> 本节是"代码写完之后"回头梳理的真实执行顺序，跟上面第一节"规划阶段的数据流图"对照着看——
> 整体结构完全一致，多出来的是 `webapp.py` 层具体怎么衔接 SSE、以及子图调用方式这类落地细节。

### 5.1 从"顾问点按钮"到"数据落库"，按代码真实执行顺序走一遍

**第一层：`webapp.py`（网络入口）**

程序启动时，`lifespan` 函数先执行：创建全局唯一的 `PostgresSaver`（管 `interrupt()` 冻结恢复）和 `PostgresStore`（管所有 Store 数据读写），两者进程运行期间只创建一次（全局单例+连接池，不是每次请求各建各的）。用这两个对象编译出画像子图（`build_profile_graph(checkpointer, store)`）和主图（`build_kam_graph(checkpointer, store)`），子图实例通过 `kam_node.set_profile_graph(profile_graph)` 注入给主图节点函数使用。

顾问点击生成画像按钮 → 前端调 `POST /tasks/generate_profile` → 这个接口**只生成一个 `thread_id` 字符串返回，不执行任何图逻辑**。前端拿着 `thread_id` 去连 `GET /tasks/stream/{thread_id}`（SSE），**这里才真正调用 `kam_graph.invoke(...)`，图从这一刻才开始跑**（这个"先发号、SSE连上才真正执行"的顺序是刻意设计的，为了避免"POST已经把图跑完、SSE还没连上导致事件被漏推"的竞态，见 `02-架构设计文档.md` 5.1.2节说明）。

**第二层：主图 `kam_graph.py`**

结构：`START → load_data → detect_intent → (条件分支route_by_intent) → call_profile_subgraph → END`。

- `load_data`（`kam_node_load_data.py`）：调用 `store_client.py` 里一批 `get_xxx`/`search_xxx` 函数，把这个客户相关的全部数据（基本信息、企微聊天记录、客服聊天记录、订单、已有画像）一次性从 PostgreSQL 查出来，塞进主图 State。这是三个子图共同的前置步骤，只查一次，后面不重复查库。
- `detect_intent`（`kam_node.py`）：MVP阶段固定返回 `{"intent": "profile"}`，写死走画像流程。
- `route_by_intent`（`kam_node.py`）：条件边函数，读 State 里的 `intent`，决定跳到哪个节点（现在只有一条路）。
- `call_profile_subgraph`（`kam_node.py`）：**这是"公用/规划文档没细讲"的一个关键实现细节**——它不是 LangGraph 官方推荐的"子图挂载"写法（`add_node("x", compiled_subgraph)`，靠字段名自动合并 State），而是在这个包装函数内部**手动**从主图 State 里挑出子图需要的字段组装成新字典，再调用 `_profile_graph.invoke(sub_input)`；子图跑完后，也手动从子图返回结果里只挑主图关心的 `field_updates` 字段传回去。选这种"手动映射"而不是"自动挂载"，是因为三个子图（画像/标签/回复建议）需要的数据、State结构差异很大，如果为了自动合并硬凑字段名，主图 State 会被撑成一个臃肿的大字典、子图之间互相耦合；手动映射多写几行代码，换来每个子图完全独立、互不影响。这种写法是否会导致 `interrupt()` 恢复时从头重跑（曾查到 LangGraph 官方 GitHub issue #4796 报告过类似问题），已用 `experiments/独立子图中断实验` 做最小复现实验验证过：不会重跑，只要子图自己 `compile()` 时也传了同一个 checkpointer 即可。

**第三层：画像子图 `sub_profile_graph.py`**

结构：`START → generate_all_drafts → (条件分支/Send扇出dispatch_fields_for_confirm) → confirm_one_field → END`。

- `generate_all_drafts`（`sub_profile_node.py`）：调用 `llm_suggest_profile.py` 的 `suggest_profile_draft`，把聊天记录/订单一次性喂给 LLM，生成全部字段的草稿（value/confidence/source），再对 `confidence<=3` 的字段打 `need_verify` 标记。
- `dispatch_fields_for_confirm`（`sub_profile_node.py`）：条件边，把草稿字典拆开，每个字段各生成一个 `Send` 任务，并行独立地跑 `confirm_one_field`（这是"一份数据拆成N份都要跑"的并行扇出，跟主图 `route_by_intent` 那种"三选一"的条件分支是两种不同用法，不要混淆）。
- `confirm_one_field`（`sub_profile_node.py`）：真正调用 `interrupt(...)` 的地方，调用后图在这里冻结、状态存进 PostgreSQL，`.invoke()` 函数返回，返回值的 `__interrupt__` 列表里每一项有 `.id`（即 `interrupt_id`）和 `.value`（要展示给前端的字段内容）。

**第四层：`webapp.py` 的 SSE 端点推送**

拿到 `invoke()` 结果后，循环 `__interrupt__` 列表，每一项拼成一条 `event: profile_field_update` 的 SSE 消息推给前端，全部推完再推一条 `event: done`。

**第五层：顾问确认，图从冻结点精确恢复**

前端展示字段草稿，顾问点确认/丢弃/重新生成，调 `POST /tasks/confirm`（带 `thread_id` + `interrupt_id` + `action`）→ `webapp.py` 调用 `graph.invoke(Command(resume={interrupt_id: action}), config=...)` → 图从 `interrupt()` 那一行**精确恢复**（不是从头重跑，已用真实数据验证：重复请求同一 `thread_id` 的 SSE 端点会瞬间返回、不重新执行 `load_data`/`generate_all_drafts`）→ `confirm_one_field` 按 `action` 三分支处理 → 调用 `store_client.py` 的 `upsert_external_user_profile` 写回 PostgreSQL。

### 5.2 一句话版数据流（文件名串联，方便快速回忆）

```
webapp.py（网络层，SSE入口）
  → kam_graph.py（主图：查数据+分流）
    → kam_node_load_data.py（查Store）
    → kam_node.py（意图识别 + 手动映射调用子图）
      → sub_profile_graph.py（子图结构定义）
        → sub_profile_node.py（生成草稿 / Send扇出 / interrupt确认，真正调LLM和interrupt的地方）
          → llm_suggest_profile.py（怎么问LLM）
          → store_client.py（怎么读写数据库，load_data和confirm_one_field两处都调用它）
            → kam_models.py（贯穿全程的数据结构，FieldStatus/ConfirmAction等）
```

### 5.3 为什么不用官方"子图挂载"机制（常见追问，面试可能被问到）

官方挂载写法是 `add_node("profile_subgraph", profile_graph)`，直接把编译好的子图对象注册成父图节点，LangGraph 会自动按**字段名重叠**把父子图 State 对齐传值，不用手写映射代码。

本项目没选这条路，是因为子图之间需要的数据差异太大——画像子图需要聊天记录原始数据、内部又会产生只有自己关心的中间字段（`profile_draft`），最终真正要交回给主图的只有一个 `field_updates`。如果指望"字段名自动对齐"，就必须让不同子图的 State 结构相互迁就、字段名统一，主图 State 会被三个子图共同撑成一个臃肿字典，子图之间会产生不必要的耦合（改一个子图的字段设计可能牵连其他子图）。

改用"手写包装节点"（`call_profile_subgraph`），在函数内部显式挑选"要传给子图哪些字段""子图返回后主图只关心哪个字段"，是用多写几行映射代码换子图之间完全独立、互不影响。这个选择已经用真实实验验证过技术上是可行的（不会导致 interrupt 恢复异常），不是"图省事选了个有隐患的方案"。
