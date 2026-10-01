"""
标签推荐子图的节点函数。

与画像子图（kam_sub_graph_profile）的关键区别：
- 没有recreate分支，不需要 Command(goto=Send(...)) 动态路由那一套。
- 一次 invoke() 只产生 1 条 interrupt（整批标签列表打包成一个 value），
  不是"每个字段各自的子图实例各产生1条"。
- 因为没有recreate，也就不会出现"同一执行实例多个interrupt同时pending"，
  天然不会触碰 F01 当初踩的那个 LangGraph 框架边界，不需要"多条推荐各自
  再单独拆一个thread_id"这种设计。

注意：以上说的是"不需要按推荐条数/字段数拆分多个thread_id"，不代表
"可以直接复用主图自己的thread_id"——这是两件独立的事。这个子图调用方
（kam_node.py的call_tag_subgraph）仍然必须给它一个和主图不同的thread_id
（f"{main_thread_id}:tag"），否则会和主图自己的checkpoint写在同一个位置
互相覆盖（真实联调时复现过这个问题，add_recommendations在
resume后读出来是空的，根因就是这个）。

设计判断过程见 设计文档/08-F04标签推荐设计文档.md。
"""

import os
import sys

from langgraph.types import interrupt

from src.graphs.kam_graph.kam_sub_graph_tag.sub_tag_state import TagSubState
from src.llm.llm_suggest_tag import suggest_tag_recommendations
from src.store.store_client import get_external_user, upsert_external_user


def generate_tag_recommendations(state: TagSubState) -> dict:
    """
    调LLM生成整批标签推荐（新增+移除两组），不涉及交互。

    suggest_tag_recommendations() 的入参是 current_tags/tag_catalog 等具体
    字段，不是整个 state——从 state 里手动取出来传进去。它返回的字典键名
    是 "add"/"remove"（不是 "add_recommendations"/"remove_recommendations"，
    与本子图 State 的字段名不同），这里做一次手动映射，不能直接 **result 展开。
    """
    result = suggest_tag_recommendations(
        current_tags=state["current_tags"],
        tag_catalog=state["tag_catalog"],
        wxqy_msgs=state["wxqy_msgs"],
        wxkf_msgs=state["wxkf_msgs"],
        orders=state["orders"],
    )
    return {
        "add_recommendations": result["add"],
        "remove_recommendations": result["remove"],
    }


def _validate_confirmed_ids(
    confirmed_add_tag_ids: list,
    confirmed_remove_tag_ids: list,
    add_recommendations: list,
    remove_recommendations: list,
) -> tuple[list[str], list[str]]:
    """
    后端二次校验前端提交的确认结果，不盲信前端传来的id列表。

    校验规则：
    - confirmed_add_tag_ids 必须是本轮 add_recommendations 的子集；
    - confirmed_remove_tag_ids 必须是本轮 remove_recommendations 的子集；
    - 两组不能重叠——正常情况下 add/remove 候选池本来就互斥（生成阶段
      llm_suggest_tag.py 里 add 只能从"客户当前没有的标签"选、remove
      只能从"客户当前已有的标签"选，两个候选池天然不相交），这里重复
      校验是防御性的：万一前端传了脏数据、或者以后交互逻辑改了，这层
      还能兜底。如果真出现同一个id同时在两组里，两组都丢弃这个id
      （意图有歧义，不猜测哪个是用户的真实意图，直接不生效更安全）。
    - 不合法的id直接过滤掉，不抛异常——前端传来的可能只是"这次提交漏选/
      多选了几个"，属于正常的用户交互结果，不是系统性错误，没必要中断
      整个确认流程去报错。
    """
    if not isinstance(confirmed_add_tag_ids, list):
        confirmed_add_tag_ids = []
    if not isinstance(confirmed_remove_tag_ids, list):
        confirmed_remove_tag_ids = []

    allowed_add_ids = {item["tag_id"] for item in add_recommendations}
    allowed_remove_ids = {item["tag_id"] for item in remove_recommendations}

    valid_add = {tid for tid in confirmed_add_tag_ids if tid in allowed_add_ids}
    valid_remove = {tid for tid in confirmed_remove_tag_ids if tid in allowed_remove_ids}

    # 两组不能重叠：理论上不会发生（候选池天然互斥），出现则两边都丢弃
    overlap = valid_add & valid_remove
    if overlap:
        valid_add -= overlap
        valid_remove -= overlap

    return sorted(valid_add), sorted(valid_remove)


