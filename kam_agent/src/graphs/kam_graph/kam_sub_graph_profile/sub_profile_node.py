"""
画像子图的节点函数。

 重构（recreate 多pending interrupt框架边界修复，见 实战记录.md 当天条目）：
子图不再处理"全部字段"，只处理"一个字段"——generate_all_drafts / dispatch_fields_for_confirm
两个节点已上移到主图 kam_node.py（generate_all_drafts 逻辑）+ 主图循环发起独立 invoke
（取代原来子图内部的 Send 扇出）。子图现在只剩 confirm_one_field 一个节点，
从 START 直接进来就是这一个字段的三元组，recreate 分支的"重新生成→再interrupt"
循环逻辑不变（因为这个子图执行实例从头到尾只有它自己这一个 interrupt，不会跟其他
字段的 pending interrupt 共存，之前卡住的框架边界在这个结构下不会被触发）。
"""

from langgraph.types import Command, Send, interrupt

from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_state import ProfileSubState
from src.llm.llm_suggest_profile import suggest_profile_draft
from src.models.kam_models import ConfirmAction, NEED_VERIFY_THRESHOLD
from src.store.store_client import upsert_external_user_profile


def regenerate_one_field(field_state: dict) -> Command:
    """
    重新生成当前字段的草稿（recreate 分支触发）。

    虽然 suggest_profile_draft 会返回全部字段，但本节点只取 field_name
    对应的一项，其他字段不会写回、不会影响——这个子图执行实例本来就只
    关心这一个字段，"全部字段"的草稿在主图层已经生成过一次了，这里只是
    recreate 时的重新生成，复用同一个 LLM 调用函数、只取需要的部分。
    """
    all_drafts = suggest_profile_draft(
        wxqy_msgs=field_state["wxqy_msgs"],
        wxkf_msgs=field_state["wxkf_msgs"],
        orders=field_state["orders"],
    )
    new_field_data = all_drafts.get(field_state["field_name"])

    if new_field_data is None:
        next_field_state = {
            **field_state,
            "source": (
                f'{field_state["source"]}；'
                "本次未能重新生成，保留原草稿"
            ),
        }
        return Command(goto=Send("confirm_one_field", next_field_state))

    new_status = new_field_data["status"]
    if new_field_data["confidence"] <= NEED_VERIFY_THRESHOLD:
        new_status = "need_verify"

    next_field_state = {
        **field_state,
        "value": new_field_data["value"],
        "confidence": new_field_data["confidence"],
        "source": new_field_data["source"],
        "status": new_status,
    }
    return Command(goto=Send("confirm_one_field", next_field_state))


def confirm_one_field(field_state: dict) -> dict:
    """
    循环体节点：处理"一个字段"的确认。

    这个子图执行实例从 START 进来就是这一个字段（不再经过 Send 扇出——
    子图入口本身就是单字段，见 sub_profile_graph.py 的 START 直连）。
    ok/discard 是终态，写 Store 后正常 return；recreate 走 Command(goto=Send(...))
    动态路由到 regenerate_one_field，重新生成后再 Send 回这里、再 interrupt 一次。
    """
    user_action = interrupt(
        {
            "field": field_state["field_name"],
            "value": field_state["value"],
            "confidence": field_state["confidence"],
            "source": field_state["source"],
            "status": field_state["status"],
        }
    )

    if user_action == ConfirmAction.OK:
        new_value = field_state["value"]
        new_status = "confirmed"

    elif user_action == ConfirmAction.DISCARD:
        new_value = ""
        new_status = "discarded"

    elif user_action == ConfirmAction.RECREATE:
        return Command(
            goto=Send("regenerate_one_field", dict(field_state))
        )

    else:
        raise ValueError(f"不支持的确认动作: {user_action}")

    updated_field = {
        "value": new_value,
        "confidence": field_state["confidence"],
        "source": field_state["source"],
        "status": new_status,
    }

    upsert_external_user_profile(
        follow_user_id=field_state["follow_user_id"],
        external_id=field_state["external_id"],
        new_profile_items={
            field_state["field_name"]: updated_field,
        },
    )

    return {
        "field_updates": {
            field_state["field_name"]: new_status,
        }
    }
