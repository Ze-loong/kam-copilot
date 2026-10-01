"""
跨模块综合推理子图（F17）的图定义。

结构：START → generate_plan → execute_step ⟲（自我循环，直到步骤执行完）
     → synthesize → END。

execute_step 到自己的条件边是全子图唯一的循环点——route_after_execute_step
（sub_reasoning_node.py）判断 current_step_index 是否已经等于 len(plan)，
没执行完就继续回到 execute_step 处理下一步，执行完才放行到 synthesize。
这是LangGraph里标准的"节点自循环"写法（条件边的目标可以是自己），不需要
额外的while循环或递归。

没有interrupt、没有Send并行扇出——三段式严格顺序执行（含循环），checkpointer/
store仍照常传给compile()，理由与F02/F03/F16一致（子图不需要中途暂停，
但主图/子图共用同一checkpointer实例这条约定全项目统一，不因为这个子图
"更简单"就破例）。
"""

from langgraph.graph import END, START, StateGraph

from src.graphs.kam_graph.kam_sub_graph_reasoning.sub_reasoning_node import (
    execute_step,
    generate_plan,
    route_after_execute_step,
    synthesize,
)
from src.graphs.kam_graph.kam_sub_graph_reasoning.sub_reasoning_state import ReasoningState

_builder = StateGraph(ReasoningState)

_builder.add_node("generate_plan", generate_plan)
_builder.add_node("execute_step", execute_step)
_builder.add_node("synthesize", synthesize)

_builder.add_edge(START, "generate_plan")
_builder.add_edge("generate_plan", "execute_step")

_builder.add_conditional_edges(
    "execute_step",
    route_after_execute_step,
    {
        "execute_step": "execute_step",
        "synthesize": "synthesize",
    },
)

_builder.add_edge("synthesize", END)


def build_reasoning_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)
