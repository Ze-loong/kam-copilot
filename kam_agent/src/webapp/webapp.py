"""
kam_agent 对外接口层（对应 02 号架构文档 5.1 节）。

三个核心接口：
  POST /tasks/generate_profile     发起画像生成任务，只返回 thread_id，不执行图
  GET  /tasks/stream/{thread_id}   SSE，真正触发 graph.invoke()，逐字段推送草稿
  POST /tasks/confirm              顾问确认/丢弃/重新生成单个字段

执行时序设计：
  为什么 /tasks/generate_profile 里不直接调 invoke()？
  ——如果在这里就把图跑完，图会一路执行到 interrupt() 处冻结，
    但这时前端可能还没连上 SSE。等前端连上 /tasks/stream/{thread_id} 时，
    "图正在执行、正产生事件"这个过程已经结束了，SSE 端点这边没有
    "正在发生的执行"可以监听，只能去读已经落盘的中断状态——不是不能做，
    但会把"实时监听执行过程"这个简单模型复杂化。所以改成：
    先只发 thread_id，SSE 连上、响应流建立好之后，才真正触发 invoke()，
    从根上避免"POST 已跑完、SSE 还没连上、事件被漏掉"的竞态。

checkpointer 生命周期（ 定案）：
  全局单例 + 连接池。应用启动时（FastAPI lifespan）创建一次 PostgresSaver，
  同一个实例同时传给主图 build_kam_graph() 和子图 build_profile_graph()，
  所有请求共用。原因见实战记录：状态数据存在 PostgreSQL 里、不在进程内存，
  多请求共享一个 PostgresSaver 实例是安全的；真正的并发风险点是"同一
  thread_id 被并发写"，但本项目不同顾问操作不同客户天然是不同 thread_id，
  不会撞上。

interrupt_id 提取方式（ 重构后）：
  generate_all_field_drafts（一次 LLM 调用生成全部字段草稿）在主图
  call_profile_subgraph 节点内部执行；之后对每个字段各自发起一次独立
  thread_id 的画像子图 invoke()，每个子图执行实例只处理 1 个字段、
  只产生 1 条 interrupt。这些 interrupt 在 call_profile_subgraph 内部
  被收集进 "profile_field_interrupts" 这个普通返回字段（不再冒泡成主图
  这次 invoke() 自己的 "__interrupt__"），SSE 端点从这个字段读出列表，
  把每一项拆成一条 profile_field_update 事件，事件里带各自的 field_thread_id
  （前端后续 /tasks/confirm 要用这个子 thread_id，不是主 thread_id）。

  为什么要改成这样（recreate 多pending interrupt框架边界修复）：
  之前是子图内部一次 invoke 处理全部字段（Send 扇出多个并行分支，
  各自 interrupt，一起冒泡给主图）。这个结构下，如果只对其中一个字段
  recreate，该分支动态 Send 产生的新 interrupt 不会出现在当次 resume
  的 invoke() 返回值里——这是 LangGraph 在"同一子图执行实例内，多个
  interrupt 同时 pending、恢复其中一个又动态追加新 Send"这个组合场景下
  的真实框架边界（已用 experiments/repro_parallel_dynamic_interrupt.py 组D
  真实复现坐实，官方"挂载子图"方式同样会踩，不是手动invoke独有问题）。
  改成每个字段各自独立 thread_id 后，每个子图执行实例永远只有 1 个
  interrupt，不会触发这个边界，recreate 分支的 Send 循环逻辑不用改。
"""

import json
import hmac
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore
from langgraph.types import Command
from pydantic import BaseModel
import os
import uuid

# ：此前 .env 只在
# llm_setting.py 被导入时才会隐式生效（load_dotenv() 写在那个模块顶层），
# webapp.py 自己从未显式加载过——KAM_POSTGRES_URL 这类变量能不能读到
# 正确值，全靠"某个间接 import 链路里恰好先导入了 llm_setting.py"这个
# 偶然顺序，进程如果继承了同名的系统/shell环境变量，会在 lifespan 里
# 静默连错数据库（本次验证时的真实报错：Postgres 尝试连接到 "=disable"）。
# 这里显式调用一次，确保 webapp.py 作为应用入口自己能兜底把 .env 加载到位，
# 不依赖其他模块的导入顺序。override=True 是必要的：默认值不会覆盖进程继承的
# 同名变量，仍可能静默使用旧连接串；本项目以本地 .env 作为该服务的配置真源。
load_dotenv(override=True)

from src.graphs.kam_graph import kam_node
from src.graphs.kam_graph.kam_graph import build_kam_graph
from src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_graph import (
    build_chat_suggestion_graph,
)
from src.graphs.kam_graph.kam_sub_graph_kb.sub_kb_graph import build_kb_graph
from src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_graph import (
    build_kf_chat_suggestion_graph,
)
from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_graph import build_profile_graph
from src.graphs.kam_graph.kam_sub_graph_reasoning.sub_reasoning_graph import build_reasoning_graph
from src.graphs.kam_graph.kam_sub_graph_tag.sub_tag_graph import build_tag_graph
from src.kb.kb_ingest import get_document_content, ingest_document, list_documents
from src.models.kam_models import ConfirmAction
from src.store.store_client import (
    get_external_user_profile,
    get_reasoning_enabled,
    get_tags_setting,
    search_external_user,
    search_wxkf_msg,
    search_wxqy_msg,
)

# ---------------------------------------------------------------------------
# 应用启动/关闭生命周期：全局单例 checkpointer 在这里创建，进程运行期间只建一次。
# ---------------------------------------------------------------------------

