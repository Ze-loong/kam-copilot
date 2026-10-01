"""
kam_sidebar 对 kam_agent 的全部调用都收口在这个文件。

对应 06号设计文档 决策1（登录）/决策2（SSE消费再转发）：
  - kam_sidebar 不引用 kam_client（那是 kam_admin 专属的共享包，见02号架构文档2.1节），
    这里是 kam_sidebar 内部自己的一层薄封装，不是给别的服务复用的公共包。
  - 登录查 employee：直接调 kam_agent 已有的 /store/get 通用接口，
    与 kam_client 的 StoreHTTPClient.get() 做的事情一样，但代码独立实现
    （两个服务没有共享代码层，06号文档决策1已说明这不是需要解决的重复）。
  - SSE 转发：消费 kam_agent 的 /tasks/stream/{thread_id}，解析成 Python
    对象后重新组织再转发给前端，不是逐字节透传（06号文档决策2的理由）。

错误处理原则：与 kam_client/_http.py 保持同一条原则——网络错误/超时/4xx/5xx
全部包成 KamAgentAPIError 往上抛，不在这一层吞掉，让路由函数决定怎么给前端
返回错误信息。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx


class KamAgentAPIError(RuntimeError):
    """调用 kam_agent 失败时抛出（网络错误、超时、4xx/5xx）。"""


class KamAgentClient:
    """kam_sidebar 唯一应该用来跟 kam_agent 打交道的类。"""

    def __init__(
        self, base_url: str, timeout: float = 10.0, internal_api_key: str | None = None
    ):
        if not base_url:
            raise ValueError("base_url 不能为空（kam_agent 服务地址）")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.internal_api_key = internal_api_key or os.getenv("INTERNAL_API_KEY")

    def _internal_headers(self) -> dict[str, str]:
        """为 Store 内部接口附加共享密钥。"""
        if not self.internal_api_key:
            return {}
        return {"X-Internal-API-Key": self.internal_api_key}

    # ==================== 登录：查 employee 校验密码 ====================
    # 06号文档决策1：与 kam_admin 同款逻辑，但独立实现，走 /store/get 通用接口。

    async def get_employee(self, user_id: str) -> dict | None:
        """查 ("employee",) 命名空间。返回 None 表示这个 user_id 不存在。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/store/get",
                    json={"namespace": ["employee"], "key": user_id},
                    headers=self._internal_headers(),
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /store/get 失败：{exc}") from exc

            data = resp.json()
            item = data.get("item")
            return item["value"] if item is not None else None

    # ==================== /tasks/generate_profile ====================

    async def generate_profile(self, follow_user_id: str, external_id: str) -> str:
        """发起画像生成任务，返回主 thread_id。对应 02号文档5.1.1节。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/generate_profile",
                    json={"follow_user_id": follow_user_id, "external_id": external_id},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/generate_profile 失败：{exc}") from exc

            return resp.json()["thread_id"]

    # ==================== /tasks/stream/{thread_id}（消费再转发） ====================

    async def stream_profile_events(
        self, thread_id: str, follow_user_id: str, external_id: str
    ) -> AsyncIterator[dict]:
        """
        消费 kam_agent 的 SSE，逐条 yield 解析后的事件字典 {"event": ..., "data": {...}}。

        06号文档决策2：不做逐字节透传。这里用 httpx 的流式请求 + 手写 SSE 协议
        解析（"event: xxx" 和 "data: {...}" 两行一组，空行分隔事件），因为
        httpx 本身不自带 SSE 客户端。解析出来的 dict 交给调用方（main.py 的
        路由函数）决定怎么重新格式化、要不要补充字段再发给浏览器。
        """
        url = f"{self.base_url}/tasks/stream/{thread_id}"
        params = {"follow_user_id": follow_user_id, "external_id": external_id}

        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", url, params=params) as resp:
                    resp.raise_for_status()

                    event_name = None
                    data_lines: list[str] = []

                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:"):].strip())
                        elif line == "":
                            # 空行 = 一条事件结束（SSE 协议约定）
                            if event_name is not None:
                                raw_data = "".join(data_lines)
                                try:
                                    data = json.loads(raw_data) if raw_data else {}
                                except json.JSONDecodeError:
                                    data = {"_raw": raw_data}
                                yield {"event": event_name, "data": data}
                            event_name = None
                            data_lines = []
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/stream 失败：{exc}") from exc

    # ==================== /tasks/confirm（透传） ====================

    async def confirm_field(
        self,
        thread_id: str,
        follow_user_id: str,
        external_id: str,
        field: str,
        interrupt_id: str,
        action: str,
    ) -> dict:
        """透传给 kam_agent 的 /tasks/confirm，原样返回响应体（含 recreate 场景
        的新草稿，见02号文档5.1.3节）。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/confirm",
                    json={
                        "thread_id": thread_id,
                        "follow_user_id": follow_user_id,
                        "external_id": external_id,
                        "field": field,
                        "interrupt_id": interrupt_id,
                        "action": action,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/confirm 失败：{exc}") from exc

            return resp.json()


    # ==================== /tasks/generate_tags ====================
    #  F04收尾：与画像三件套结构完全对称（generate/stream/confirm），
    # 但内部语义不同——标签是"整批一次interrupt"，不是逐字段循环（见
    # 设计文档/08-F04标签推荐设计文档.md）。confirm_tags 提交的是两个id
    # 列表而不是单个action，SSE只会收到1条 tag_batch_update + done，
    # 不是像画像那样N条 profile_field_update。

    async def generate_tags(self, follow_user_id: str, external_id: str) -> str:
        """发起标签推荐任务，返回主 thread_id。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/generate_tags",
                    json={"follow_user_id": follow_user_id, "external_id": external_id},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/generate_tags 失败：{exc}") from exc

            return resp.json()["thread_id"]

    # ==================== /tasks/stream_tags/{thread_id}（消费再转发） ====================

    async def stream_tag_events(
        self, thread_id: str, follow_user_id: str, external_id: str
    ) -> AsyncIterator[dict]:
        """消费 kam_agent 的标签推荐 SSE，逐条 yield 解析后的事件字典。
        和 stream_profile_events 用同一套手写 SSE 协议解析逻辑，只是
        URL 换成 /tasks/stream_tags/{thread_id}。"""
        url = f"{self.base_url}/tasks/stream_tags/{thread_id}"
        params = {"follow_user_id": follow_user_id, "external_id": external_id}

        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", url, params=params) as resp:
                    resp.raise_for_status()

                    event_name = None
                    data_lines: list[str] = []

                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:"):].strip())
                        elif line == "":
                            if event_name is not None:
                                raw_data = "".join(data_lines)
                                try:
                                    data = json.loads(raw_data) if raw_data else {}
                                except json.JSONDecodeError:
                                    data = {"_raw": raw_data}
                                yield {"event": event_name, "data": data}
                            event_name = None
                            data_lines = []
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/stream_tags 失败：{exc}") from exc

    # ==================== /tasks/confirm_tags（透传） ====================

    async def confirm_tags(
        self,
        thread_id: str,
        follow_user_id: str,
        external_id: str,
        interrupt_id: str,
        confirmed_add_tag_ids: list[str],
        confirmed_remove_tag_ids: list[str],
    ) -> dict:
        """透传给 kam_agent 的 /tasks/confirm_tags。thread_id 必须是SSE事件
        里 tag_batch_update.thread_id 带的那个"标签子图自己的" thread_id
        （格式 f"{主thread_id}:tag"），不是 done 事件里的主 thread_id——
        这是真实联调排查出的坑（详见 实战记录.md 当天条目），
        传错会导致 confirm_tags resume 失败。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/confirm_tags",
                    json={
                        "thread_id": thread_id,
                        "follow_user_id": follow_user_id,
                        "external_id": external_id,
                        "interrupt_id": interrupt_id,
                        "confirmed_add_tag_ids": confirmed_add_tag_ids,
                        "confirmed_remove_tag_ids": confirmed_remove_tag_ids,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/confirm_tags 失败：{exc}") from exc

            return resp.json()


    # ==================== F02/F03 回复建议（ 接入） ====================
    # 与画像/标签三件套的关键区别：kam_agent 端这两个接口没有interrupt，一次
    # POST 请求-响应就是完整交互，不需要"发号→SSE流式→confirm"三段式，这里
    # 也就不需要 generate_xxx()+stream_xxx_events()+confirm_xxx() 三个方法，
    # 一个方法直接对应一次调用（见 09号设计文档 判断点4、判断点2）。

    async def generate_chat_suggestion(self, follow_user_id: str, external_id: str) -> dict:
        """F02.1 销售聊天回复建议：同步请求-响应，直接返回 {suggestion_text, reasoning}。"""
        # 首次检索会加载本地向量模型，不能使用普通 Store 请求的 10 秒上限。
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/generate_chat_suggestion",
                    json={"follow_user_id": follow_user_id, "external_id": external_id},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(
                    f"请求 kam_agent /tasks/generate_chat_suggestion 失败：{exc}"
                ) from exc

            return resp.json()

    async def generate_kf_chat_suggestion(self, follow_user_id: str, external_id: str) -> dict:
        """F02.2 客服聊天回复建议：结构与 generate_chat_suggestion 完全对称，
        只是打到 kam_agent 的另一个接口。"""
        # 客服建议同样需要等待模型生成，避免正常的慢响应被当作接口故障。
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/generate_kf_chat_suggestion",
                    json={"follow_user_id": follow_user_id, "external_id": external_id},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(
                    f"请求 kam_agent /tasks/generate_kf_chat_suggestion 失败：{exc}"
                ) from exc

            return resp.json()

    # ==================== F16 知识库问答（ 从 src/ 平行实现移植） ====================
    # 与F02/F03同一种"一次POST请求-响应"结构：kam_agent的/tasks/generate_kb_answer
    # 没有interrupt，不需要SSE。external_id仍要传——kam_agent主图的load_data
    # 节点对所有intent统一无条件执行（见kam_graph.py），需要一个有效
    # external_id才能正常跑完这一步，不是F16业务逻辑本身需要客户数据。

    async def generate_kb_answer(self, follow_user_id: str, external_id: str, question: str) -> dict:
        """F16 知识库问答：同步返回 {answer_text, cited_sources}。"""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/generate_kb_answer",
                    json={
                        "follow_user_id": follow_user_id,
                        "external_id": external_id,
                        "question": question,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(
                    f"请求 kam_agent /tasks/generate_kb_answer 失败：{exc}"
                ) from exc

            return resp.json()

    # ==================== F17 跨模块综合推理（ 从 src/ 平行实现移植） ====================
    # 两段式设计，跟画像/标签一样：generate只发号不执行，SSE连上后kam_agent才
    # 真正调用reasoning_graph.stream(..., stream_mode="custom")——真实耗时
    # 20+秒，避免"POST已跑完、SSE还没连上、事件被漏掉"的竞态。question无法
    # 放进generate请求体持久化（kam_agent这一步本来就不执行，没地方存），
    # 改为SSE连接时通过query string原样透传，与follow_user_id/external_id
    # 处理方式一致。

    async def generate_reasoning(self, follow_user_id: str, external_id: str, question: str) -> str:
        """只发号，返回主 thread_id。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/tasks/generate_reasoning",
                    json={
                        "follow_user_id": follow_user_id,
                        "external_id": external_id,
                        "question": question,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(
                    f"请求 kam_agent /tasks/generate_reasoning 失败：{exc}"
                ) from exc

            return resp.json()["thread_id"]

    async def stream_reasoning_events(
        self, thread_id: str, follow_user_id: str, external_id: str, question: str
    ) -> AsyncIterator[dict]:
        """消费 kam_agent 的F17推理SSE，逐条yield解析后的事件字典。跟
        stream_profile_events/stream_tag_events同一套手写SSE协议解析逻辑，
        只是多一个question query参数。"""
        url = f"{self.base_url}/tasks/stream_reasoning/{thread_id}"
        params = {
            "follow_user_id": follow_user_id,
            "external_id": external_id,
            "question": question,
        }

        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", url, params=params) as resp:
                    resp.raise_for_status()

                    event_name = None
                    data_lines: list[str] = []

                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:"):].strip())
                        elif line == "":
                            if event_name is not None:
                                raw_data = "".join(data_lines)
                                try:
                                    data = json.loads(raw_data) if raw_data else {}
                                except json.JSONDecodeError:
                                    data = {"_raw": raw_data}
                                yield {"event": event_name, "data": data}
                            event_name = None
                            data_lines = []
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /tasks/stream_reasoning 失败：{exc}") from exc

    async def get_customers(self, follow_user_id: str) -> dict:
        """读取当前顾问名下客户列表。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/tasks/customers",
                    params={"follow_user_id": follow_user_id},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(
                    f"请求 kam_agent /tasks/customers 失败：{exc}"
                ) from exc
            return resp.json()

    async def get_chat_history(
        self, follow_user_id: str, external_id: str, channel: str
    ) -> dict:
        """读取并返回已经归一化的聊天历史。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/tasks/chat_history",
                    params={
                        "follow_user_id": follow_user_id,
                        "external_id": external_id,
                        "channel": channel,
                    },
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(
                    f"请求 kam_agent /tasks/chat_history 失败：{exc}"
                ) from exc
            return resp.json()

    # ==================== 画像只读查看（） ====================
    # 与 generate_profile+stream_profile_events 那条"重新生成"路径完全独立：
    # 这里走 kam_agent 已有的 /store/get 通用接口直接读，不触发LLM、不产生
    # 新草稿、不进interrupt确认流程。用途：“沟通记录”tab右侧客户信息卡片
    # 需要一个"看一眼已确认画像"的入口，此前只能去"客户画像"tab重新生成一遍
    # 才能看到字段内容（详见任务清单.md当天条目）。

    async def get_customer_profile(self, follow_user_id: str, external_id: str) -> dict | None:
        """查 ("external_user_profile", follow_user_id) 命名空间。返回的 value
        形状是 {"profile_items": {字段名: {value/confidence/source/status}},
        "timeline": [...], "updated_at": ...}（与 kam_agent 侧 store_client.py
        的 upsert_external_user_profile 写入结构一致）。返回 None 表示这个
        客户还没有任何画像记录。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/store/get",
                    json={"namespace": ["external_user_profile", follow_user_id], "key": external_id},
                    headers=self._internal_headers(),
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise KamAgentAPIError(f"请求 kam_agent /store/get 失败：{exc}") from exc

            data = resp.json()
            item = data.get("item")
            return item["value"] if item is not None else None


def create_kam_agent_client(base_url: str, timeout: float = 10.0) -> KamAgentClient:
    return KamAgentClient(base_url, timeout=timeout)
