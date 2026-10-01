"""批量生成"展示用"画像+标签 —— 给5个mock客户跑一遍真实F01/F04链路。

背景：mock_data_expand_customers.py（kam_agent那边）只写了聊天记录，画像
和标签本来就是顾问按需触发生成的（不是预置数据），这是既有设计，跟原来
演示客户需要有可展示的画像和标签，因此挑选若干客户跑一遍真实
LLM生成+确认，让demo数据看起来更完整。

复用 kam_sidebar 已有、已验证过的 `kam_agent_client.py`（跟sidebar浏览器
操作走的是同一套HTTP接口，不是另外发明一套调用方式）：
  画像：generate_profile → 消费SSE拿到每个字段的 draft+interrupt_id →
        对每个字段都用 action="ok" 确认（这是批量生成脚本，不是真的顾问
        在做判断，全部接受LLM草稿）。
  标签：generate_tags → 消费SSE拿到批量推荐 → confirm_tags 全部新增、
        不移除（这些客户之前没有标签，remove列表天然为空）。

运行前提：kam_agent 必须已经在 127.0.0.1:8000 跑着（真实调用LLM，不是
mock）。运行方式（kam_sidebar 目录下）：
    uv run python -m experiments.generate_showcase_profiles

会真实消耗 DEEPSEEK_API_KEY 的调用额度：5个客户 × (1次画像生成 + 约5次
字段确认 + 1次标签生成 + 1次标签确认)，都是真实网络请求，不是空跑。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kam_agent_client import KamAgentAPIError, create_kam_agent_client

FOLLOW_USER_ID = "demo_emp_xiaozhang"

# 演示客户应覆盖不同的技改阶段、预算顾虑与维保场景。
SHOWCASE_EXTERNAL_IDS = [
    "demo_ext_zhaonvshi",
    "demo_ext_sunxiansheng",
    "demo_ext_wuxiansheng",
    "demo_ext_yangnvshi",
    "demo_ext_hexiansheng",
]


async def generate_profile_for(client, external_id: str) -> int:
    """跑完一个客户的画像生成+全部字段确认，返回确认的字段数。"""
    thread_id = await client.generate_profile(FOLLOW_USER_ID, external_id)

    fields = []
    async for evt in client.stream_profile_events(thread_id, FOLLOW_USER_ID, external_id):
        if evt["event"] == "profile_field_update":
            fields.append(evt["data"])
        elif evt["event"] == "error":
            raise KamAgentAPIError(f"profile stream error: {evt['data']}")

    for field_data in fields:
        await client.confirm_field(
            thread_id=field_data["thread_id"],
            follow_user_id=FOLLOW_USER_ID,
            external_id=external_id,
            field=field_data["field"],
            interrupt_id=field_data["interrupt_id"],
            action="ok",
        )

    return len(fields)


async def generate_tags_for(client, external_id: str) -> int:
    """跑完一个客户的标签生成+确认（全部接受新增推荐），返回新增标签数。"""
    thread_id = await client.generate_tags(FOLLOW_USER_ID, external_id)

    batch = None
    async for evt in client.stream_tag_events(thread_id, FOLLOW_USER_ID, external_id):
        if evt["event"] == "tag_batch_update":
            batch = evt["data"]
        elif evt["event"] == "error":
            raise KamAgentAPIError(f"tag stream error: {evt['data']}")

    if batch is None:
        return 0

    add_ids = [item["tag_id"] for item in batch.get("add_recommendations", [])]
    if not add_ids and not batch.get("remove_recommendations"):
        return 0

    result = await client.confirm_tags(
        thread_id=batch["thread_id"],
        follow_user_id=FOLLOW_USER_ID,
        external_id=external_id,
        interrupt_id=batch["interrupt_id"],
        confirmed_add_tag_ids=add_ids,
        confirmed_remove_tag_ids=[],
    )
    return len(result.get("confirmed_add_tag_ids", []))


async def main() -> None:
    base_url = os.environ.get("KAM_AGENT_BASE_URL", "http://127.0.0.1:8000")
    client = create_kam_agent_client(base_url=base_url, timeout=60.0)

    print(f"Target kam_agent: {base_url}")
    print(f"Customers to process: {len(SHOWCASE_EXTERNAL_IDS)}")

    for external_id in SHOWCASE_EXTERNAL_IDS:
        print(f"\n=== {external_id} ===")
        try:
            n_fields = await generate_profile_for(client, external_id)
            print(f"  profile: {n_fields} fields confirmed (action=ok)")
        except KamAgentAPIError as exc:
            print(f"  profile FAILED: {exc}")
            continue

        try:
            n_tags = await generate_tags_for(client, external_id)
            print(f"  tags: {n_tags} tags confirmed")
        except KamAgentAPIError as exc:
            print(f"  tags FAILED: {exc}")

    print("\nDone. Re-check via kam_sidebar '沟通记录' tab (has_profile badge + 标签 count).")


if __name__ == "__main__":
    asyncio.run(main())
