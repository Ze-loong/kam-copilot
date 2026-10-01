"""
客服聊天回复建议子图自己的 State。

老客服务应答使用客户信息、客服聊天与订单记录（设备采购和维保历史）。
不传销售画像，避免把推断当成售后事实；新客和老客子图的数据来源不同。
"""

from typing import TypedDict


class KfChatSuggestionState(TypedDict):
    follow_user_id: str
    external_id: str

    external_user: dict | None
    wxkf_msgs: list       # 客服聊天记录，来源于企业微信「微信客服」功能，与 wxqy_msgs 是不同命名空间
    orders: list          # 已采购设备与维保记录

    suggestion_text: str
    reasoning: str
