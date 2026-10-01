"""F17跨模块综合推理（生成计划 + 综合建议）的 LLM 调用与结果校验。

对应架构设计文档 7.2 节"Plan-and-Execute LangGraph子图结构"，State/节点
结构在架构评审阶段定案，本文件只负责其中两个LLM调用节点（generate_plan/
synthesize）各自的prompt设计与输出校验。

两个函数的设计说明：

1. **generate_reasoning_plan**：让LLM从5个固定信息来源（对应ReasoningTool）
   里选出真正需要查的几项、给出理由，不需要CoT——这一步本身就是"决策"而非
   "转述"，直接让LLM输出结构化选择即可。输出包成`{"plan": [...]}`而不是裸数组，
   是为了复用全项目统一的`_extract_json_block`正则约定（该正则只认顶层JSON
   对象），避免为了一个数组输出单独写一套提取逻辑。

2. **synthesize_reasoning_suggestion**：沿用全项目CoT风格（先梳理每步查询
   结果里哪些真正有用，再给结论），这一步理由更充分——真的需要"综合判断"，
   跟F16"据实回答"节点不同（F16判断已经在retrieve节点做完，generate节点是
   转述；这里generate_plan已经决定查什么，但"综合"这一步本身就是推理动作）。

本文件raise/print的文字统一用英文，理由与llm_answer_kb.py一致：避免Windows
终端（GBK编码）打印非GBK字符时崩溃，业务文案不受影响，该中文还是中文。
"""

import json
import os
import re
import sys

from src.graphs.kam_graph.kam_sub_graph_reasoning.sub_reasoning_state import PlanStep
from src.llm.llm_setting import get_llm
from src.llm.json_retry import invoke_and_parse_json

_DEBUG_LLM_RAW = os.environ.get("KAM_DEBUG_LLM_RAW") == "1"

_VALID_TOOLS = {"knowledge_base", "profile", "order", "tag", "solution_catalog"}
# 只有5个固定信息来源，一份计划最多5步（多了必然是重复选择，属于LLM输出异常，
# 截断保护而非正常业务场景）。
_MAX_PLAN_STEPS = 5


def _extract_json_block(text: str) -> dict:
    """从 LLM 回复中取出 JSON 代码块；没有代码块时按整段 JSON 解析。

    与 llm_suggest_chat.py / llm_answer_kb.py 的同名函数完全一致，复用
    同一个正则约定（```json 代码块优先，没有则整段尝试解析）。
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    json_text = match.group(1) if match else text
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek response is not valid JSON") from error


def _validate_plan_payload(payload: dict) -> list[PlanStep]:
    """校验LLM输出的plan数组，过滤掉tool不在白名单/reason为空的无效项。

    采用"过滤无效项而不是整体拒绝"的策略（与llm_answer_kb.py的cited_sources
    校验同一思路）：LLM在5步计划里有1步写错了tool名，不应该让整个计划作废，
    只丢弃那一步。但如果过滤完一步都不剩，说明这次输出质量太差，直接报错
    比硬造一个空计划更安全（空计划会导致synthesize阶段无米下锅）。
    """
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek response for reasoning plan must be a JSON object")

    raw_steps = payload.get("plan")
    if not isinstance(raw_steps, list):
        raise ValueError("DeepSeek did not return a valid plan (must be a list)")

    valid_steps: list[PlanStep] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        reason = item.get("reason")
        if tool not in _VALID_TOOLS:
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        valid_steps.append(
            {"step_index": len(valid_steps), "tool": tool, "reason": reason.strip()}
        )
        if len(valid_steps) >= _MAX_PLAN_STEPS:
            break

    if not valid_steps:
        raise ValueError("DeepSeek reasoning plan contained no valid steps")

    return valid_steps


def generate_reasoning_plan(question: str) -> list[PlanStep]:
    """根据客户问题，生成一份"查哪些信息、为什么查"的计划。

    对应架构文档7.2节`generate_plan`节点。计划生成后不再变更（不支持中途
    重新规划，见架构文档7.2节选型依据2/3），所以这一步要一次想清楚。
    """
    system_prompt = """
你是制造业大客户销售团队 AI 助手的"任务规划"模块。客户向销售顾问提出了一个需要综合
多方面信息才能回答的问题，你的任务是判断回答这个问题需要查哪些信息、按什么顺序查，
输出一份查询计划——不需要真的去查，后续步骤会执行。

# 可选的信息来源（只能从这5个里选，不能杜撰其他来源）
- knowledge_base：集团资料库（集团概况、资质与标杆案例、常见问题 FAQ）
- solution_catalog：产品与解决方案目录（各类设备/系统的标准配置、适用场景、典型交付周期）
- profile：客户画像（AI 此前分析出的企业规模、产线现状、技改目标、预算、决策链等）
- order：订单记录（客户已采购的设备、系统和维保合同）
- tag：客户标签（行业、企业类型、意向等级、客户价值等分类标签）