def confirm_tag_batch(state: TagSubState) -> dict:
    """
    整批interrupt一次：把两组推荐列表打包成一个value抛给前端。

    resume 时前端提交的是"最终确认的 add/remove 标签id列表"（已经是前端
    根据勾选框状态整理好的业务结果，不是原始的勾选框dict）——这个职责划分
    职责划分是：前端负责"用户选了什么"，后端负责"这个选择能不能
    执行"，不能反过来让子图去猜UI控件状态属于哪一组。resume 的 value
    直接是提交结果的 dict，不像F01那样是单个"ok/discard/recreate"字符串。
    """
    submission = interrupt(
        {
            "add_recommendations": state["add_recommendations"],
            "remove_recommendations": state["remove_recommendations"],
        }
    )

    # 仅开发环境诊断用（KAM_DEBUG_LLM_RAW=1 时打印，复用 llm_suggest_tag.py
    # 同一个环境变量开关，避免又加一个新变量）：resume 后打印 submission 和
    # state 里的推荐列表，用来确认这次 resume 拿到的 checkpoint 是否还保留
    # 着 add_recommendations—— 真实联调排查出的 thread_id 撞车
    # 问题（详见本函数调用方 kam_node.py call_tag_subgraph 的说明）就是靠
    # 这类打印定位到的。
    if os.environ.get("KAM_DEBUG_LLM_RAW") == "1":
        print(
            f"[confirm_tag_batch DEBUG] submission = {submission!r}\n"
            f"[confirm_tag_batch DEBUG] state add_recommendations = {state['add_recommendations']!r}\n"
            f"[confirm_tag_batch DEBUG] state remove_recommendations = {state['remove_recommendations']!r}\n",
            file=sys.stderr,
        )

    confirmed_add_tag_ids, confirmed_remove_tag_ids = _validate_confirmed_ids(
        submission.get("confirmed_add_tag_ids", []),
        submission.get("confirmed_remove_tag_ids", []),
        state["add_recommendations"],
        state["remove_recommendations"],
    )

    if os.environ.get("KAM_DEBUG_LLM_RAW") == "1":
        print(
            f"[confirm_tag_batch DEBUG] validated add = {confirmed_add_tag_ids!r}\n"
            f"[confirm_tag_batch DEBUG] validated remove = {confirmed_remove_tag_ids!r}\n",
            file=sys.stderr,
        )

    # 写回 Store：在当前 tags 基础上加上确认新增的、去掉确认移除的
    external_user = get_external_user(state["follow_user_id"], state["external_id"])
    current_tags = set(external_user.value["tags"]) if external_user else set()
    new_tags = (current_tags | set(confirmed_add_tag_ids)) - set(confirmed_remove_tag_ids)

    if external_user:
        upsert_external_user(
            external_id=state["external_id"],
            union_id=external_user.value["union_id"],
            follow_user_id=state["follow_user_id"],
            name=external_user.value["name"],
            remark_name=external_user.value["remark_name"],
            tags=list(new_tags),
        )

    # 当前仅更新 Store；企微标签同步属于 F15 规划，见 08 号设计文档。

    return {
        "confirmed_add_tag_ids": confirmed_add_tag_ids,
        "confirmed_remove_tag_ids": confirmed_remove_tag_ids,
    }
