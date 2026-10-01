"""
主图定义。

主图按意图路由到各能力子图：
  START → load_data → detect_intent → (conditional: route_by_intent)
       ├─ call_profile_subgraph
       ├─ call_tag_subgraph
       ├─ call_chat_suggestion_subgraph
       ├─ call_kf_chat_suggestion_subgraph
       └─ call_kb_subgraph

**F17不在这张图里**：改为webapp.py直接对reasoning_graph做
stream_mode="custom"流式调用（真实步骤透明推送），不再经过主图intent
路由——原因见kam_node.py的route_by_intent文档字符串。
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from src.graphs.kam_graph.kam_node import (
    call_chat_suggestion_subgraph,
    call_kb_subgraph,
    call_kf_chat_suggestion_subgraph,
    call_profile_subgraph,
    call_tag_subgraph,
    detect_intent,
    route_by_intent,
)
from src.graphs.kam_graph.kam_node_load_data import load_data


class MainState(TypedDict):
    """
    主图 State 声明加载数据、路由意图和各子图输出字段。
    没有在 TypedDict 中声明的字段会被 LangGraph 静默丢弃。
    """
    #  新增：call_profile_subgraph 需要用主 thread_id 拼出每个字段
    # 各自的子 thread_id（f"{_main_thread_id}:{field_name}"）。节点函数只能读
    # State、读不到 config["configurable"]["thread_id"]，所以调用方（webapp.py）
    # 发起主图 invoke() 时，要把同一个 thread_id 也放进初始 State 里一份。
    _main_thread_id: str

    follow_user_id: str
    external_id: str
    intent: str
    profile_field_updates: dict
    #  新增：每个字段各自子图 invoke 后产生的 interrupt 信息列表，
    # webapp.py 的 SSE 端点从这里读出来拼事件（见 kam_node.py 里的说明）
    profile_field_interrupts: list

    #  新增（F04接线）：call_tag_subgraph 整批invoke后产生的那一条
    # interrupt（没有的话是 None，理论上不会发生——子图入口必然interrupt）。
    tag_interrupt: dict | None

    #  新增（F02/F03接线）：call_chat_suggestion_subgraph /
    # call_kf_chat_suggestion_subgraph 的产出——没有interrupt，直接是终态
    # 结果，webapp.py 的接口从这里读出来直接返回给前端（不需要SSE推送
    # 逐条事件，见09号设计文档判断点4）。call_kb_subgraph
    # （F16）也复用同一个字段，产出结构见kam_node.py里的说明。
    task_result: dict | None

    # F16 使用的客户原始问题文本，只有intent=
    # "knowledge_base"时才会被调用方（webapp.py）放进初始State——其余
    # intent不需要这个字段，也不会有节点去设置它，所以类型允许None
    # （不像follow_user_id/external_id那样每次请求必然有值）。
    question: str | None

    #  补充：load_data 的产出，画像子图需要这些数据（方案A，见实战记录）
    external_user: dict | None
    profile: dict | None
    wxqy_msgs: list
    wxkf_msgs: list
    orders: list


_builder = StateGraph(MainState)

_builder.add_node("load_data", load_data)
_builder.add_node("detect_intent", detect_intent)
_builder.add_node("profile_subgraph", call_profile_subgraph)
_builder.add_node("tag_subgraph", call_tag_subgraph)
_builder.add_node("chat_suggestion_subgraph", call_chat_suggestion_subgraph)
_builder.add_node("kf_chat_suggestion_subgraph", call_kf_chat_suggestion_subgraph)
_builder.add_node("kb_subgraph", call_kb_subgraph)

_builder.add_edge(START, "load_data")
_builder.add_edge("load_data", "detect_intent")

_builder.add_conditional_edges(
    "detect_intent",
    route_by_intent,
    {
        "profile_subgraph": "profile_subgraph",
        "tag_subgraph": "tag_subgraph",
        "chat_suggestion_subgraph": "chat_suggestion_subgraph",
        "kf_chat_suggestion_subgraph": "kf_chat_suggestion_subgraph",
        "kb_subgraph": "kb_subgraph",
    },
)

_builder.add_edge("profile_subgraph", END)
_builder.add_edge("tag_subgraph", END)
_builder.add_edge("chat_suggestion_subgraph", END)
_builder.add_edge("kf_chat_suggestion_subgraph", END)
_builder.add_edge("kb_subgraph", END)

#  决定（见 实战记录.md 当天条目）：checkpointer 采用全局单例+连接池，
# 由 webapp.py 在应用启动时创建一次 PostgresSaver，同一个实例同时传给这里
# 和 sub_profile_graph.py 的 build_profile_graph()，保证主图子图共用同一份。
# 不在这里内部固定调 .compile()，改成工厂函数，避免模块导入时就要求
# checkpointer 已经就绪（webapp.py 需要先建好连接池再编译图）。
#
# ：compile() 除了 checkpointer 参数
# 管 interrupt() 的"冻结恢复"，还有一个完全独立的 store 参数——
# load_data 节点调用的 get_external_user 等 store_client.py 函数内部用
# get_store() 拿 Store 实例，而 get_store() 只能在"图 compile() 时传了 store 参数"
# 的运行时上下文里工作，不传的话 get_store() 返回 None，后续 store.get(...)
# 直接崩掉（真实报错：AttributeError: 'NoneType' object has no attribute 'get'，
# 卡在 load_data 节点里 external_user.value 那一行，因为 external_user 本身
# 就是 None）。checkpointer 和 store 是 compile() 的两个不同入参，管的是两件
#不同的事，之前只顾着接 checkpointer 把这个漏掉了。


def build_kam_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)


# 主图 build_kam_graph() 的 checkpointer/store 和标签子图 build_tag_graph()
# 必须是同一个实例——这条约定和画像子图完全一致（见文件顶部  决定），
# webapp.py 的 lifespan 里要记得同时编译并注入标签子图。
