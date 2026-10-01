"""
知识库问答子图（F16）的图定义。

结构：START → retrieve_kb_chunks → generate_kb_answer → END。
比F02多一个节点（多了检索环节），但同样没有interrupt、没有条件边——
一次invoke()跑完，不会中途暂停。checkpointer/store仍照常传给compile()，
理由与F02一致（见 sub_chat_suggestion_graph.py 的说明），此处不重复。

设计判断过程见 02 号架构设计文档 7.3 节。
"""

from langgraph.graph import END, START, StateGraph

from src.graphs.kam_graph.kam_sub_graph_kb.sub_kb_node import (
    generate_kb_answer,
    retrieve_kb_chunks,
)
from src.graphs.kam_graph.kam_sub_graph_kb.sub_kb_state import KbState

_builder = StateGraph(KbState)

_builder.add_node("retrieve_kb_chunks", retrieve_kb_chunks)
_builder.add_node("generate_kb_answer", generate_kb_answer)

_builder.add_edge(START, "retrieve_kb_chunks")
_builder.add_edge("retrieve_kb_chunks", "generate_kb_answer")
_builder.add_edge("generate_kb_answer", END)


def build_kb_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)
