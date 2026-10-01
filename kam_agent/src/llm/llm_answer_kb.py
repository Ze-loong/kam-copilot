"""F16知识库问答（据实回答 + 兜底话术）的 LLM 调用与结果校验。

对应01号需求文档3.7.1节"检索到匹配内容据实回答，检索不到或匹配度不达标走
兜底话术；兜底话术需嵌入顾问姓名，不允许无资料支撑时自由发挥"。

三个设计判断：

1. **据实回答的prompt沿用全项目CoT风格**（先梳理哪些片段真正相关，再给
   结论），与F01/F02/F04保持一致——即使这一步的"判断"比F02的"决策阶段
   诊断"简单得多（hit_knowledge_base已经在retrieve_kb_chunks节点判完了），
   仍照做，理由是统一全项目prompt风格降低维护成本，而不是这一步本身
   需要多复杂的推理。

2. **兜底话术固定模板+姓名替换，不调用LLM**。兜底场景本身就是"AI没有
   把握"，用LLM生成反而会引入不必要的不确定性，跟"绝不允许自由发挥"
   这条硬约束的精神相悖；固定模板还省了一次LLM调用的延迟和token成本。

3. **cited_sources由LLM自报实际引用的来源，而不是把检索到的chunks来源
   全部列出**。retrieve_kb_chunks节点的命中判断只看分数最高那一条（见
   sub_kb_node.py的KB_HIT_THRESHOLD逻辑），但top_k=3意味着不管命中与否，
   传进来的chunks永远是3条，后两条经常是分数明显更低、跟问题其实不太
   相关的"陪跑"结果——全部列出会让cited_sources失真，损害顾问对"引用
   来源"这个功能的信任。代价是要多做一层白名单校验，防止LLM编造一个
   不在chunks里的来源（见_validate_kb_answer_payload）。

本文件raise/print的文字统一用英文，不用中文——调试
verify_kb_pipeline.py时真的因为print()里一个Unicode对勾符号在Windows
终端（GBK编码）崩过一次，为了不再踩同类坑，运行时会打印到终端的文字
（不含发给客户的业务文案，业务文案该用中文还是用中文）统一改英文。
"""

import json
import os
import re
import sys
from typing import TypedDict

from src.llm.llm_setting import get_llm
from src.llm.json_retry import invoke_and_parse_json
from src.store.store_client import get_employee

_DEBUG_LLM_RAW = os.environ.get("KAM_DEBUG_LLM_RAW") == "1"


class KbAnswerResult(TypedDict):
    answer_text: str
    cited_sources: list[str]


