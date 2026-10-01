"""
KamStoreAPI 的底层 HTTP 封装。

对应 03号设计文档 决策1/决策3/第六节：
  - 走自定义 REST 接口（不是 langgraph_sdk），因为 kam_agent 是纯 FastAPI 部署，
    不是 LangGraph Server/Platform 模式，没有官方 Store HTTP API 可用。
  - namespace 在 HTTP/JSON 层面只能是数组，这里统一接收 list/tuple 都行，
    对外发请求前强制转成 list（JSON 没有 tuple 类型，requests 序列化 tuple
    也会变成 JSON array，效果一样，这里显式转一下只是让类型更清楚）。
  - 错误处理原则（03号文档第六节）：连接失败/超时/4xx/5xx 全部让异常往上抛，
    不在这一层吞掉或悄悄转成 None——"kam_agent 挂了"和"数据确实不存在"
    是两种不同的情况，调用方需要能区分。
"""

import os
from typing import Any

import requests


class KamStoreAPIError(RuntimeError):
    """kam_agent 的 /store/* 接口调用失败时抛出（网络错误、超时、4xx/5xx）。"""


class StoreHTTPClient:
    """只管"怎么把一次 get/put/search 请求发给 kam_agent"，不含任何业务语义。

    KamStoreAPI 类（store_client/kam_store_api.py）在这之上包一层语义化方法名，
    这个类本身不直接暴露给 kam_admin 使用。
    """

    def __init__(
        self, base_url: str, timeout: float = 10.0, internal_api_key: str | None = None
    ):
        if not base_url:
            raise ValueError("base_url 不能为空（kam_agent 服务地址）")
        # 去掉结尾斜杠，避免拼接时出现 "//store/get" 这种双斜杠
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.internal_api_key = internal_api_key or os.getenv("INTERNAL_API_KEY")

    def _headers(self) -> dict[str, str]:
        """统一为 kam_agent 内部接口附加共享密钥。"""
        if not self.internal_api_key:
            return {}
        return {"X-Internal-API-Key": self.internal_api_key}

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            # 连接失败/超时等网络层错误，统一包一层更明确的异常类型再抛出，
            # 方便 kam_admin 侧用 except KamStoreAPIError 统一捕获。
            raise KamStoreAPIError(f"请求 kam_agent 失败：{url}，原因：{exc}") from exc

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KamStoreAPIError(
                f"kam_agent 返回错误状态码 {response.status_code}：{url}，"
                f"响应内容：{response.text}"
            ) from exc

        return response.json()

    def get(self, namespace: list[str] | tuple, key: str) -> dict | None:
        """对应 POST /store/get。返回 item 的 value dict，未命中返回 None。"""
        data = self._post("/store/get", {"namespace": list(namespace), "key": key})
        item = data.get("item")
        return item["value"] if item is not None else None

    def put(self, namespace: list[str] | tuple, key: str, value: dict[str, Any]) -> None:
        """对应 POST /store/put。直通写入，不含任何命名空间专属的合并规则
        （合并规则只存在于 kam_agent 侧的 store_client.py 内部，见 03号文档决策4）。"""
        self._post("/store/put", {"namespace": list(namespace), "key": key, "value": value})

    def post_json(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        """通用POST，供没有专属命名空间语义的接口使用（如资料库上传，
        见 KamStoreAPI.upload_kb_document）——知识库向量数据不在 Store
        的7个命名空间里，没法复用 get/put/search 这套 namespace/key 形状，
        直接透传路径和请求体给 kam_agent。

        timeout 可选覆盖默认值：资料上传要对每个切分片段真实调用一次
        embedding模型（真实验证时发现，文档片段多或模型刚启动
        还没加载好时可能耗时较久），用 get/put/search 那套10秒默认值
        （为快速识别"kam_agent挂了"设计的）会误判成超时失败。
        """
        return self._post(path, payload, timeout=timeout)

    def get_json(self, path: str) -> dict:
        """通用GET，与 post_json 配对（如资料库列表，见
        KamStoreAPI.list_kb_documents）。错误处理与 _post 保持一致——
        网络错误/4xx/5xx 统一包成 KamStoreAPIError 往上抛。"""
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise KamStoreAPIError(f"请求 kam_agent 失败：{url}，原因：{exc}") from exc

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KamStoreAPIError(
                f"kam_agent 返回错误状态码 {response.status_code}：{url}，"
                f"响应内容：{response.text}"
            ) from exc

        return response.json()

    def search(
        self,
        namespace: list[str] | tuple,
        filter: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """对应 POST /store/search。

        返回 value dict 列表，且每个 dict 里补一个 "_key" 字段存 item.key。

        补 "_key" 的原因（kam_admin 客户/订单/标签模块开工时才发现的缺口）：
        wxxd_order/tags_setting 两个命名空间的业务主键（order_id/tag_id）
        是 LangGraph Store 的 key，不在 value 内容里（见 kam_agent 侧
        store_client.py 的 upsert_wxxd_order/upsert_tag 写法）。之前这里
        只取 item["value"]，key 直接丢了，导致 search 出来的订单/标签
        数据没法标识"这是哪一条"。用 "_key" 前缀命名（不用裸 "key"）
        是为了跟业务字段明确区分，避免以后哪个命名空间自己也有个叫
        "key" 的字段时产生歧义。
        """
        data = self._post(
            "/store/search",
            {"namespace": list(namespace), "filter": filter, "limit": limit},
        )
        result = []
        for item in data.get("items", []):
            value = dict(item["value"])
            value["_key"] = item["key"]
            result.append(value)
        return result

    @staticmethod
    def _search_items_to_values(items: list[dict]) -> list[dict]:
        """把 Store search 返回的 item 列表转成带 `_key` 的业务值。"""
        result = []
        for item in items:
            value = dict(item["value"])
            value["_key"] = item["key"]
            result.append(value)
        return result

    def batch_get(self, requests_: list[dict[str, Any]]) -> list[dict | None]:
        """对应 POST /store/batch_get，返回与请求顺序一致的 value 列表。"""
        data = self._post("/store/batch_get", {"items": requests_})
        return [item["value"] if item is not None else None for item in data["items"]]

    def batch_search(self, requests_: list[dict[str, Any]]) -> list[list[dict]]:
        """对应 POST /store/batch_search，每个子列表对应一个 search 请求。"""
        data = self._post("/store/batch_search", {"items": requests_})
        return [self._search_items_to_values(items) for items in data["results"]]
