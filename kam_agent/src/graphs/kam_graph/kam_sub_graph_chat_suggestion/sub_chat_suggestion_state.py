"""
销售聊天回复建议子图自己的 State —— 与主图 State、其他子图 State 都完全独立。

对比 TagSubState（F04）：没有 recommendations/confirmed_xxx_ids 这类
"推荐-确认"配对字段，也没有 interrupt 相关的任何东西——F02没有interrupt，
检索后由 generate 节点产出终态。设计判断过程见
设计文档/09-F02F03回复建议设计文档.md 第三节。
"""

from typing import TypedDict


class ChatSuggestionState(TypedDict):
    # 定位信息
    follow_user_id: str
    external_id: str

    # 生成建议需要的素材：SRS 3.2.1 所需数据输入——
    # 员工信息、聊天历史、订单历史、客户信息、客户标签、客户画像。
    # （员工信息/客户信息目前项目里没有单独的"员工画像"结构，暂不单独引入，
    # external_user 里已经带 tags；如果后续发现员工信息确实用得上，
    # 再补充字段，不在本轮无依据地预留）
    external_user: dict | None
    profile: dict | None
    wxqy_msgs: list      # 销售聊天记录（企业微信），与客服场景的 wxkf_msgs 不同来源
    orders: list
    solution_refs: list  # 按客户近期消息检索的产品与解决方案片段

    # LLM生成结果：建议文本 + 推理说明，两部分对应 NFR-U02
    suggestion_text: str
    reasoning: str
