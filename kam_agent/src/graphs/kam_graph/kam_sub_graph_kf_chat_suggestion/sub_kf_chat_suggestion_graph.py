"""
客服聊天回复建议子图的图定义。结构与 sub_chat_suggestion_graph.py（销售场景）
完全对称：START → generate_kf_chat_suggestion → END，无interrupt。
"""

from langgraph.graph import END, START, StateGraph

from src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_node import (
    generate_kf_chat_suggestion,
)
from src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_state import (
    KfChatSuggestionState,
)

_builder = StateGraph(KfChatSuggestionState)

_builder.add_node("generate_kf_chat_suggestion", generate_kf_chat_suggestion)

_builder.add_edge(START, "generate_kf_chat_suggestion")
_builder.add_edge("generate_kf_chat_suggestion", END)


def build_kf_chat_suggestion_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)