# 用一个简单的容器持有编译好的图，供各接口函数使用。
# （不用模块级裸变量是因为要等 lifespan 里连接池就绪后才能编译图，
#  用 dict 包一层方便在闭包/函数间读写同一份引用。）
#  新增 "profile_graph"：/tasks/confirm 现在要直接对画像子图
# resume（每个字段是独立的子图执行实例），不再通过主图转发，所以 webapp.py
# 自己也要持有一份子图引用，不能只注入进 kam_node 模块里。
#  "store"：kam_client 要走 /store/get /put /search 三个
# 通用接口读写数据（详见文件末尾说明），这几个接口直接复用 lifespan 里已经建好的
# PostgresStore 实例，不走 get_store()——get_store() 只在"图节点执行的运行时
# 上下文"里才能工作，FastAPI 路由函数不在这个上下文里，直接用同一个 store 实例即可。
_graph_registry: dict = {
    "kam_graph": None,
    "profile_graph": None,
    "tag_graph": None,
    "kb_graph": None,
    "reasoning_graph": None,
    "store": None,
}
# ：reasoning_graph 单独存一份到
# _graph_registry——跟profile_graph/tag_graph一样，是因为
# /tasks/stream_reasoning/{thread_id} 需要直接对它调用
# .stream(..., stream_mode="custom")，不能像同步接口那样只经由
# kam_node.py的包装节点走主图（主图节点内部是"手动invoke"整个子图一次
# 拿到最终结果，没法把子图内部逐步产生的自定义流事件转发出来）。
# ：chat_suggestion/kf_chat_suggestion 两个子图没有
# interrupt，webapp.py 的接口不需要像 profile_graph/tag_graph 那样直接持有
# 引用去 resume——它们只经由 kam_node.py 的包装节点被主图间接调用，所以不
# 需要在 _graph_registry 里也存一份，只需要注入给 kam_node 模块。


@asynccontextmanager
async def lifespan(app: FastAPI):
    connection_uri = os.environ.get("KAM_POSTGRES_URL")
    if not connection_uri:
        raise RuntimeError("未设置 KAM_POSTGRES_URL，webapp 无法启动")

    # PostgresSaver 管 interrupt()/checkpoint（图执行到哪一步的状态）；
    # PostgresStore 管 store_client.py 里 get_store() 依赖的业务数据读写
    # （员工/客户/聊天记录/画像等7个命名空间）。这是 compile() 的两个独立参数，
    # 都要传，否则 load_data 等节点内部 get_store() 拿到 None 直接崩
    # （ 端到端联调时的真实报错，见 实战记录.md 当天条目）。
    # 都用 with 管理生命周期，保证进程退出时连接池被正确关闭。
    with PostgresSaver.from_conn_string(connection_uri) as checkpointer, \
         PostgresStore.from_conn_string(connection_uri) as store:
        checkpointer.setup()
        store.setup()

        # 主图子图必须共用同一个 checkpointer 实例（见文件头部说明 +
        # experiments/独立子图中断实验 的真实验证结论），
        # store 同理也共用同一个实例。标签子图（ F04接线）遵循
        # 同一条约定，和画像子图一样需要单独注入给 kam_node 模块。
        profile_graph = build_profile_graph(checkpointer, store)
        kam_node.set_profile_graph(profile_graph)  # 注入给 call_profile_subgraph 用
        _graph_registry["profile_graph"] = profile_graph  # /tasks/confirm 直接用

        tag_graph = build_tag_graph(checkpointer, store)
        kam_node.set_tag_graph(tag_graph)  # 注入给 call_tag_subgraph 用
        _graph_registry["tag_graph"] = tag_graph  # /tasks/confirm_tags 直接用

        # ：这两个子图没有interrupt，webapp.py 自己
        # 不需要单独持有引用去 resume——只注入给 kam_node 模块，由主图的
        # 包装节点 call_chat_suggestion_subgraph/call_kf_chat_suggestion_subgraph
        # 间接调用即可，与 profile_graph/tag_graph 的注入方式一致，只是不需要
        # 再存进 _graph_registry。
        chat_suggestion_graph = build_chat_suggestion_graph(checkpointer, store)
        kam_node.set_chat_suggestion_graph(chat_suggestion_graph)

        kf_chat_suggestion_graph = build_kf_chat_suggestion_graph(checkpointer, store)
        kam_node.set_kf_chat_suggestion_graph(kf_chat_suggestion_graph)

        # ：知识库问答子图同样没有interrupt，与
        # chat_suggestion/kf_chat_suggestion走同一套注入方式。
        # ：同时也存进_graph_registry——F17流式接口关停
        # 开关生效时要退回F16，需要直接.invoke()它（不能裸调answer_from_kb()
        # 这类函数，它们内部依赖get_store()，只能在图节点真实执行的上下文
        # 里工作，见下方get_kb_graph()的使用处）。
        kb_graph = build_kb_graph(checkpointer, store)
        kam_node.set_kb_graph(kb_graph)
        _graph_registry["kb_graph"] = kb_graph

        # ：F17不走主图/intent路由（见kam_node.py route_by_intent
        # 的说明），webapp.py直接持有reasoning_graph，/tasks/stream_reasoning
        # 需要直接对它调用.stream(..., stream_mode="custom")。
        _graph_registry["reasoning_graph"] = build_reasoning_graph(checkpointer, store)

        _graph_registry["kam_graph"] = build_kam_graph(checkpointer, store)
        _graph_registry["store"] = store  # /store/* 通用接口直接用

        yield  # 应用运行期间，_graph_registry["kam_graph"] 保持可用

        # with 块结束时连接池自动关闭


app = FastAPI(lifespan=lifespan)


def get_kam_graph():
    graph = _graph_registry["kam_graph"]
    if graph is None:
        raise RuntimeError("kam_graph 尚未初始化，lifespan 是否正确执行？")
    return graph


def get_profile_graph():
    graph = _graph_registry["profile_graph"]
    if graph is None:
        raise RuntimeError("profile_graph 尚未初始化，lifespan 是否正确执行？")
    return graph


def get_tag_graph():
    graph = _graph_registry["tag_graph"]
    if graph is None:
        raise RuntimeError("tag_graph 尚未初始化，lifespan 是否正确执行？")
    return graph


def get_reasoning_graph():
    graph = _graph_registry["reasoning_graph"]
    if graph is None:
        raise RuntimeError("reasoning_graph 尚未初始化，lifespan 是否正确执行？")
    return graph


def get_kb_graph():
    graph = _graph_registry["kb_graph"]
    if graph is None:
        raise RuntimeError("kb_graph 尚未初始化，lifespan 是否正确执行？")
    return graph


def get_store_instance():
    store = _graph_registry["store"]
    if store is None:
        raise RuntimeError("store 尚未初始化，lifespan 是否正确执行？")
    return store


