"""
销售聊天回复建议子图的节点函数。

与F04(标签)/F01(画像)的关键区别：没有interrupt，没有确认
闭环——先检索产品方案，再由generate节点产出终态，不需要confirm节点、不需要处理
resume。设计判断过程见 设计文档/09-F02F03回复建议设计文档.md 第二节
判断点2/4。

llm_suggest_chat.py 负责生成回复建议，这里只负责组装入参/取出结果。
"""

import logging

from src.graphs.kam_graph.kam_sub_graph_kb.sub_kb_node import KB_HIT_THRESHOLD
from src.kb.kb_retriever import search_knowledge_base
from src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_state import (
    ChatSuggestionState,
)
from src.llm.llm_suggest_chat import suggest_chat_reply

logger = logging.getLogger(__name__)


def retrieve_solution_refs(state: ChatSuggestionState) -> dict:
    """用客户最近三条企微消息检索产品方案；检索异常时继续生成建议。"""
    external_id = state.get("external_id")
    messages = [
        item.value if hasattr(item, "value") else item
        for item in state.get("wxqy_msgs", [])
    ]
    customer_messages = [
        item for item in messages
        if isinstance(item, dict) and item.get("from_id") == external_id
        and isinstance(item.get("content"), str) and item["content"].strip()
    ]
    customer_messages.sort(key=lambda item: item.get("msg_time", ""))
    query = "\n".join(item["content"] for item in customer_messages[-3:])
    if not query:
        return {"solution_refs": []}
    try:
        chunks = search_knowledge_base(query=query, top_k=3, category="solution")
    except Exception:
        logger.exception("Solution retrieval failed; continuing without references")
        return {"solution_refs": []}
    return {"solution_refs": [
        chunk for chunk in chunks if chunk.get("score", 0) >= KB_HIT_THRESHOLD
    ]}


def generate_chat_suggestion(state: ChatSuggestionState) -> dict:
    """
    调LLM生成回复建议+推理说明，一次性产出终态，不涉及交互。
    """
    result = suggest_chat_reply(
        external_user=state.get("external_user"),
        profile=state.get("profile"),
        wxqy_msgs=state.get("wxqy_msgs", []),
        orders=state.get("orders", []),
        solution_refs=state.get("solution_refs", []),
    )
    return {
        "suggestion_text": result["suggestion_text"],
        "reasoning": result["reasoning"],
    }
