"""
标签推荐子图的图定义（编译成一个可 .invoke() 的独立 graph 对象）。

结构：START → generate_tag_recommendations → confirm_tag_batch。
没有recreate分支，不需要 Command(goto=Send(...)) 动态路由，比画像子图
简单——generate_tag_recommendations 也不需要像画像子图的
generate_all_field_drafts 那样上移到主图层，因为这个子图从头到尾只
invoke一次，生成和确认在同一次执行里，没有必要跨层拆分。
"""

from langgraph.graph import START, StateGraph

from src.graphs.kam_graph.kam_sub_graph_tag.sub_tag_node import (
    confirm_tag_batch,
    generate_tag_recommendations,
)
from src.graphs.kam_graph.kam_sub_graph_tag.sub_tag_state import TagSubState

_builder = StateGraph(TagSubState)

_builder.add_node("generate_tag_recommendations", generate_tag_recommendations)
_builder.add_node("confirm_tag_batch", confirm_tag_batch)

_builder.add_edge(START, "generate_tag_recommendations")
_builder.add_edge("generate_tag_recommendations", "confirm_tag_batch")

# confirm_tag_batch interrupt 一次、resume 后直接写 Store 并 return，
# 没有 recreate 分支，不需要额外的动态路由或静态边去接 regenerate 节点。


def build_tag_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)
