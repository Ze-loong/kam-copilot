"""
客服聊天回复建议子图的节点函数。结构与 sub_chat_suggestion_node.py（销售
场景）结构对称。老客应答使用客服聊天和订单里的设备、维保事实，不使用销售画像。
"""

from src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_state import (
    KfChatSuggestionState,
)
from src.llm.llm_suggest_chat import suggest_kf_chat_reply


def generate_kf_chat_suggestion(state: KfChatSuggestionState) -> dict:
    """调LLM生成客服应答建议+推理说明，一次性产出终态，不涉及交互。"""
    result = suggest_kf_chat_reply(
        external_user=state.get("external_user"),
        wxkf_msgs=state.get("wxkf_msgs", []),
        orders=state.get("orders", []),
    )
    return {
        "suggestion_text": result["suggestion_text"],
        "reasoning": result["reasoning"],
    }
