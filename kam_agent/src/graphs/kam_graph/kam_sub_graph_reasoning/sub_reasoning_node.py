"""
跨模块综合推理子图（F17）的节点函数：generate_plan → execute_step（循环）→ synthesize。

三段式结构、State设计已在02号架构设计文档7.2节定案。execute_step节点内部
"tool名 -> 该查哪张表/调哪个函数"这套映射，架构文档没有细化到这一层
（文档原话："资源切分策略实测确定"留给开发阶段），是由AI在
"用户暂时无法应答、已授权自主推进"前提下做的工程判断，映射关系直接对应
01号需求文档3.7.2节"五个信息入口"表格，不是自由发挥。

execute_step 是全子图唯一会自我循环的节点：每次只处理plan里的一步，
current_step_index 递增后由 sub_reasoning_graph.py 的条件边判断是继续
循环还是转到synthesize——这是"步数上限=计划长度"这条设计（架构文档7.2节）
在代码层面的落地方式，不需要额外的计数器/上限变量。

**：`get_stream_writer()`真实步骤透明推送**。此前F01的
SSE"实时推送"仔细看并不是真正的边算边推——`call_profile_subgraph`
内部循环把5个字段全部同步生成完，`graph.invoke()`才返回，SSE端点只是
把一个已经算完的列表逐条yield出去，前端看起来像"陆续到达"，实际计算
早就结束了。F17如果照抄这个套路，20秒的等待体验不会有任何实质改善。
这里改用LangGraph官方的`stream_mode="custom"`机制：每个节点函数内部
调用`get_stream_writer()`拿到的写入函数，在真实计算发生的当下就把消息
写出去，配合webapp.py那边用`graph.stream(..., stream_mode="custom")`
（而不是`.invoke()`）来跑图，才能做到消息与真实计算进度同步。每个
节点在"开始做"和"做完"两个时间点各写一条消息（不是只在做完后写），
这样LLM调用/查询期间用户也能看到"正在...”这类进行中提示，不是干等到
结果出来才看到一条"已完成"。
"""

from langgraph.config import get_stream_writer

from src.graphs.kam_graph.kam_sub_graph_reasoning.sub_reasoning_state import (
    ReasoningState,
    StepResult,
)
from src.kb.kb_retriever import search_knowledge_base
from src.llm.llm_reasoning import generate_reasoning_plan, synthesize_reasoning_suggestion
from src.store.store_client import (
    get_external_user,
    get_external_user_profile,
    search_tags_setting,
    search_wxxd_order,
)

# tool -> 展示给顾问看的"进行中"提示文案（01号文档"步骤透明"约束的文案素材，
# 具体措辞是内容层面的选择，不是架构判断，后续可以随意调整）。
_TOOL_DISPLAY_LABEL = {
    "knowledge_base": "集团资料",
    "solution_catalog": "产品与解决方案",
    "profile": "客户画像",
    "order": "订单记录",
    "tag": "客户标签",
}


def generate_plan(state: ReasoningState) -> dict:
    """计划阶段：让LLM决定查哪几项信息、为什么查。产出后不再变更。"""
    writer = get_stream_writer()
    writer({"type": "planning_start"})

    plan = generate_reasoning_plan(question=state["question"])

    writer({"type": "plan_ready", "plan": plan})
    return {"plan": plan, "current_step_index": 0, "step_results": []}


def _run_tool(tool: str, follow_user_id: str, external_id: str, question: str) -> list | dict:
    """按tool类型执行对应的查询，返回原始数据（未包装）。

    与F16/F01/F04不同，这里是"按需现查"而不是复用load_data已经预加载好的
    数据——F17这条链路（webapp.py `/tasks/stream_reasoning`直接对
    reasoning_graph操作，不再经过主图/load_data）只透传
    follow_user_id/external_id/question三个字段，真正符合"Plan-and-Execute"
    里"execute每一步才真的调用对应工具"这个语义，而不是"反正数据都已经
    在手上了随便挑"。
    """
    if tool == "knowledge_base":
        return search_knowledge_base(query=question, top_k=3)

    if tool == "solution_catalog":
        return search_knowledge_base(query=question, top_k=3, category="solution")

    if tool == "profile":
        item = get_external_user_profile(follow_user_id, external_id)
        return item.value["profile_items"] if item else {}

    if tool == "order":
        user_item = get_external_user(follow_user_id, external_id)
        union_id = user_item.value["union_id"] if user_item else None
        if not union_id:
            return []
        return [dict(order.value) for order in search_wxxd_order(union_id)]

    if tool == "tag":
        user_item = get_external_user(follow_user_id, external_id)
        tag_ids = user_item.value["tags"] if user_item else []
        catalog = {
            item.key: item.value
            for item in search_tags_setting()
            if hasattr(item, "key") and hasattr(item, "value")
        }
        return [catalog[tag_id]["tag_name"] for tag_id in tag_ids if tag_id in catalog]

    raise ValueError(f"unknown reasoning tool: {tool}")


def execute_step(state: ReasoningState) -> dict:
    """执行阶段：处理plan[current_step_index]这一步，结果追加进step_results。"""
    index = state["current_step_index"]
    step = state["plan"][index]
    tool = step["tool"]
    label = _TOOL_DISPLAY_LABEL.get(tool, tool)

    writer = get_stream_writer()
    writer({"type": "step_start", "step_index": index, "tool": tool, "display_text": f"正在查询{label}..."})

    raw_items = _run_tool(
        tool=tool,
        follow_user_id=state["follow_user_id"],
        external_id=state["external_id"],
        question=state["question"],
    )

    display_text = f"已查询{label}" + (f"（{len(raw_items)}条）" if raw_items else "（无结果）")

    step_result: StepResult = {
        "step_index": index,
        "tool": tool,
        "raw_result": {"items": raw_items},
        "display_text": display_text,
    }

    writer({"type": "step_done", "step_result": step_result})

    return {
        "step_results": state["step_results"] + [step_result],
        "current_step_index": index + 1,
    }


def route_after_execute_step(state: ReasoningState) -> str:
    """条件边：还有步骤没执行完就继续循环execute_step，否则转synthesize。"""
    if state["current_step_index"] < len(state["plan"]):
        return "execute_step"
    return "synthesize"


def synthesize(state: ReasoningState) -> dict:
    """综合阶段：把全部step_results整合成最终建议，信息不全时标注不确定性。"""
    writer = get_stream_writer()
    writer({"type": "synthesizing_start"})

    result = synthesize_reasoning_suggestion(
        question=state["question"],
        step_results=state["step_results"],
    )

    writer(
        {
            "type": "synthesis_done",
            "final_suggestion": result["final_suggestion"],
            "confidence_note": result["confidence_note"],
        }
    )
    return {
        "final_suggestion": result["final_suggestion"],
        "confidence_note": result["confidence_note"],
    }
