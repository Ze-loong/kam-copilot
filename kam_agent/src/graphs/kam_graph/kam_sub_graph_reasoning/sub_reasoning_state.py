"""
跨模块综合推理子图（F17）自己的 State——与主图 State、其他子图 State 都完全独立。

结构直接照抄 02 号架构设计文档 7.2 节已经定案的设计，没有新的设计判断，
这里只是把文档里的伪代码落成真正能跑的 TypedDict。

与 F16（kam_sub_graph_kb）的关键区别：F16 是"一次检索 + 一次生成"的两步
子图，F17 是"一次生成计划 + 循环执行N步 + 一次综合"的三段式子图，
中间的 execute_step 节点会在图里自我循环（对应 current_step_index 从 0
递增到 len(plan)），不是线性走一遍就结束。

与 F01/F04 的关键区别（架构文档已强调，这里复述一遍避免以后忘记）：
F17 产出的是一段建议文本，不写回 Store 任何业务字段，所以不需要
interrupt()——跟 F02/F03/F16 一样，一次 invoke() 跑到底就是终态。
"""

from typing import Literal, TypedDict

# 五个信息入口。solution_catalog（产品与解决方案）
# 复用 F16 知识库里 category="solution" 的内容，不是
# 独立数据源，见 kb_retriever.py 里 search_knowledge_base() 新增的
# category 参数说明。
ReasoningTool = Literal["knowledge_base", "profile", "order", "tag", "solution_catalog"]


class PlanStep(TypedDict):
    """generate_plan 节点产出的单个计划步骤。"""

    step_index: int
    tool: ReasoningTool
    reason: str  # AI为什么要查这一步，供侧边栏透明展示（01号文档"步骤透明"约束）


class StepResult(TypedDict):
    """execute_step 节点每执行完一步后追加的一条结果。"""

    step_index: int
    tool: str
    raw_result: dict  # 统一包成 {"items": ...} 形状，见 sub_reasoning_node.py 说明
    display_text: str  # 侧边栏展示的一行提示，如"已查询客户画像"


class ReasoningState(TypedDict):
    # 定位信息
    follow_user_id: str
    external_id: str

    # 输入
    question: str  # 客户原始问题（跨模块组合型问题）

    # 计划阶段产出（一次性生成，不再变更——不支持中途重新规划，见架构文档7.2节依据2/3）
    plan: list[PlanStep]

    # 执行阶段产出（逐步累加）
    step_results: list[StepResult]
    current_step_index: int  # 当前执行到第几步；等于len(plan)时循环结束（步数上限=计划长度）

    # 综合阶段产出
    final_suggestion: str | None
    confidence_note: str | None  # 不确定性标注，信息不全或某步结果薄弱时必填，否则为None
