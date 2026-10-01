"""复现同一子图内并发中断与动态 Send 叠加时，新中断未进入返回值的现象。

预期：只恢复三个待确认字段中的一个并重新生成时，返回值应含另外两个旧中断
及该字段新中断；实际：组 D 的集合检查显示新中断可能缺失。
业务修复位于 src/graphs/kam_graph/kam_node.py 的 call_profile_subgraph：
每个字段使用独立 thread_id 和单字段子图，避免同一实例存在多个待处理中断。

实验复刻真实 recreate 主链路，但不调用 LLM：

    START → entry_node → confirm_node(interrupt)
          → recreate → Send(regenerate_node) → Send(confirm_node, 新值) → 第二轮 interrupt

组 A：主图包装节点内部手动 subgraph.invoke(sub_input)，对应当前 call_profile_subgraph。
组 B：主图直接 add_node("subgraph_node", compiled_subgraph)，对应官方挂载方式。
组 C：手动 invoke 后只映射一个业务字段，复刻真实 call_profile_subgraph 的窄返回。
组 D：三个字段用 Send 并行扇出；只 resume 其中一个字段的 recreate。

连接串只从 KAM_POSTGRES_URL 环境变量读取。

运行方式（在 kam_agent 目录）：
    uv run python -m experiments.repro_parallel_dynamic_interrupt
"""

import os
from typing import TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt


class SharedState(TypedDict):
    """组 B 父子图共享的 State；组 A 的两张图也使用同一最小字段集合方便对照。"""

    field_value: str
    round_name: str
    final_value: str


def entry_node(state: SharedState) -> dict:
    """模拟首次草稿生成；不做副作用，也不调用真实 LLM。"""
    return {
        "field_value": state["field_value"],
        "round_name": state.get("round_name", "initial"),
    }


def confirm_node(state: SharedState):
    """模拟单字段确认：recreate 时动态 Send 到重生节点，终态时结束。"""
    action = interrupt(
        {
            "field": "demo_field",
            "value": state["field_value"],
            "round": state["round_name"],
        }
    )
    if action == "recreate":
        return Command(goto=Send("regenerate_node", dict(state)))
    if action == "ok":
        return {"final_value": state["field_value"]}
    if action == "discard":
        return {"final_value": ""}
    raise ValueError(f"不支持的 action: {action}")


def regenerate_node(state: SharedState):
    """模拟单字段 LLM 重生：写死一份新草稿，再 Send 回确认节点。"""
    regenerated_state = {
        **state,
        "field_value": "regenerated-value",
        "round_name": "regenerated",
    }
    return Command(goto=Send("confirm_node", regenerated_state))


def build_subgraph(checkpointer=None):
    builder = StateGraph(SharedState)
    builder.add_node("entry_node", entry_node)
    builder.add_node("confirm_node", confirm_node)
    builder.add_node("regenerate_node", regenerate_node)
    builder.add_edge(START, "entry_node")
    builder.add_edge("entry_node", "confirm_node")
    # confirm_node / regenerate_node 都由 Command(goto=Send(...)) 动态路由，不能再加静态边。
    return builder.compile(checkpointer=checkpointer)


def build_manual_invoke_parent(checkpointer):
    """组 A：复刻当前业务的“包装节点内手动 invoke 子图”。"""
    subgraph = build_subgraph(checkpointer=checkpointer)

    def call_subgraph_node(state: SharedState) -> dict:
        return subgraph.invoke(
            {
                "field_value": state["field_value"],
                "round_name": state.get("round_name", "initial"),
                "final_value": "",
            }
        )

    builder = StateGraph(SharedState)
    builder.add_node("call_subgraph_node", call_subgraph_node)
    builder.add_edge(START, "call_subgraph_node")
    builder.add_edge("call_subgraph_node", END)
    return builder.compile(checkpointer=checkpointer)


