"""
主图第一个节点："加载数据"。

公用步骤（03号文档已定：属于三个子图共同的前置步骤，不属于任何一个子图），
从 Store 里把这个客户相关的数据都读出来，塞进主图 State，后面的意图识别 /
各子图都从主图 State 里取，不用各自重复查库。

这里是工程性代码（纯粹的"调用几个已有的store_client函数、组装成dict"），
集中加载主图所需的业务数据。
"""

from src.store.store_client import (
    get_external_user,
    get_external_user_profile,
    search_wxkf_msg,
    search_wxqy_msg,
    search_wxxd_order,
)


def load_data(state: dict) -> dict:
    follow_user_id = state["follow_user_id"]
    external_id = state["external_id"]

    external_user = get_external_user(follow_user_id, external_id)
    profile = get_external_user_profile(follow_user_id, external_id)
    wxqy_msgs = search_wxqy_msg(follow_user_id, external_id)
    wxkf_msgs = search_wxkf_msg(external_id)

    # union_id 要先从 external_user 里拿到才能查订单
    union_id = external_user.value["union_id"] if external_user else None
    orders = search_wxxd_order(union_id) if union_id else []

    return {
        "external_user": external_user,
        "profile": profile,
        "wxqy_msgs": wxqy_msgs,
        "wxkf_msgs": wxkf_msgs,
        "orders": orders,
    }