def _extract_json_block(text: str) -> dict:
    """从 LLM 回复中取出 JSON 代码块；没有代码块时按整段 JSON 解析。

    与 llm_suggest_chat.py / llm_suggest_tag.py 的同名函数完全一致，
    复用同一个正则约定（```json 代码块优先，没有则整段尝试解析）。
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    json_text = match.group(1) if match else text
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("DeepSeek response is not valid JSON") from error


def _get_advisor_name(follow_user_id: str) -> str | None:
    """查 employee 命名空间拿顾问姓名，查不到时返回None而不是崩溃或占位文案。

    兜底话术本身就是"系统能力不够时的最后一道保险"，不能因为员工数据
    缺失又让这一步本身崩掉——那样客户会收到接口报错，而不是任何回复。
    返回None（而不是"您的客户经理"这类占位字符串）是因为调用方
    _build_fallback_answer需要区分"没查到姓名"和"查到了具体姓名"两种
    情况来组织不同的句子，如果这里直接把占位文案当成"姓名"返回，
    会在模板里被拼接两次（"您的客户经理您的客户经理对这方面很熟悉"），
    这是本文件第一版真实跑通验证时（知识库问答实验用例3）实际
    暴露出的问题，不是假设性风险。
    """
    employee = get_employee(follow_user_id)
    employee_value = employee.value if hasattr(employee, "value") else employee
    if not employee_value:
        return None
    return employee_value.get("name") or None


def _build_fallback_answer(follow_user_id: str) -> KbAnswerResult:
    """固定模板 + 姓名替换，不调用LLM（设计判断2，见文件头说明）。

    cited_sources固定为空列表：兜底话术没有引用任何资料，不存在"引用
    来源"这个概念。
    """
    advisor_name = _get_advisor_name(follow_user_id)
    if advisor_name:
        answer_text = (
            f"这个问题我需要进一步确认，您的客户经理{advisor_name}对这方面很熟悉，"
            f"会尽快和您详细沟通。"
        )
    else:
        answer_text = "这个问题我需要进一步确认，您的客户经理对这方面很熟悉，会尽快和您详细沟通。"
    return {"answer_text": answer_text, "cited_sources": []}


def _validate_kb_answer_payload(payload: dict, valid_sources: set[str]) -> KbAnswerResult | None:
    """校验LLM输出的answer_text/cited_sources两个字段。

    cited_sources采用"过滤不合法项，而不是整体拒绝"的容错策略：LLM报出
    一个不在valid_sources白名单里的来源（编造、或者记错成别的资料名），
    只丢弃这一条，不让整个回答失败——answer_text本身的可信度不依赖
    cited_sources是否精确，为了引用来源的小瑕疵让客户收不到任何回复，
    得不偿失。
    """
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek response for KB answer must be a JSON object")

    # 第二道判断：检索分数过了阈值，但模型逐条看完片段后认为答不上。
    # 返回 None 交给调用方改走兜底话术，保证"答不上"时客户看到的永远是
    # 同一套带客户经理姓名的标准话术，而不是模型临场组织的"暂未找到"。
    if payload.get("answerable") is False:
        return None

    answer_text = payload.get("answer_text")
    if not isinstance(answer_text, str) or not answer_text.strip():
        raise ValueError("DeepSeek did not return a valid answer_text")

    raw_sources = payload.get("cited_sources")
    if not isinstance(raw_sources, list):
        raise ValueError("DeepSeek did not return a valid cited_sources (must be a list)")

    cited_sources = [s for s in raw_sources if isinstance(s, str) and s in valid_sources]

    return {"answer_text": answer_text.strip(), "cited_sources": cited_sources}


def answer_from_kb(
    question: str,
    chunks: list[dict],
    hit_knowledge_base: bool,
    follow_user_id: str,
) -> KbAnswerResult:
    """
    根据检索结果生成知识库回答。

    Args:
        question: 客户原始问题
        chunks: retrieve_kb_chunks节点检索出的片段列表，
                每条是 {"content": ..., "source_doc": ..., "score": ...}
        hit_knowledge_base: 是否达到相似度阈值，决定走据实回答还是兜底话术
        follow_user_id: 当前顾问ID，兜底话术需要查出顾问姓名嵌入话术里

    Returns:
        {"answer_text": ..., "cited_sources": [...]}
    """
    if not hit_knowledge_base:
        return _build_fallback_answer(follow_user_id)

    valid_sources = {chunk["source_doc"] for chunk in chunks}
    chunks_text = "\n\n".join(
        f"[来源：{chunk['source_doc']}（相似度{chunk['score']:.2f}）]\n{chunk['content']}"
        for chunk in chunks
    )

    system_prompt = """
你是制造业大客户销售团队的知识库问答助手。客户通过企业微信向销售顾问提出问题，
系统已经从集团资料库里检索出若干相关片段，你的任务是基于这些片段据实回答，附带引用来源。

# 分析步骤（必须先做，再给结论，不能跳过）
1. 逐条查看下面提供的资料片段，判断每一条是否真的能用来回答客户的问题（检索
   是按相似度返回的，不代表每一条都真正相关，需要你自己甄别）。
2. 基于真正相关的片段，组织出一段完整、专业、可以直接发给客户的回答。

# 事实边界（硬约束，不允许违反）
- 只能使用下面提供的资料片段作答，不能添加片段之外的信息，也不能用常识或行业
  惯例去"合理推测"资料没有明确写出的内容。
- 资料中的交付周期、节拍、效果数据等都是通用参考值或案例数据，回答时要保留
  "典型""参考""以正式方案/合同为准"这类限定，不能改写成对客户的承诺。
- 如果所有片段其实都答不上客户的问题，把 answerable 设为 false（系统会改用
  标准兜底话术转交客户经理），不要为了给出回答而牵强凑内容。
- 不使用"据我所知""通常来说""一般情况下"这类暗示是常识判断、而非依据资料作答
  的措辞。

# 输出格式
先用一小段文字完成上面的"分析步骤"（哪些片段真正相关、为什么），随后只输出一个
JSON 代码块：
```json
{
  "answerable": true,
  "answer_text": "可以直接发送给客户的完整回答；answerable 为 false 时填空字符串",
  "cited_sources": ["实际参考的资料来源标识，只列真正用到的片段"]
}
```
代码块内只保留这三个字段，cited_sources 里的值必须是下面片段列表中出现过的
source_doc 原文，不能自己编造。
"""

    user_prompt = f"""
【客户问题】
{question}

【检索到的资料片段】
{chunks_text}

请先完成分析步骤，再严格按指定 JSON 格式输出。
"""

    payload = invoke_and_parse_json(
        [("system", system_prompt), ("human", user_prompt)], _extract_json_block, model=get_llm()
    )
    result = _validate_kb_answer_payload(payload, valid_sources)
    if result is None:
        return _build_fallback_answer(follow_user_id)
    return result