# ---------------------------------------------------------------------------
# 请求/响应体模型（对应 02 号文档 5.1.1 / 5.1.3 节）
# ---------------------------------------------------------------------------
# 请求体
class GenerateProfileRequest(BaseModel):
    follow_user_id: str
    external_id: str

# 响应体
class GenerateProfileResponse(BaseModel):
    thread_id: str


class ConfirmRequest(BaseModel):
    #  语义变更：不再是主任务的 thread_id，而是这个字段自己的
    # 子 thread_id（格式 f"{主thread_id}:{field_name}"，SSE 事件里的
    # "thread_id" 字段透传过来的那个值）——因为现在每个字段各自是独立的
    # 子图执行实例，Command(resume=...) 必须配合正确的子 thread_id 才能
    # 定位到具体是哪个字段在等待恢复。
    thread_id: str
    follow_user_id: str
    external_id: str
    field: str               # 仅用于后端二次校验，精确恢复凭据是 interrupt_id
    interrupt_id: str
    action: str               # ok / discard / recreate


class ConfirmResponse(BaseModel):
    success: bool
    field: str
    status: str
    # recreate 分支专属：新一轮 interrupt 的内容，直接在这次响应里带回新草稿，
    # 不再依赖前端重新连 SSE（SSE 只负责首次 generate_profile 后的推送）。
    # OK/DISCARD 分支这个字段固定为 None。
    new_interrupt_id: str | None = None
    new_value: str | None = None
    new_confidence: int | None = None
    new_source: str | None = None


# ---------------------------------------------------------------------------
# POST /tasks/generate_profile —— 只发 thread_id，不执行图
# ---------------------------------------------------------------------------

@app.post("/tasks/generate_profile", response_model=GenerateProfileResponse)
def generate_profile(payload: GenerateProfileRequest):
    # thread_id 只是这次画像生成会话的身份标识，用来给 SSE 端点、
    # /tasks/confirm 后续找到"同一次图执行"，跟 follow_user_id/external_id
    # 一起决定"要不要真的开始跑图"是 SSE 端点的职责，这里只管发号。
    thread_id = f"thr_{uuid.uuid4().hex}"
    return GenerateProfileResponse(thread_id=thread_id)


# ---------------------------------------------------------------------------
# GET /tasks/stream/{thread_id} —— SSE，这里才真正触发 graph.invoke()
# ---------------------------------------------------------------------------

