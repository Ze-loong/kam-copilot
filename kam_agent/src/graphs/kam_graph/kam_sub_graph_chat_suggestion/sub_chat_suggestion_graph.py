"""
销售聊天回复建议子图的图定义。

结构：START → retrieve_solution_refs → generate_chat_suggestion → END。
没有interrupt，没有recreate，没有条件边。checkpointer/store
仍然照常传给 compile()（保持与其他子图接口一致、且 generate 节点内部如果
以后要用 get_store() 读取更多数据仍然需要 store 参数就绪），但这个子图
不依赖 checkpointer 的"冻结恢复"能力——invoke() 一次跑完，不会中途暂停。
设计判断过程见 设计文档/09-F02F03回复建议设计文档.md 第二节判断点2。
"""

from langgraph.graph import END, START, StateGraph

from src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_node import (
    generate_chat_suggestion,
    retrieve_solution_refs,
)
from src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_state import (
    ChatSuggestionState,
)

_builder = StateGraph(ChatSuggestionState)

_builder.add_node("retrieve_solution_refs", retrieve_solution_refs)
_builder.add_node("generate_chat_suggestion", generate_chat_suggestion)

_builder.add_edge(START, "retrieve_solution_refs")
_builder.add_edge("retrieve_solution_refs", "generate_chat_suggestion")
_builder.add_edge("generate_chat_suggestion", END)


def build_chat_suggestion_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)