def build_manual_invoke_narrow_return_parent(checkpointer):
    """组 C：复刻真实包装节点只从子图结果挑业务字段回传的写法。"""
    subgraph = build_subgraph(checkpointer=checkpointer)

    def call_subgraph_node(state: SharedState) -> dict:
        sub_result = subgraph.invoke(
            {
                "field_value": state["field_value"],
                "round_name": state.get("round_name", "initial"),
                "final_value": "",
            }
        )
        # 对应真实代码：只取 field_updates，而不把 sub_result 原样返回给父图。
        return {"final_value": sub_result.get("final_value", "")}

    builder = StateGraph(SharedState)
    builder.add_node("call_subgraph_node", call_subgraph_node)
    builder.add_edge(START, "call_subgraph_node")
    builder.add_edge("call_subgraph_node", END)
    return builder.compile(checkpointer=checkpointer)


def build_mounted_subgraph_parent(checkpointer):
    """组 B：官方挂载；子图默认继承父图的 checkpointer。"""
    subgraph = build_subgraph()
    builder = StateGraph(SharedState)
    builder.add_node("subgraph_node", subgraph)
    builder.add_edge(START, "subgraph_node")
    builder.add_edge("subgraph_node", END)
    return builder.compile(checkpointer=checkpointer)


def multi_entry_node(_state: SharedState) -> dict:
    """模拟一次生成三个字段草稿，供后续 Send 并行确认。"""
    return {
        "drafts": {
            "field_a": "initial-a",
            "field_b": "initial-b",
            "field_c": "initial-c",
        }
    }


def dispatch_multi_fields(state: dict):
    """复刻 dispatch_fields_for_confirm：每个字段生成一张独立 Send 任务卡。"""
    return [
        Send(
            "confirm_multi_field",
            {
                "field_name": field_name,
                "field_value": value,
                "round_name": "initial",
            },
        )
        for field_name, value in state["drafts"].items()
    ]


def confirm_multi_field(state: dict):
    """多字段版本确认节点；recreate 只重新生成当前任务卡所属字段。"""
    action = interrupt(
        {
            "field": state["field_name"],
            "value": state["field_value"],
            "round": state["round_name"],
        }
    )
    if action == "recreate":
        return Command(goto=Send("regenerate_multi_field", dict(state)))
    if action in {"ok", "discard"}:
        return {}
    raise ValueError(f"不支持的 action: {action}")


def regenerate_multi_field(state: dict):
    """只为被 resume 的字段生成固定新草稿，再进入该字段的第二轮确认。"""
    regenerated_state = {
        **state,
        "field_value": f"regenerated-{state['field_name']}",
        "round_name": "regenerated",
    }
    return Command(goto=Send("confirm_multi_field", regenerated_state))


def build_multi_pending_subgraph(checkpointer):
    """组 D 子图：一次扇出三条确认任务，允许其中一条动态 recreate。"""
    builder = StateGraph(dict)
    builder.add_node("multi_entry_node", multi_entry_node)
    builder.add_node("confirm_multi_field", confirm_multi_field)
    builder.add_node("regenerate_multi_field", regenerate_multi_field)
    builder.add_edge(START, "multi_entry_node")
    builder.add_conditional_edges(
        "multi_entry_node",
        dispatch_multi_fields,
        ["confirm_multi_field"],
    )
    return builder.compile(checkpointer=checkpointer)


def run_multi_pending_case(checkpointer) -> bool:
    """只 resume field_b，核对其新 interrupt 与未触及字段的旧 interrupt 是否同时返回。"""
    graph = build_multi_pending_subgraph(checkpointer)
    config = {"configurable": {"thread_id": "dynamic-send-multi-pending-probe"}}

    print("\n=== 组 D：三字段并行 Send：首次 invoke ===")
    initial = graph.invoke({}, config=config)
    first_interrupts = list(initial.get("__interrupt__", ()))
    first_by_field = {item.value["field"]: item for item in first_interrupts}
    print(f"首次 interrupt 数量: {len(first_interrupts)}")
    for item in first_interrupts:
        print(f"  id={item.id} field={item.value['field']} round={item.value['round']}")
    if set(first_by_field) != {"field_a", "field_b", "field_c"}:
        raise AssertionError("组 D 首次应恰好得到 field_a / field_b / field_c 三条 interrupt")

    print("\n=== 组 D：只 resume field_b 的 recreate ===")
    resumed = graph.invoke(
        Command(resume={first_by_field["field_b"].id: "recreate"}),
        config=config,
    )
    second_interrupts = list(resumed.get("__interrupt__", ()))
    actual = {(item.value["field"], item.value["round"], item.value["value"]) for item in second_interrupts}
    expected = {
        ("field_a", "initial", "initial-a"),
        ("field_b", "regenerated", "regenerated-field_b"),
        ("field_c", "initial", "initial-c"),
    }
    print(f"resume 返回的 interrupt 数量: {len(second_interrupts)}")
    for item in second_interrupts:
        print(f"  id={item.id} field={item.value['field']} round={item.value['round']} value={item.value['value']}")
    if actual == expected:
        print("[PASS] 两条未触及旧 interrupt 与 field_b 新 interrupt 都正确返回")
        return True

    print(f"[FAIL] 实际 interrupt 集合: {actual}")
    return False