def _format_sse_event(event: str, data: dict) -> str:
    """按 SSE 协议格式拼一条消息：event: xxx\\ndata: {...}\\n\\n"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _select_recreated_interrupt(result: dict, field: str):
    """
    从这次子图 resume 的 __interrupt__ 里取出重新生成后的中断。

     重构后，每个字段各自是独立的子图执行实例（独立 thread_id），
    这里的 result 只可能来自"这一个字段自己的子图"，__interrupt__ 正常应该
    恰好 1 条。仍然按 field 校验（而不是直接取索引0）是故意保留的防御性
    断言——如果真的匹配到 0 条或 >1 条，说明子图结构或 State 传递出了
    非预期的问题，直接报错比静默兜底更安全，不能让错误被掩盖过去。
    """
    matches = [
        item
        for item in result.get("__interrupt__", ())
        if isinstance(item.value, dict) and item.value.get("field") == field
    ]
    if len(matches) != 1:
        raise HTTPException(
            status_code=500,
            detail=(
                f"recreate 恢复后字段 {field!r} 应恰好产生 1 条新 interrupt，"
                f"实际匹配到 {len(matches)} 条——子图独立 thread 的前提下"
                f"这不应该发生，需要人工排查"
            ),
        )
    return matches[0]


def _stream_profile_events(thread_id: str, follow_user_id: str, external_id: str):
    """
    生成器函数：真正触发主图执行，把每个字段各自子图 invoke 产生的 interrupt
    拆成一条 SSE 事件。

    这是一个同步生成器（用 yield 而不是 return），FastAPI 的 StreamingResponse
    会不断从这个生成器里取值、每取到一条就往前端推一条，直到生成器结束，
    这就是"服务器主动、持续推送多条消息"在代码层面的样子。

     重构：主图不再自己产生 __interrupt__（call_profile_subgraph 内部
    循环对每个字段各自 invoke 独立子 thread，interrupt 已经在这一步被内部消化、
    折叠进 profile_field_interrupts 这个普通返回字段里，不会再冒泡成主图这次
    invoke() 的 __interrupt__）。所以这里改成读 result["profile_field_interrupts"]，
    不再读 result["__interrupt__"]。每条事件自带它自己字段的子 thread_id，
    前端后续 /tasks/confirm 要用这个子 thread_id（不是这里的主 thread_id）。
    """
    graph = get_kam_graph()
    # 主 thread_id 既要传给 config（供主图自己的 checkpointer 定位），也要放进
    # 初始 State 一份（供 call_profile_subgraph 节点函数内部读取，节点函数拿不到
    # config，只能从 State 里读，见 kam_graph.py MainState._main_thread_id 的说明）。
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "_main_thread_id": thread_id,
        "follow_user_id": follow_user_id,
        "external_id": external_id,
    }
    result = graph.invoke(initial_state, config=config)

    field_interrupts = result.get("profile_field_interrupts", [])
    for event_data in field_interrupts:
        # event_data 已经在 kam_node.py 里组装好：
        # {"field", "value", "confidence", "source", "status", "interrupt_id", "thread_id"}
        yield _format_sse_event("profile_field_update", event_data)

    yield _format_sse_event("done", {"thread_id": thread_id})

@app.get("/tasks/stream/{thread_id}")
def stream_profile(thread_id: str, follow_user_id: str, external_id: str):
    # follow_user_id/external_id 通过查询参数传（如 ?follow_user_id=u001&external_id=ext_8891），
    # 因为 SSE 是 GET 请求，不能像 POST 一样带 JSON body。
    return StreamingResponse(
        _stream_profile_events(thread_id, follow_user_id, external_id),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# POST /tasks/confirm —— 只负责接收确认动作、触发 resume，不返回新草稿
# ---------------------------------------------------------------------------

_VALID_ACTIONS = {ConfirmAction.OK, ConfirmAction.DISCARD, ConfirmAction.RECREATE}


@app.post("/tasks/confirm", response_model=ConfirmResponse)
def confirm_field(payload: ConfirmRequest):
    if payload.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的 action: {payload.action}")

    #  重构：resume 目标从主图换成画像子图——每个字段现在是独立的
    # 子图执行实例，payload.thread_id 传的是这个字段自己的子 thread_id
    # （格式 f"{主thread_id}:{field_name}"，来自 SSE 事件里的 thread_id），
    # 不再是主任务的 thread_id。主图在 call_profile_subgraph 里已经把
    # interrupt 消化处理完，不会再有内容等主图去 resume。
    graph = get_profile_graph()
    config = {"configurable": {"thread_id": payload.thread_id}}

    # 精确恢复：resume 的 key 必须是 interrupt_id，不能用 field 名反查
    # （见 02 号文档 5.1.2 节说明）。
    result = graph.invoke(
        Command(resume={payload.interrupt_id: payload.action}),
        config=config,
    )

    if payload.action == ConfirmAction.RECREATE:
        # recreate 分支：confirm_one_field 内部直接 goto regenerate_one_field，
        # 不写 Store、不产生终态。这次 invoke(Command(resume=...)) 会一路跑到
        # regenerate_one_field → 新草稿 → 新一轮 confirm_one_field 里的
        # interrupt()，子图再次冻结、返回值的 __interrupt__ 里就是这次新草稿。
        # 这个子图执行实例从头到尾只有这一个字段，__interrupt__ 正常应恰好
        # 1 条；_select_recreated_interrupt 仍按 field 校验作为防御性断言。
        new_interrupt = _select_recreated_interrupt(result, payload.field)
        new_value_data = dict(new_interrupt.value)

        return ConfirmResponse(
            success=True,
            field=payload.field,
            status="draft",  # 新草稿还没被确认，回到待确认状态，不是终态
            new_interrupt_id=new_interrupt.id,
            new_value=new_value_data.get("value"),
            new_confidence=new_value_data.get("confidence"),
            new_source=new_value_data.get("source"),
        )

    # OK / DISCARD：confirm_one_field 已经写完 Store 并返回终态，
    # 这里只是把同样的映射关系用于组装响应体，不重复业务判断。
    status_map = {
        ConfirmAction.OK: "confirmed",
        ConfirmAction.DISCARD: "discarded",
    }

    return ConfirmResponse(
        success=True,
        field=payload.field,
        status=status_map[payload.action],
    )


# ---------------------------------------------------------------------------
# 标签推荐三件套（F04， 新增）
#
# 08号设计文档判断点：为什么不复用 /tasks/generate_profile + /tasks/confirm，
# 而是独立开一组接口——ConfirmRequest 是"单字段+action"形状（ok/discard/
# recreate），标签确认提交的是"两个id列表"，形状完全不同，硬塞进同一个
# 请求体模型只会让 Pydantic 模型和分支判断变得别扭。三个接口分别对应画像
# 三件套的角色，但内部更简单：SSE 只推一条整批事件（不是循环推多条字段），
# confirm 不区分 ok/discard/recreate（没有 recreate，标签只有"勾/不勾"）。
# ---------------------------------------------------------------------------


class GenerateTagsRequest(BaseModel):
    follow_user_id: str
    external_id: str


class GenerateTagsResponse(BaseModel):
    thread_id: str


@app.post("/tasks/generate_tags", response_model=GenerateTagsResponse)
def generate_tags(payload: GenerateTagsRequest):
    """只发 thread_id，不执行图——与 /tasks/generate_profile 同样的时序设计
    （先发号，等 SSE 真正连上再触发 invoke，避免"POST已跑完、SSE还没连上、
    事件被漏掉"的竞态，见文件头部 /tasks/generate_profile 的说明）。"""
    thread_id = f"thr_tag_{uuid.uuid4().hex}"
    return GenerateTagsResponse(thread_id=thread_id)


def _stream_tag_events(thread_id: str, follow_user_id: str, external_id: str):
    """
    生成器函数：触发主图执行（intent="tag"），把标签子图整批 invoke 产生的
    那一条 interrupt 拆成一条 SSE 事件。

    与 _stream_profile_events 的关键区别：只推 1 条 tag_batch_update 事件
    （不是循环推多条 profile_field_update），因为标签子图本来就只invoke一次、
    只产生一条interrupt（整批推荐打包在一起），不是"每个字段各自一条"。
    """
    graph = get_kam_graph()
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "_main_thread_id": thread_id,
        "follow_user_id": follow_user_id,
        "external_id": external_id,
        # 显式指定 intent="tag"——detect_intent 节点会直接透传这个值，不需要
        # 再去猜（见 kam_node.py  的修正说明）。
        "intent": "tag",
    }
    result = graph.invoke(initial_state, config=config)

    tag_interrupt = result.get("tag_interrupt")
    if tag_interrupt:
        # tag_interrupt 已经在 kam_node.py 里组装好：
        # {"add_recommendations", "remove_recommendations", "interrupt_id", "thread_id"}
        yield _format_sse_event("tag_batch_update", tag_interrupt)

    yield _format_sse_event("done", {"thread_id": thread_id})


@app.get("/tasks/stream_tags/{thread_id}")
def stream_tags(thread_id: str, follow_user_id: str, external_id: str):
    return StreamingResponse(
        _stream_tag_events(thread_id, follow_user_id, external_id),
        media_type="text/event-stream",
    )


class ConfirmTagsRequest(BaseModel):
    # 与画像流程的 ConfirmRequest 语义类似：thread_id 是这次标签推荐任务
    # 自己的 thread_id（SSE 事件里 tag_batch_update.thread_id 透传过来的
    # 那个值）。 真实联调排查更正：这里**不是**主图自己的
    # main_thread_id，而是 kam_node.py 的 call_tag_subgraph 拼出来的
    # f"{main_thread_id}:tag"——标签子图和主图共用同一个thread_id会导致
    # checkpoint互相覆盖（详见 实战记录.md 当天条目），所以标签子图必须
    # 有自己独立的thread_id，只是不需要像画像子图那样再按字段继续拆分。
    thread_id: str
    follow_user_id: str
    external_id: str
    interrupt_id: str
    # 前端已经根据勾选框状态整理好的最终结果，不是原始勾选框dict——
    # 职责划分见 实战记录.md  "确认机制的职责划分"条目：前端负责
    # "用户选了什么"，子图负责"这个选择能不能执行"。后端仍会用
    # _validate_confirmed_ids 二次校验，不盲信这里传来的id列表。
    confirmed_add_tag_ids: list[str] = []
    confirmed_remove_tag_ids: list[str] = []


