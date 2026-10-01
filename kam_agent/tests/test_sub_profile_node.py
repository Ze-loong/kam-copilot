"""
画像子图节点的单元测试：单字段确认（ok/discard/recreate三分支）与重生。

2026-08-11 重构后，子图只剩 confirm_one_field / regenerate_one_field 两个节点
（generate_all_drafts 上移到主图 kam_node.py，改名 generate_all_field_drafts，
对应测试见 test_kam_node.py；dispatch_fields_for_confirm 已删除，主图循环
对每个字段各自发起独立 thread_id 的子图 invoke 取代了子图内部的 Send 扇出）。
"""

from unittest import TestCase
from unittest.mock import patch

from langgraph.types import Command, Send

from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node import (
    confirm_one_field,
    regenerate_one_field,
)


class ConfirmOneFieldTests(TestCase):
    def setUp(self):
        self.field_state = {
            "follow_user_id": "u001",
            "external_id": "ext_001",
            "field_name": "tech_goal",
            "wxqy_msgs": [{"content": "客户已立项，计划提升产线良率"}],
            "wxkf_msgs": [{"content": "咨询设备报警处理"}],
            "orders": [],
            "value": "提升产线良率",
            "confidence": 2,
            "source": "模拟聊天记录",
            "status": "need_verify",
        }

    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "upsert_external_user_profile"
    )
    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "interrupt"
    )
    def test_ok_confirms_field(self, mock_interrupt, mock_upsert):
        mock_interrupt.return_value = "ok"

        result = confirm_one_field(self.field_state)

        self.assertEqual(
            result["field_updates"]["tech_goal"],
            "confirmed",
        )

        saved_item = mock_upsert.call_args.kwargs["new_profile_items"]["tech_goal"]
        self.assertEqual(saved_item["status"], "confirmed")
        self.assertEqual(saved_item["value"], "提升产线良率")

    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "upsert_external_user_profile"
    )
    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "interrupt"
    )
    def test_discard_clears_field_value(self, mock_interrupt, mock_upsert):
        mock_interrupt.return_value = "discard"

        result = confirm_one_field(self.field_state)

        self.assertEqual(
            result["field_updates"]["tech_goal"],
            "discarded",
        )

        saved_item = mock_upsert.call_args.kwargs["new_profile_items"]["tech_goal"]
        self.assertEqual(saved_item["status"], "discarded")
        self.assertEqual(saved_item["value"], "")

    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "upsert_external_user_profile"
    )
    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "interrupt"
    )
    def test_recreate_routes_to_regeneration_node(
        self,
        mock_interrupt,
        mock_upsert,
    ):
        mock_interrupt.return_value = "recreate"

        result = confirm_one_field(self.field_state)

        self.assertIsInstance(result, Command)
        self.assertIsInstance(result.goto, Send)
        self.assertEqual(result.goto.node, "regenerate_one_field")
        self.assertEqual(result.goto.arg, self.field_state)
        mock_upsert.assert_not_called()

    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "suggest_profile_draft"
    )
    def test_regenerate_uses_new_draft_for_current_field(self, mock_suggest):
        mock_suggest.return_value = {
            "tech_goal": {
                "value": "先做单线试点，再推广到全厂",
                "confidence": 2,
                "source": "重新分析后的聊天记录",
                "status": "draft",
            },
            "project_stage": {
                "value": "方案评估中",
                "confidence": 9,
                "source": "无关字段，不应采用",
                "status": "draft",
            },
        }

        result = regenerate_one_field(self.field_state)

        self.assertIsInstance(result, Command)
        self.assertIsInstance(result.goto, Send)
        self.assertEqual(result.goto.node, "confirm_one_field")
        self.assertEqual(result.goto.arg["field_name"], "tech_goal")
        self.assertEqual(result.goto.arg["value"], "先做单线试点，再推广到全厂")
        self.assertEqual(result.goto.arg["confidence"], 2)
        self.assertEqual(result.goto.arg["source"], "重新分析后的聊天记录")
        self.assertEqual(result.goto.arg["status"], "need_verify")

        mock_suggest.assert_called_once_with(
            wxqy_msgs=[{"content": "客户已立项，计划提升产线良率"}],
            wxkf_msgs=[{"content": "咨询设备报警处理"}],
            orders=[],
        )

    @patch(
        "src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node."
        "suggest_profile_draft"
    )
    def test_regenerate_keeps_old_draft_when_field_is_missing(
        self,
        mock_suggest,
    ):
        mock_suggest.return_value = {
            "project_stage": {
                "value": "方案评估中",
                "confidence": 9,
                "source": "只生成了别的字段",
                "status": "draft",
            },
        }

        result = regenerate_one_field(self.field_state)

        self.assertIsInstance(result, Command)
        self.assertIsInstance(result.goto, Send)
        self.assertEqual(result.goto.node, "confirm_one_field")
        self.assertEqual(
            result.goto.arg["source"],
            "模拟聊天记录；本次未能重新生成，保留原草稿",
        )
        self.assertEqual(result.goto.arg["value"], "提升产线良率")
        self.assertEqual(result.goto.arg["confidence"], 2)
        self.assertEqual(result.goto.arg["status"], "need_verify")
