"""
公用数据结构定义。
对应设计文档 02-架构设计文档.md 第四节的画像置信度五元组，
以及 store_client.py 里各命名空间的 value 结构。

这里只定义"跨子图共用"的数据形状，各子图专属的 State 不放这里
（画像子图的 State 见 kam_sub_graph_profile/sub_profile_state.py）。
"""

from typing import Literal, TypedDict

# 字段状态：draft: AI 初次建议，尚未确认 / need_verify:低置信度，必须重点核实 / confirmed:顾问确认，后续 AI 不得覆盖
# discarded :顾问明确放弃这条建议 / need_regenerate:顾问要求这条字段重新生成
FieldStatus = Literal[
    "draft",
    "need_verify",
    "confirmed",
    "discarded",
    "need_regenerate",
]

# confidence <= 此值时，upsert_external_user_profile 会自动打 need_verify（见 02 号文档 4.1 节）
NEED_VERIFY_THRESHOLD = 3


class ProfileFieldItem(TypedDict):
    """画像单个字段的三元组结构：值 / 置信度 / 来源，外加 status。"""
    value: str
    confidence: int | None
    source: str | None
    status: FieldStatus


class ConfirmAction:
    """/tasks/confirm 接口 action 字段的三个合法取值（02 号文档 5.1.3 节）。"""
    OK = "ok"
    DISCARD = "discard"
    RECREATE = "recreate"