class ConfirmTagsResponse(BaseModel):
    success: bool
    confirmed_add_tag_ids: list[str]
    confirmed_remove_tag_ids: list[str]


@app.post("/tasks/confirm_tags", response_model=ConfirmTagsResponse)
def confirm_tags(payload: ConfirmTagsRequest):
    """
    提交标签批量确认结果，resume 标签子图。

    与 /tasks/confirm 的关键区别：resume 的 value 是一个 dict（两个id列表），
    不是单个 "ok"/"discard"/"recreate" 字符串；没有 action 校验、没有
    recreate 分支需要处理——confirm_tag_batch 这一次 resume 后就直接写 Store
    并 return 终态，不会再产生新的 interrupt。

    payload.thread_id 这里不能是主图自己的 thread_id，必须是 kam_node.py
    的 call_tag_subgraph 里拼出来的 f"{main_thread_id}:tag"（
    真实联调排查出的坑：如果标签子图和主图共用同一个 thread_id，两者的
    checkpoint 会写在同一个位置互相覆盖，resume 时读到的 add_recommendations
    会是空的）。这里不需要额外处理——只要前端老实把 SSE 事件里的 thread_id
    原样传回来就是对的，这个字段的值从源头（tag_interrupt["thread_id"]）
    就已经是正确的衍生id了。
    """
    graph = get_tag_graph()
    config = {"configurable": {"thread_id": payload.thread_id}}

    result = graph.invoke(
        Command(
            resume={
                payload.interrupt_id: {
                    "confirmed_add_tag_ids": payload.confirmed_add_tag_ids,
                    "confirmed_remove_tag_ids": payload.confirmed_remove_tag_ids,
                }
            }
        ),
        config=config,
    )

    return ConfirmTagsResponse(
        success=True,
        confirmed_add_tag_ids=result.get("confirmed_add_tag_ids", []),
        confirmed_remove_tag_ids=result.get("confirmed_remove_tag_ids", []),
    )


# ---------------------------------------------------------------------------
# 回复建议两个接口（F02/F03， 新增）
#
# 与画像/标签三件套的关键区别：没有interrupt，不需要"先发thread_id、等SSE
# 连上再invoke"这套时序设计（那套是为了让interrupt冻结前SSE已经连好、不
# 漏事件），也不需要单独的/confirm接口——子图invoke()一次跑完直接是终态，
# 一个同步POST接口足够：前端调用→后端invoke()→直接返回结果，请求-响应
# 语义就是完整的，不需要拆成"发号/流式/确认"三段（见09号设计文档判断点4）。
# 两个接口分别对应F02.1（销售）/F02.2（客服），路由到主图不同的intent
# （详见 09号设计文档 判断点3：意图区分交给接口层，不靠LLM猜或角色判断）。
# ---------------------------------------------------------------------------


class ChatSuggestionRequest(BaseModel):
    follow_user_id: str
    external_id: str


class ChatSuggestionResponse(BaseModel):
    suggestion_text: str
    reasoning: str


def _store_values(items: list[Any]) -> list[dict]:
    """把 LangGraph Store 搜索结果转换成可序列化的业务字典。"""
    return [dict(item.value) for item in items]


def _normalize_chat_messages(
    items: list[Any], channel: str, follow_user_id: str
) -> list[dict]:
    """统一企微销售和微信客服两种消息结构，并按消息时间正序返回。"""
    messages = []
    for value in _store_values(items):
        if channel == "wxqy_msg":
            sender = "advisor" if value.get("from_id") == follow_user_id else "customer"
        else:
            sender = "advisor" if value.get("origin") == "staff" else "customer"
        messages.append(
            {
                "content": value.get("content", ""),
                "msg_time": value.get("msg_time", ""),
                "sender": sender,
            }
        )
    return sorted(messages, key=lambda message: message["msg_time"])


@app.get("/tasks/customers")
def list_customers(follow_user_id: str):
    """返回当前顾问名下的客户，供聊天记录查看器左栏展示。附带 has_profile
    标记（）：并非所有客户都已经生成过画像（生成是顾问在
    "客户画像"tab里手动触发的按需操作，不是自动预置的），前端需要一眼
    看出区别，不用逐个点开才知道，所以在这里多查一次每个客户的画像是否
    存在。同时把 `tags` 从原始 tag_id 列表解析成 {tag_id, tag_name} 列表——
    `external_user.tags` 底层存的是id（`confirm_tag_batch` 的设计，见
    `sub_tag_node.py`，避免标签改名要回改每个客户记录），kam_admin 那边
    是通过 kam_client 联表查出名字显示的，但 kam_sidebar 这条链路之前没
    做这一步解析，前端拿到的就是一串没法读的id字符串。"""
    store = get_store_instance()
    items = search_external_user(follow_user_id, store=store)
    customers = _store_values(items)
    for customer in customers:
        profile = get_external_user_profile(follow_user_id, customer["external_id"], store=store)
        customer["has_profile"] = profile is not None

        resolved_tags = []
        for tag_id in customer.get("tags", []):
            tag_setting = get_tags_setting(tag_id, store=store)
            tag_name = tag_setting.value["tag_name"] if tag_setting else tag_id
            resolved_tags.append({"tag_id": tag_id, "tag_name": tag_name})
        customer["tags"] = resolved_tags
    customers.sort(key=lambda customer: (customer.get("remark_name") or customer.get("name") or ""))
    return {"customers": customers}