# 规划原则
1. 只选真正回答这个问题需要的信息来源，不要为了"全面"把5个全选上。
2. 每一步给出简短理由，说明为什么需要查这个来源——这段话会展示给顾问看，
   要写成自然的口语化表述（比如"先看看客户之前买过哪些设备"），不要写成
   "调用order工具获取数据"这类技术术语。
3. 步骤之间没有先后依赖关系（不存在"必须先查A才能判断要不要查B"的情况），
   排序按"从客户背景到方案细节"的自然顺序即可。

# 输出格式
只输出一个 JSON 代码块：
```json
{
  "plan": [
    {"tool": "order", "reason": "先看看客户之前采购过哪些设备"},
    {"tool": "solution_catalog", "reason": "再查一下扩产适用的方案和交付周期"}
  ]
}
```
tool 的值必须是上面5个来源标识之一，不能自己编。
"""

    user_prompt = f"""
【客户问题】
{question}

请输出查询计划。
"""

    payload = invoke_and_parse_json(
        [("system", system_prompt), ("human", user_prompt)], _extract_json_block, model=get_llm()
    )
    return _validate_plan_payload(payload)


def _validate_synthesis_payload(payload: dict) -> dict:
    """校验LLM输出的final_suggestion/confidence_note两个字段。

    confidence_note允许为空字符串（信息充分、不需要标注不确定性），这里
    统一归一化成None，方便下游判断"有没有标注"时用`is not None`而不是
    还要额外判断空字符串。
    """
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek response for reasoning synthesis must be a JSON object")

    final_suggestion = payload.get("final_suggestion")
    if not isinstance(final_suggestion, str) or not final_suggestion.strip():
        raise ValueError("DeepSeek did not return a valid final_suggestion")

    confidence_note = payload.get("confidence_note")
    if isinstance(confidence_note, str) and confidence_note.strip():
        confidence_note = confidence_note.strip()
    else:
        confidence_note = None

    return {"final_suggestion": final_suggestion.strip(), "confidence_note": confidence_note}


def _step_results_to_text(step_results: list[dict]) -> str:
    """把step_results格式化成prompt里可读的文本块。"""
    if not step_results:
        return "（没有任何查询结果）"
    blocks = []
    for result in step_results:
        items = result["raw_result"].get("items")
        items_text = json.dumps(items, ensure_ascii=False, default=str)
        blocks.append(
            f"[第{result['step_index'] + 1}步 · {result['tool']}]\n"
            f"{result['display_text']}\n"
            f"查询结果：{items_text}"
        )
    return "\n\n".join(blocks)


def synthesize_reasoning_suggestion(question: str, step_results: list[dict]) -> dict:
    """把全部step_results整合成一段完整建议，信息不全时标注不确定性。

    对应架构文档7.2节`synthesize`节点。这一步是真正的"综合判断"，采用
    全项目CoT风格（先梳理每步结果里哪些真正有用，再给结论）。
    """
    system_prompt = """
你是制造业大客户销售团队 AI 助手的"综合建议"模块。前面的步骤已经按计划查询了若干项
信息，你的任务是把这些信息整合成一段完整的建议，供销售顾问参考后决定要不要发给客户。

# 分析步骤（必须先做，再给结论，不能跳过）
1. 逐项查看下面每个步骤查到的信息，判断哪些是真正有用的、哪些查询结果为空
   或明显不相关。
2. 基于真正有用的信息，站在资深大客户顾问的角度，组织出一段完整的建议——不是简单
   罗列查到了什么，而是像一个有经验的顾问看完这些信息后会怎么回应客户、下一步怎么推进。

# 事实边界（硬约束，不允许违反）
- 只能基于下面提供的查询结果组织建议，不能编造查询结果里没有的具体信息
  （比如具体报价、折扣、交期承诺、性能指标、案例客户名称，查询结果里没有就不要编）。
- 资料里的"典型交付周期""参考节拍"等是通用参考值，引用时必须说明"以现场勘查和正式合同为准"，
  不能说成对这个客户的承诺。
- 如果某些步骤查询结果为空或明显不足以支撑一个完整判断，不要假装信息齐全，
  必须在 confidence_note 里如实说明，例如"客户画像中没有预算信息，建议先确认预算范围再发送"
  （对应"不确定性标注"这条硬约束）。
- 如果全部步骤查询结果都不足以给出有意义的建议，如实在 final_suggestion
  里说明信息不足，不要牵强凑一段建议。

# 输出格式
先用一小段文字完成上面的"分析步骤"，随后只输出一个 JSON 代码块：
```json
{
  "final_suggestion": "给顾问看的完整建议文本",
  "confidence_note": "信息不全时必填的提醒文字；信息充分时填空字符串"
}
```
代码块内只保留这两个字段。
"""

    user_prompt = f"""
【客户问题】
{question}

【已执行的查询步骤及结果】
{_step_results_to_text(step_results)}

请先完成分析步骤，再严格按指定 JSON 格式输出综合建议。
"""

    payload = invoke_and_parse_json(
        [("system", system_prompt), ("human", user_prompt)], _extract_json_block, model=get_llm()
    )
    return _validate_synthesis_payload(payload)
