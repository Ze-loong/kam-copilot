"""
KamStoreAPI —— kam_admin 唯一应该 import 的门面类。

对应 03号设计文档 决策4 + 第四节方法清单。这里每个方法都很薄：拼 namespace、
调 StoreHTTPClient、把结果原样返回，不做业务逻辑判断（业务判断留给 kam_admin
或已经在 kam_agent 侧 store_client.py 里实现好的规则，比如画像合并逻辑）。

命名空间与 kam_agent 侧 store_client.py 一一对应（02号架构文档4.3节确认的
7个命名空间）：external_user / employee / tags_setting / wxkf_msg / wxxd_order /
wxqy_msg / external_user_profile。
"""

from typing import Any
from urllib.parse import quote

from store_client._http import StoreHTTPClient
from store_client.passwords import hash_password


def _sorted_from_to_key(id_a: str, id_b: str) -> str:
    """企微聊天消息双向查询分区键。

    必须与 kam_agent 侧 store_client.py 的同名函数保持完全一致的算法
    （"".join(sorted([...]))），两边是两个独立进程，没有代码复用关系，
    只能各自实现、约定好算法（见 03号设计文档 4.5节说明）。
    """
    return "".join(sorted([id_a, id_b]))


class KamStoreAPI:
    """封装对 kam_agent 7 个 Store 命名空间的读写调用。

    仅供 kam_admin 引用（02号架构文档2.1节：kam_client 是共享Python包，
    仅被 kam_admin 引用；kam_sidebar 场景单一，走 kam_agent 的 /tasks/* 任务型
    接口，不需要这个类）。
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._client = StoreHTTPClient(base_url, timeout=timeout)

    # ==================== employee 员工 ====================

    def get_employee(self, user_id: str) -> dict | None:
        return self._client.get(("employee",), user_id)

    def list_employees(self) -> list[dict]:
        # 列表场景不需要密码，在共享客户端层统一删除，
        # 防止后台其他页面无意间把密码哈希透传到浏览器。
        employees = self._client.search(("employee",))
        return [
            {key: value for key, value in employee.items() if key != "password"}
            for employee in employees
        ]

    def disable_employee(self, user_id: str) -> bool:
        """软禁用员工账号，保留历史客户归属和操作记录。"""
        employee = self.get_employee(user_id)
        if employee is None:
            return False
        disabled_employee = dict(employee)
        disabled_employee["disabled"] = True
        self._client.put(("employee",), user_id, disabled_employee)
        return True

    def upsert_employee(
        self, user_id: str, name: str, role: str, region: str, password: str
    ) -> None:
        """employee 命名空间没有合并逻辑，直通写入没有风险
        （对照 external_user_profile 不提供写方法，见下方说明）。

        `password` 传入参数是明文，但在本方法内立即转为带随机盐的
        PBKDF2-SHA256 哈希，Store 中不再写入新的明文密码。
        """
        self._client.put(
            ("employee",),
            user_id,
            {
                "user_id": user_id,
                "name": name,
                "role": role,
                "region": region,
                "password": hash_password(password),
                "disabled": False,
            },
        )

    # ==================== external_user 客户基础信息 ====================

    def get_external_user(self, follow_user_id: str, external_id: str) -> dict | None:
        return self._client.get(("external_user", follow_user_id), external_id)

    def list_external_users(self, follow_user_id: str) -> list[dict]:
        """按顾问查客户列表。kam_admin 侧还需结合 employee 的 role/region
        做权限过滤——过滤逻辑不在这里，属于 kam_admin 的职责（03号文档4.2节）。"""
        return self._client.search(("external_user", follow_user_id))

    def list_external_users_batch(self, follow_user_ids: list[str]) -> list[dict]:
        """一次 HTTP 请求批量获取多个顾问名下的客户。"""
        results = self._client.batch_search(
            [
                {"namespace": ["external_user", follow_user_id], "limit": 1000}
                for follow_user_id in follow_user_ids
            ]
        )
        customers = []
        for follow_user_id, items in zip(follow_user_ids, results, strict=True):
            for item in items:
                customer = dict(item)
                customer["follow_user_id"] = follow_user_id
                customers.append(customer)
        return customers

    # ==================== external_user_profile 客户画像（只读） ====================

    def get_customer_profile(self, follow_user_id: str, external_id: str) -> dict | None:
        """只读方法，故意不提供写方法。

        画像写入必须经过 kam_agent 内部 upsert_external_user_profile 的合并规则
        （已确认字段不被新草稿覆盖、confidence<=3自动标need_verify），如果这里
        暴露一个直通写方法，调用方可能误用它绕过合并规则、覆盖已确认字段。
        当前 kam_admin 功能范围不涉及画像写入（画像生成/确认全部在 kam_sidebar
        侧走 /tasks/* 完成），所以不提供，见03号设计文档4.3节 + 待确认事项。
        """
        return self._client.get(("external_user_profile", follow_user_id), external_id)

    def get_customer_profiles_batch(self, customers: list[dict]) -> list[dict | None]:
        """批量读取客户画像，返回顺序与 customers 一致。"""
        return self._client.batch_get(
            [
                {
                    "namespace": ["external_user_profile", customer["follow_user_id"]],
                    "key": customer["external_id"],
                }
                for customer in customers
            ]
        )

    # ==================== tags_setting 标签体系 ====================

    def get_tag(self, tag_id: str) -> dict | None:
        return self._client.get(("tags_setting",), tag_id)

    def get_tags(self, tag_ids: list[str]) -> list[dict]:
        """批量获取标签，忽略已删除或不存在的 tag_id。"""
        values = self._client.batch_get(
            [
                {"namespace": ["tags_setting"], "key": tag_id}
                for tag_id in tag_ids
            ]
        )
        return [value for value in values if value is not None]

    def list_tags(self) -> list[dict]:
        """返回的每个 dict 带 "_key" 字段，即 tag_id（tags_setting 的业务
        主键是 Store key，不在 value 内容里，见 _http.py search() 说明）。"""
        return self._client.search(("tags_setting",))

    def upsert_tag(
        self,
        tag_id: str,
        tag_name: str,
        deleted: bool,
        strategy_id: int,
        group_id: str,
        group_name: str,
    ) -> None:
        """tags_setting 是纯配置数据，没有草稿态/合并逻辑，直通写入没有风险。"""
        self._client.put(
            ("tags_setting",),
            tag_id,
            {
                "tag_name": tag_name,
                "deleted": deleted,
                "strategy_id": strategy_id,
                "group_id": group_id,
                "group_name": group_name,
            },
        )

    # ==================== wxqy_msg 企微聊天消息 ====================

    def list_chat_messages(self, follow_user_id: str, external_id: str) -> list[dict]:
        """双向查询：无论传参顺序是顾问在前还是客户在前，排序拼接后定位到
        同一分区（与 kam_agent 侧 _sorted_from_to_key 算法保持一致）。"""
        sorted_key = _sorted_from_to_key(follow_user_id, external_id)
        return self._client.search(("wxqy_msg", sorted_key))

    # ==================== wxkf_msg 微信客服消息 ====================

    def list_kf_messages(self, external_id: str) -> list[dict]:
        return self._client.search(("wxkf_msg", external_id))

    # ==================== wxxd_order 客户订单 ====================

    def list_orders(self, union_id: str) -> list[dict]:
        """返回的每个 dict 带 "_key" 字段，即 order_id（同上，order_id 是
        Store key，不在 value 内容里）。"""
        return self._client.search(("wxxd_order", union_id))

    def list_orders_batch(self, union_ids: list[str]) -> dict[str, list[dict]]:
        """一次 HTTP 请求批量获取多个客户的订单。"""
        unique_ids = list(dict.fromkeys(union_id for union_id in union_ids if union_id))
        results = self._client.batch_search(
            [
                {"namespace": ["wxxd_order", union_id], "limit": 1000}
                for union_id in unique_ids
            ]
        )
        return dict(zip(unique_ids, results, strict=True))

    # ==================== system_config 全局配置（F17关停开关） ====================

    def get_reasoning_enabled(self) -> bool:
        """F17跨模块推理的全局关停开关（02号架构文档7.5节）。命名空间/key
        与 kam_agent 侧 store_client.py 的同名函数保持完全一致
        （("system_config",) / "reasoning_enabled"，value形状{"enabled": bool}），
        没有记录时默认true——两边约定一致，不能只改一边。"""
        value = self._client.get(("system_config",), "reasoning_enabled")
        if value is None:
            return True
        return bool(value.get("enabled", True))

    def set_reasoning_enabled(self, enabled: bool) -> None:
        self._client.put(("system_config",), "reasoning_enabled", {"enabled": enabled})

    # ==================== 资料库管理（F16资料库，） ====================
    # 02号架构文档7.4节：kam_admin不直连向量库，只能把原始内容整个转发给
    # kam_agent的专属接口，由kam_agent完成切分/向量化/写入pgvector。

    def upload_kb_document(self, source_doc: str, category: str, content: str) -> int:
        """上传/替换一份资料文档。返回写入的片段数，供页面展示确认信息。

        替换语义（同名source_doc会先删旧片段再插入新的）由kam_agent侧
        ingest_document()内部实现，这里只是转发，不重复判断。

        timeout给到60秒（远高于本类其余方法用的默认10秒）——切分+对每个
        片段真实调用embedding模型比普通Store读写慢得多，真实
        验证时用默认10秒超时被误判成"kam_agent失败"，实际是embedding
        计算还没做完（见_http.py post_json的timeout参数说明）。
        """
        data = self._client.post_json(
            "/kb/documents",
            {"source_doc": source_doc, "category": category, "content": content},
            timeout=60.0,
        )
        return data["chunk_count"]

    def list_kb_documents(self) -> list[dict]:
        """查看当前资料库各类别文档列表，每条含
        category/source_doc/chunk_count/updated_at。"""
        return self._client.get_json("/kb/documents")["documents"]

    def get_kb_document_content(self, source_doc: str) -> str:
        """查看某一份资料文档的完整内容（各片段按顺序拼接），供 kam_admin
        资料库管理页"查看"功能使用（）。source_doc 是运营
        人员自定义的中文文档名（如"集团概况"），quote(safe="") 显式做一次
        URL编码再拼进路径——不依赖requests库对f-string里非ASCII字符的
        隐式编码行为，跟本项目其余地方"不确定的行为不能照抄，要用更保险的
        写法"这条工程习惯一致（见根目录任务清单.md"技术备忘"）。"""
        data = self._client.get_json(f"/kb/documents/{quote(source_doc, safe='')}")
        return data["content"]

    # ==================== 聚合查询：客户详情页 ====================

    def get_customer_detail(self, follow_user_id: str, external_id: str) -> dict[str, Any]:
        """客户详情页联合查画像+订单+标签（03号设计文档第五节）。

        这是 kam_client 存在的核心价值场景：kam_admin 的一次页面渲染需要
        4类数据，如果每个都单独调用、在路由函数里手动拼装，逻辑会散落在
        各处；这里统一组装好再返回一个 dict。

        组合逻辑仍放在调用方 kam_client；其中标签改用 batch_get，
        避免一个客户有多个标签时逐标签产生 N+1 HTTP 请求。
        """
        external_user = self.get_external_user(follow_user_id, external_id)
        profile = self.get_customer_profile(follow_user_id, external_id)

        union_id = external_user.get("union_id") if external_user else None
        orders = self.list_orders(union_id) if union_id else []

        tag_ids = external_user.get("tags", []) if external_user else []
        tags = self.get_tags(tag_ids)

        return {
            "external_user": external_user,
            "profile": profile,
            "orders": orders,
            "tags": tags,
        }


# 工厂函数，方便 kam_admin 侧统一创建实例
def create_kam_store_api(base_url: str, timeout: float = 10.0) -> KamStoreAPI:
    return KamStoreAPI(base_url, timeout=timeout)
