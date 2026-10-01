"""
recreate 分支的图级回归测试。

使用 InMemorySaver / InMemoryStore 模拟真实 LangGraph 的 checkpoint 与 Store，
不连接 PostgreSQL、不调用真实 LLM。

2026-08-11 重构后，子图入口直接是单字段三元组（不再是原始聊天素材+
generate_all_drafts），子图从 START 直连 confirm_one_field。这个测试
验证的核心链路不变：
旧草稿 interrupt → recreate → 只重新生成当前字段 → 新 interrupt
→ 确认新草稿 → Store 最终写入新值。

尤其防止"节点恢复重跑时再次调 LLM，导致前端看到的草稿和最终确认值不一致"。
"""

from unittest import TestCase
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_graph import (
    build_profile_graph,
)


class RecreateGraphFlowTests(TestCase):
    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "upsert_external_user_profile"
    )
    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "suggest_profile_draft"
    )
    def test_recreate_generates_new_draft_then_waits_for_new_confirmation(
        self,
        mock_suggest,
        mock_upsert,
    ):
        # regenerate_one_field 触发时才会调用一次 suggest_profile_draft，
        # 子图入口不再调用它（草稿已经在主图层生成好、作为输入传进来）。
        mock_suggest.return_value = {
            "tech_goal": {
                "value": "先做单线试点，再推广到全厂",
                "confidence": 8,
                "source": "重新生成的草稿",
                "status": "draft",
            },
        }

        graph = build_profile_graph(
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
        )
        config = {"configurable": {"thread_id": "recreate-flow-test:tech_goal"}}

        # 第一次运行：子图入口直接是这个字段的旧草稿，拿到 interrupt。
        initial_result = graph.invoke(
            {
                "follow_user_id": "u001",
                "external_id": "ext_001",
                "field_name": "tech_goal",
                "value": "提升产线良率",
                "confidence": 2,
                "source": "初始草稿",
                "status": "draft",
                "wxqy_msgs": [{"content": "目标提升产线良率"}],
                "wxkf_msgs": [],
                "orders": [],
            },
            config=config,
        )
        first_interrupt = initial_result["__interrupt__"][0]

        self.assertEqual(first_interrupt.value["value"], "提升产线良率")
        mock_suggest.assert_not_called()

        # 对旧草稿点击 recreate：应重生并暂停在新草稿。
        recreated_result = graph.invoke(
            Command(resume={first_interrupt.id: "recreate"}),
            config=config,
        )
        second_interrupt = recreated_result["__interrupt__"][0]

        self.assertNotEqual(second_interrupt.id, first_interrupt.id)
        self.assertEqual(
            second_interrupt.value["value"],
            "先做单线试点，再推广到全厂",
        )
        self.assertEqual(second_interrupt.value["status"], "draft")
        mock_suggest.assert_called_once()

        # 确认新草稿：最终写入的必须是新值，而不是旧值。
        graph.invoke(
            Command(resume={second_interrupt.id: "ok"}),
            config=config,
        )

        saved_tech_goal = mock_upsert.call_args.kwargs["new_profile_items"]["tech_goal"]
        self.assertEqual(saved_tech_goal["value"], "先做单线试点，再推广到全厂")
        self.assertEqual(saved_tech_goal["status"], "confirmed")