@app.get("/tasks/chat_history")
def get_chat_history(follow_user_id: str, external_id: str, channel: str):
    """读取指定客户聊天记录，并隐藏底层命名空间的字段差异。"""
    store = get_store_instance()
    if channel == "wxqy_msg":
        items = search_wxqy_msg(follow_user_id, external_id, store=store)
    elif channel == "wxkf_msg":
        items = search_wxkf_msg(external_id, store=store)
    else:
        raise HTTPException(status_code=400, detail="channel 必须是 wxqy_msg 或 wxkf_msg")
    return {
        "messages": _normalize_chat_messages(items, channel, follow_user_id),
    }


def _run_task_result_intent(
    follow_user_id: str,
    external_id: str,
    intent: str,
    extra_state: dict | None = None,
) -> dict:
    """
    公共逻辑：invoke主图（指定intent），从结果里取出task_result。

    与画像/标签流程共用同一个主图 build_kam_graph()——这几个分支只是
    route_by_intent 多出来的路径，不需要另建一个主图实例。thread_id
    这里用一次性的（不需要像画像/标签那样保留给后续/confirm接口复用，
    因为没有下一步要恢复的动作），用完即弃。

     extra_state 参数：F02/F03 不需要额外传入
    任何数据（画像/聊天记录/订单都由 load_data 节点自己查），但 F16
    需要把客户的问题文本（question）一起放进初始 State——这个字段没有
    节点会替调用方生成，必须由发起请求的一方显式提供，与 intent 的
    传入方式一致。extra_state 默认 None，不传就是原来的行为，F02/F03
    两个既有调用方不用改。
    """
    graph = get_kam_graph()
    thread_id = f"thr_chat_{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "_main_thread_id": thread_id,
        "follow_user_id": follow_user_id,
        "external_id": external_id,
        "intent": intent,
    }
    if extra_state:
        initial_state.update(extra_state)
    result = graph.invoke(initial_state, config=config)

    task_result = result.get("task_result")
    if not task_result:
        raise HTTPException(
            status_code=500,
            detail=f"intent={intent!r} 的子图未产生 task_result，需要人工排查",
        )
    return task_result


@app.post("/tasks/generate_chat_suggestion", response_model=ChatSuggestionResponse)
def generate_chat_suggestion_endpoint(payload: ChatSuggestionRequest):
    """F02.1 销售聊天回复建议：同步返回，不走SSE。"""
    task_result = _run_task_result_intent(
        payload.follow_user_id, payload.external_id, intent="chat_suggestion"
    )
    return ChatSuggestionResponse(
        suggestion_text=task_result["suggestion_text"],
        reasoning=task_result["reasoning"],
    )


@app.post("/tasks/generate_kf_chat_suggestion", response_model=ChatSuggestionResponse)
def generate_kf_chat_suggestion_endpoint(payload: ChatSuggestionRequest):
    """F02.2 客服聊天回复建议：同步返回，不走SSE。"""
    task_result = _run_task_result_intent(
        payload.follow_user_id, payload.external_id, intent="kf_chat_suggestion"
    )
    return ChatSuggestionResponse(
        suggestion_text=task_result["suggestion_text"],
        reasoning=task_result["reasoning"],
    )


# ---------------------------------------------------------------------------
# 知识库问答接口（F16， 新增）
#
# 结构与F02/F03同一类（02号架构文档342行：F16复用kam_sidebar现有任务型
# 接口模式，无需新接口类型）——没有interrupt，一次同步POST拿到最终结果，
# 不需要SSE/确认两段式。与F02/F03的唯一实质区别是入参多了question：
# F16不看客户历史数据（不依赖load_data产出的画像/聊天记录/订单），只需要
# 客户这次问的问题文本本身，见_run_task_result_intent的extra_state参数
# 和 kam_node.py 的 call_kb_subgraph 说明。
# ---------------------------------------------------------------------------


class KbAnswerRequest(BaseModel):
    follow_user_id: str
    external_id: str
    question: str


class KbAnswerResponse(BaseModel):
    answer_text: str
    cited_sources: list[str]


@app.post("/tasks/generate_kb_answer", response_model=KbAnswerResponse)
def generate_kb_answer_endpoint(payload: KbAnswerRequest):
    """F16 知识库问答：同步返回，不走SSE。"""
    task_result = _run_task_result_intent(
        payload.follow_user_id,
        payload.external_id,
        intent="knowledge_base",
        extra_state={"question": payload.question},
    )
    return KbAnswerResponse(
        answer_text=task_result["answer_text"],
        cited_sources=task_result["cited_sources"],
    )


# ---------------------------------------------------------------------------
# 跨模块综合推理接口（F17， 新增， 改为真实步骤透明推送）
#
# **说明**：最初这里是一个同步POST接口（跟F16同一类，一次
# 请求等最终结果）。但F17真实耗时约20秒（2次LLM调用+多步查询），顾问
# 全程盯着一个空白等20秒的体验很差，架构文档7.2节本来就设想了"步骤透明
# 展示"（如"正在查询客户画像…正在检索产品方案…"）。改成跟F01同款的"两段式"：
# `/tasks/generate_reasoning`只发号不执行，`/tasks/stream_reasoning/{id}`
# 这个SSE端点里才真正用`reasoning_graph.stream(..., stream_mode="custom")`
# 触发执行——`sub_reasoning_node.py`的三个节点函数内部用`get_stream_writer()`
# 在真实计算发生的当下就把进度消息写出去，不是等全部算完再假装分批推送
# （这是本项目第一次意识到：F01原来的SSE"实时推送"其实也是"全部算完→
# 一次性倒出一串消息"，不是真正的边算边推——F17这次改成用官方custom
# stream mode，是真正验证过`stream_mode="custom"`会按实际耗时逐条产出的，
# 见`experiments/流式推送实验`）。
#
# 全局关停开关（架构文档7.5节）：开启时走完整Plan-and-Execute流程；关闭时
# 直接退回F16知识库查询这条简化模式，通过一条"kb_fallback"事件推送结果，
# 不再进入`reasoning_graph`（对应架构文档"验证关闭后确实退回简化模式"）。
# ---------------------------------------------------------------------------


class ReasoningRequest(BaseModel):
    follow_user_id: str
    external_id: str
    question: str


class GenerateReasoningResponse(BaseModel):
    thread_id: str


