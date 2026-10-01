"""数据范围过滤逻辑——"登录后能看到哪些数据"，区别于 auth.py 的
"能不能进这个页面"（04号设计文档"为什么把 auth.py 和 permissions.py
拆成两个文件"一节）。

对应 04号文档 决策3：应用层过滤（kam_admin 拿到 kam_client 返回的全量数据后，
用 Python 按 region/follow_user_id 筛选），不下沉到查询层，因为 LangGraph
Store 是简单键值存储、不支持 JOIN，下沉并不能真正省掉关联逻辑，只是把复杂度
挪到更难改的位置。

员工管理按角色限制入口；客户和订单接口调用这里的数据范围过滤逻辑。
"""

from store_client import KamStoreAPI


def get_visible_follow_user_ids(
    current_user: dict, client: KamStoreAPI
) -> list[str] | None:
    """返回当前用户能看到哪些顾问名下的客户数据。

    返回 None 表示不限制（super_admin，查全部顾问）。

    04号文档决策3示例代码的实现版本，三个角色分支：
      - super_admin：不限制，调用方自己决定"不限制"具体怎么处理
        （比如客户列表场景需要 list_employees() 拿全部 user_id 再逐个查，
        见04号文档第六节"super_admin 查全部客户没有原生查询方式"）。
      - regional_manager：只能看自己 region 内的顾问。
      - consultant：只能看自己。
    """
    role = current_user.get("role")
    if role == "super_admin":
        return None
    if role == "regional_manager":
        all_employees = client.list_employees()
        return [
            e["user_id"]
            for e in all_employees
            if e.get("region") == current_user.get("region")
        ]
    # consultant 及其他未知角色：保守处理，只能看自己
    return [current_user["user_id"]]
