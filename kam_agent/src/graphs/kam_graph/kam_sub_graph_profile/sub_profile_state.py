"""
画像子图自己的 State —— 与主图 State 完全独立（不共享同一个 TypedDict）。

 重构（recreate 多pending interrupt框架边界修复，见 实战记录.md 当天条目）：
子图不再一次性处理全部字段（不再有 generate_all_drafts / dispatch_fields_for_confirm
两个节点），改成"只处理一个字段"的单字段子图——generate_all_drafts 上移到主图层
的 kam_node.py，一次 LLM 调用生成全部字段草稿后，主图对每个字段各自发起一次独立
thread_id 的子图 invoke()，这个子图从 START 到 END 全程只处理 1 个字段。

这么改的原因：LangGraph 在"同一个子图执行实例内，多个字段的 interrupt 同时
pending，此时恢复其中一个、该分支又动态 Send 产生新的 interrupt"这个组合场景下，
新产生的 interrupt 不会正确出现在当次 invoke() 返回值里（已用最小复现脚本
repro_parallel_dynamic_interrupt.py 组D 真实验证坐实，官方"挂载子图"方式一样会踩，
不是手动invoke独有问题）。既然每个子图执行实例只有1个字段、永远不会有"多个
pending interrupt共存"这个前提条件，recreate 内部的动态 Send 循环就是安全的。
"""

from typing import TypedDict

from src.models.kam_models import ProfileFieldItem


class ProfileSubState(TypedDict):
    # 定位信息：写回 Store 时需要
    follow_user_id: str
    external_id: str

    # 主图 generate_all_drafts 已经生成好的这一个字段的草稿三元组，
    # 子图入口直接拿到手，不用再自己调 LLM 生成初稿
    field_name: str
    value: str
    confidence: int | None
    source: str | None
    status: str

    # recreate 重新生成时需要的原始素材（跟主图 load_data 查出来的是同一批，
    # 由主图在发起这个字段的独立 invoke 时一并传入，子图不重复查库）
    wxqy_msgs: list
    wxkf_msgs: list
    orders: list

    # confirm_one_field 处理完之后的最终结果（ok/discard 才会走到这里）
    field_updates: dict[str, str]