def run_case(label: str, graph, thread_id: str) -> bool:
    """运行一次首次中断 → recreate，并检查第二轮新 interrupt 是否出现在父图返回值中。"""
    config = {"configurable": {"thread_id": thread_id}}
    print(f"\n=== {label}：首次 invoke ===")
    initial = graph.invoke(
        {"field_value": "initial-value", "round_name": "initial", "final_value": ""},
        config=config,
    )
    first_interrupts = list(initial.get("__interrupt__", ()))
    print(f"首次 interrupt 数量: {len(first_interrupts)}")
    for item in first_interrupts:
        print(f"  id={item.id} value={item.value}")
    if len(first_interrupts) != 1:
        raise AssertionError(f"{label} 首次应恰好有 1 个 interrupt")

    print(f"\n=== {label}：resume recreate ===")
    resumed = graph.invoke(
        Command(resume={first_interrupts[0].id: "recreate"}),
        config=config,
    )
    second_interrupts = list(resumed.get("__interrupt__", ()))
    print(f"resume 返回的 interrupt 数量: {len(second_interrupts)}")
    for item in second_interrupts:
        print(f"  id={item.id} value={item.value}")

    regenerated = [
        item
        for item in second_interrupts
        if item.value.get("round") == "regenerated"
        and item.value.get("value") == "regenerated-value"
    ]
    if len(regenerated) == 1:
        print("[PASS] 父图返回了第二轮重生后的 interrupt")
        return True

    print("[FAIL] 父图返回中没有第二轮重生后的 interrupt")
    return False


def main() -> None:
    connection_uri = os.environ.get("KAM_POSTGRES_URL")
    if not connection_uri:
        raise RuntimeError("未设置 KAM_POSTGRES_URL")

    with PostgresSaver.from_conn_string(connection_uri) as checkpointer:
        checkpointer.setup()
        manual_ok = run_case(
            "组 A：手动 invoke 子图",
            build_manual_invoke_parent(checkpointer),
            "dynamic-send-manual-probe",
        )
        narrow_return_ok = run_case(
            "组 C：手动 invoke + 窄返回",
            build_manual_invoke_narrow_return_parent(checkpointer),
            "dynamic-send-narrow-return-probe",
        )
        mounted_ok = run_case(
            "组 B：官方挂载子图",
            build_mounted_subgraph_parent(checkpointer),
            "dynamic-send-mounted-probe",
        )
        multi_pending_ok = run_multi_pending_case(checkpointer)

    print("\n=== 实验结论 ===")
    print(f"组 A（手动 invoke）: {'PASS' if manual_ok else 'FAIL'}")
    print(f"组 C（手动 invoke + 窄返回）: {'PASS' if narrow_return_ok else 'FAIL'}")
    print(f"组 B（官方挂载）: {'PASS' if mounted_ok else 'FAIL'}")
    print(f"组 D（三字段并行 + 部分 resume）: {'PASS' if multi_pending_ok else 'FAIL'}")
    if not mounted_ok:
        raise AssertionError("官方挂载组也未拿到第二轮 interrupt，不能据此贸然重构业务图")


if __name__ == "__main__":
    main()