@app.post("/tasks/generate_reasoning", response_model=GenerateReasoningResponse)
def generate_reasoning(payload: ReasoningRequest):
    """只发thread_id，不执行——真正的执行留到SSE连上之后，理由与
    /tasks/generate_profile完全一致（避免"POST已跑完、SSE还没连上、
    事件被漏掉"的竞态，见本文件头部/tasks/generate_profile的说明）。
    question在这里就地存进请求体，SSE端点会原样通过query string传回来
    （跟follow_user_id/external_id的传递方式一致，不需要额外的服务端
    临时存储）。
    """
    thread_id = f"thr_reasoning_{uuid.uuid4().hex}"
    return GenerateReasoningResponse(thread_id=thread_id)


def _stream_reasoning_events(thread_id: str, follow_user_id: str, external_id: str, question: str):
    """生成器函数：真正触发F17执行，把节点内部get_stream_writer()写出的
    自定义消息逐条转成SSE事件推给前端。

    关停开关判断放在这里（而不是像同步版那样在kam_node.py的
    call_reasoning_subgraph里）——因为这条路径不再经过主图/intent路由，
    是webapp.py直接对reasoning_graph操作，两处判断逻辑不复用没有关系，
    都是"查一次Store里的开关状态"这几行代码，重复成本很低，没必要为了
    避免这点重复而绕一圈把主图也牵扯进这个流式接口里。

    **真实调试发现的bug**：这个生成器函数运行在普通FastAPI
    请求处理流程里，不是LangGraph节点，`get_reasoning_enabled()`/
    `answer_from_kb()`内部依赖的`get_store()`只能在"图节点真实执行"的
    运行时上下文里工作，裸调用会报`RuntimeError: Called get_config
    outside of a runnable context`——这个坑`llm_answer_kb.py`/
    `store_client.py`的文档里反复提过，但写这段代码时还是踩上了，说明
    "知道这条规则"和"每次写代码都记得套用"是两回事，真实跑一遍才现身
    （第一次尝试裸调answer_from_kb()就是这么崩的）。修复：`get_reasoning_enabled`
    显式传store参数（依赖注入，绕开get_store()）；关停时不再裸调
    answer_from_kb()，改成真的invoke一次kb_graph（两个节点在图执行的
    运行时上下文里跑，get_store()自然能工作）。
    """
    store = get_store_instance()
    if not get_reasoning_enabled(store=store):
        kb_graph = get_kb_graph()
        sub_result = kb_graph.invoke(
            {
                "follow_user_id": follow_user_id,
                "external_id": external_id,
                "question": question,
                "retrieved_chunks": [],
                "hit_knowledge_base": False,
                "answer_text": "",
                "cited_sources": [],
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        yield _format_sse_event(
            "kb_fallback",
            {"answer_text": sub_result["answer_text"], "cited_sources": sub_result["cited_sources"]},
        )
        yield _format_sse_event("done", {"thread_id": thread_id})
        return

    graph = get_reasoning_graph()
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = {
        "follow_user_id": follow_user_id,
        "external_id": external_id,
        "question": question,
        "plan": [],
        "step_results": [],
        "current_step_index": 0,
        "final_suggestion": None,
        "confidence_note": None,
    }

    for event_data in graph.stream(initial_state, config=config, stream_mode="custom"):
        event_type = event_data.pop("type")
        yield _format_sse_event(event_type, event_data)

    yield _format_sse_event("done", {"thread_id": thread_id})


@app.get("/tasks/stream_reasoning/{thread_id}")
def stream_reasoning(thread_id: str, follow_user_id: str, external_id: str, question: str):
    return StreamingResponse(
        _stream_reasoning_events(thread_id, follow_user_id, external_id, question),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# /store/get /store/put /store/search —— 通用 Store 接口（ 新增）
#
# 背景：02 号架构文档 5.1 节早就列了这三个接口（kam_client 要通过它们读写
# 员工/客户/画像/标签/消息/订单 7 个命名空间），但当时只写了"接口名+方法+用途"
# 一直没实现。现在 kam_client 要真正开工，才补上。
#
# 设计取舍：走"通用泛型接口"而不是"每个命名空间单独暴露一个
# 语义化接口"（后者是 7 个命名空间 × 3 个方法 = 21 条路由）。三个接口直接
# 把 namespace/key/value/filter 透传给 store.get/put/search，不做命名空间
# 白名单校验——代价是接口层不管"谁能读写哪个命名空间"这件事，但这几个接口
# 只给内网的 kam_admin/kam_client 调用，不对外暴露，权衡后接受这个代价换
# 更小的代码量。
#
# 为什么不直接用 langgraph_sdk 连 LangGraph Server 自带的 Store HTTP API
# FastAPI 应用（uvicorn 直接跑 webapp.py），不是用 langgraph dev/up 启动的
# LangGraph Server/Platform 部署模式，没有这套官方 Store HTTP API 可用。
# 改成这个模式要连累 /tasks/* 三个已经端到端验证通过的接口一起大改，
# 代价远大于自己写三个薄接口，所以选择在现有 FastAPI 架构里补接口。
#
# namespace 用 JSON 数组传输（HTTP/JSON 没有元组类型），接口内部转回元组
# ——store.get/put/search 要求 namespace 必须是 tuple，传 list 会报错。
# ---------------------------------------------------------------------------


class StoreGetRequest(BaseModel):
    namespace: list[str]
    key: str


class StorePutRequest(BaseModel):
    namespace: list[str]
    key: str
    value: dict[str, Any]


class StoreSearchRequest(BaseModel):
    namespace: list[str]
    filter: dict[str, Any] | None = None
    limit: int | None = None


class StoreBatchGetRequest(BaseModel):
    items: list[StoreGetRequest]


class StoreBatchSearchRequest(BaseModel):
    items: list[StoreSearchRequest]


_ALLOWED_STORE_NAMESPACES = {
    "employee",
    "external_user",
    "external_user_profile",
    "system_config",
    "tags_setting",
    "wxkf_msg",
    "wxxd_order",
    "wxqy_msg",
}


def _require_internal_api_key(
    provided_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> None:
    """校验后台服务间调用的共享密钥。"""
    expected_key = os.getenv("INTERNAL_API_KEY")
    if not expected_key:
        raise HTTPException(status_code=503, detail="Internal API key is not configured")
    if not provided_key or not hmac.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=401, detail="Invalid internal API key")


def _validated_store_namespace(namespace: list[str]) -> tuple[str, ...]:
    """把 JSON 命名空间转为 Store 元组，并限制可访问的业务域。"""
    if not namespace or namespace[0] not in _ALLOWED_STORE_NAMESPACES:
        raise HTTPException(status_code=403, detail="Store namespace is not allowed")
    if any(not part or len(part) > 200 for part in namespace):
        raise HTTPException(status_code=400, detail="Invalid store namespace")
    return tuple(namespace)


def _validate_batch_size(items: list) -> None:
    """限制单次批量请求规模，避免内部接口被误用为无上限全表扫描。"""
    if len(items) > 1000:
        raise HTTPException(status_code=400, detail="Batch size exceeds 1000 items")


def _item_to_dict(item) -> dict | None:
    """store.get/search 返回的是 LangGraph Item 对象（有 .value/.key 等属性），
    不能直接塞进 FastAPI 的 JSON 响应，这里统一转成纯 dict。"""
    if item is None:
        return None
    return {"key": item.key, "value": item.value}


@app.post("/store/get")
def store_get(payload: StoreGetRequest, _auth: None = Depends(_require_internal_api_key)):
    store = get_store_instance()
    item = store.get(_validated_store_namespace(payload.namespace), payload.key)
    return {"item": _item_to_dict(item)}


@app.post("/store/put")
def store_put(payload: StorePutRequest, _auth: None = Depends(_require_internal_api_key)):
    store = get_store_instance()
    store.put(_validated_store_namespace(payload.namespace), payload.key, payload.value)
    return {"success": True}


@app.post("/store/search")
def store_search(payload: StoreSearchRequest, _auth: None = Depends(_require_internal_api_key)):
    store = get_store_instance()
    items = store.search(
        _validated_store_namespace(payload.namespace),
        filter=payload.filter,
        limit=payload.limit,
    )
    return {"items": [_item_to_dict(item) for item in items]}


@app.post("/store/batch_get")
def store_batch_get(
    payload: StoreBatchGetRequest, _auth: None = Depends(_require_internal_api_key)
):
    """后台批量读取：一次 HTTP 请求内执行多个 Store get。"""
    _validate_batch_size(payload.items)
    store = get_store_instance()
    results = [
        _item_to_dict(
            store.get(_validated_store_namespace(item.namespace), item.key)
        )
        for item in payload.items
    ]
    return {"items": results}


@app.post("/store/batch_search")
def store_batch_search(
    payload: StoreBatchSearchRequest, _auth: None = Depends(_require_internal_api_key)
):
    """后台批量搜索：一次 HTTP 请求内执行多个 Store search。"""
    _validate_batch_size(payload.items)
    store = get_store_instance()
    results = []
    for item in payload.items:
        matches = store.search(
            _validated_store_namespace(item.namespace),
            filter=item.filter,
            limit=item.limit,
        )
        results.append([_item_to_dict(match) for match in matches])
    return {"results": results}


# ---------------------------------------------------------------------------
# 资料库管理接口（，02号架构文档7.4节）
#
# 调用方是 kam_admin（运营人员上传/替换资料），走独立的 /kb/documents
# 接口而不是通用 /store/* ——知识库向量数据根本不在7个Store命名空间里，
# 是另一个独立的pgvector实例（KAM_VECTOR_POSTGRES_URL），只有kam_agent
# 自己的kb_ingest.py知道怎么切分/向量化/写入，kam_admin只能把原始文本
# 整个转发过来，由这里调用已经写好的ingest_document()/list_documents()
# （两个函数是晚间kb_ingest_official_content.py摄入四类资料时
# 就已验证过的现成逻辑，这里只是补一层HTTP接口暴露出去，不是重新实现）。
# ---------------------------------------------------------------------------

_KB_VALID_CATEGORIES = {"profile", "solution", "honor", "faq"}


class KbUploadRequest(BaseModel):
    source_doc: str
    category: str
    content: str


class KbUploadResponse(BaseModel):
    success: bool
    chunk_count: int


@app.post("/kb/documents", response_model=KbUploadResponse)
def upload_kb_document(
    payload: KbUploadRequest, _auth: None = Depends(_require_internal_api_key)
):
    if payload.category not in _KB_VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"未知类别: {payload.category}，可选：{sorted(_KB_VALID_CATEGORIES)}",
        )
    if not payload.source_doc.strip() or not payload.content.strip():
        raise HTTPException(status_code=400, detail="source_doc 和 content 均不能为空")

    chunk_count = ingest_document(
        text=payload.content, source_doc=payload.source_doc, category=payload.category
    )
    return KbUploadResponse(success=True, chunk_count=chunk_count)


class KbDocumentInfo(BaseModel):
    category: str
    source_doc: str
    chunk_count: int
    updated_at: str | None


class KbDocumentListResponse(BaseModel):
    documents: list[KbDocumentInfo]


@app.get("/kb/documents", response_model=KbDocumentListResponse)
def get_kb_documents(_auth: None = Depends(_require_internal_api_key)):
    return KbDocumentListResponse(documents=list_documents())


class KbDocumentDetailResponse(BaseModel):
    source_doc: str
    content: str


@app.get("/kb/documents/{source_doc}", response_model=KbDocumentDetailResponse)
def get_kb_document_detail(
    source_doc: str, _auth: None = Depends(_require_internal_api_key)
):
    """查看单份文档的完整内容（，kam_admin"查看"功能用）。
    跟上面 /kb/documents（列表，不含content）是两个不同粒度的查询，
    故意不合并。"""
    content = get_document_content(source_doc)
    if content is None:
        raise HTTPException(status_code=404, detail=f"未找到文档：{source_doc}")
    return KbDocumentDetailResponse(source_doc=source_doc, content=content)


@app.get("/health")
def health_check():
    """仅供本机启动脚本探测：执行一次真实 Store 读取但不返回业务数据。"""
    store = get_store_instance()
    store.get(("system_config",), "__healthcheck__")
    return {"status": "ok"}
