"""一期标签互斥规则与老客订单接线测试。"""

from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace

from src.graphs.kam_graph import kam_node
from src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_graph import (
    build_kf_chat_suggestion_graph,
)
from src.llm.llm_suggest_tag import _enforce_single_select
from src.llm.llm_suggest_chat import suggest_kf_chat_reply


class SingleSelectTests(TestCase):
    def setUp(self):
        self.catalog = {
            tag_id: {"tag_name": tag_id, "group_id": group}
            for tag_id, group in {
                "intent_low": "intent", "intent_high": "intent",
                "intent_medium": "intent", "demand_aoi": "demand",
                "demand_agv": "demand", "concern_delivery": "concern",
                "concern_roi": "concern",
            }.items()
        }

    def item(self, tag_id):
        return {"tag_id": tag_id, "tag_name": tag_id, "reason": "客户明确提出"}

    def test_first_single_select_add_wins(self):
        add, _ = _enforce_single_select(
            [self.item("intent_high"), self.item("intent_medium")],
            [], set(), self.catalog,
        )
        self.assertEqual([item["tag_id"] for item in add], ["intent_high"])

    def test_old_single_select_is_removed(self):
        _, remove = _enforce_single_select(
            [self.item("intent_high")], [], {"intent_low"}, self.catalog,
        )
        self.assertEqual([item["tag_id"] for item in remove], ["intent_low"])

    def test_existing_removal_is_not_duplicated(self):
        _, remove = _enforce_single_select(
            [self.item("intent_high")], [self.item("intent_low")],
            {"intent_low"}, self.catalog,
        )
        self.assertEqual([item["tag_id"] for item in remove], ["intent_low"])

    def test_multiselect_groups_keep_all_additions(self):
        ids = ["demand_aoi", "demand_agv", "concern_delivery", "concern_roi"]
        add, _ = _enforce_single_select(
            [self.item(tag_id) for tag_id in ids], [], set(), self.catalog,
        )
        self.assertEqual([item["tag_id"] for item in add], ids)


class KfOrderWiringTests(TestCase):
    @patch("src.llm.llm_suggest_chat.get_llm")
    def test_order_name_reaches_actual_llm_prompt(self, mock_get_llm):
        mock_llm = mock_get_llm.return_value
        mock_llm.invoke.return_value = SimpleNamespace(content=(
            '{"suggestion_text":"请提供报警代码","reasoning":"核对设备"}'
        ))
        suggest_kf_chat_reply({}, [], [{"order_products": "AOI 视觉检测系统"}])
        self.assertIn("AOI 视觉检测系统", mock_llm.invoke.call_args.args[0][1][1])

    @patch("src.graphs.kam_graph.kam_sub_graph_kf_chat_suggestion.sub_kf_chat_suggestion_node.suggest_kf_chat_reply")
    def test_compiled_subgraph_passes_orders_to_llm(self, mock_suggest):
        mock_suggest.return_value = {"suggestion_text": "收到", "reasoning": "已核对订单"}
        orders = [{"product_name": "AOI 视觉检测系统"}]
        graph = build_kf_chat_suggestion_graph(checkpointer=None, store=None)
        result = graph.invoke({
            "follow_user_id": "u1", "external_id": "e1", "external_user": {},
            "wxkf_msgs": [{"content": "设备报警"}], "orders": orders,
            "suggestion_text": "", "reasoning": "",
        })
        self.assertEqual(result["suggestion_text"], "收到")
        self.assertEqual(mock_suggest.call_args.kwargs["orders"], orders)

    def test_main_graph_wrapper_passes_orders_to_subgraph(self):
        class FakeGraph:
            def invoke(self, state, config):
                self.state = state
                return {"suggestion_text": "收到", "reasoning": "已核对订单"}

        fake = FakeGraph()
        with patch.object(kam_node, "_kf_chat_suggestion_graph", fake):
            kam_node.call_kf_chat_suggestion_subgraph({
                "follow_user_id": "u1", "external_id": "e1",
                "_main_thread_id": "t1", "orders": [{"product_name": "AOI"}],
            })
        self.assertEqual(fake.state["orders"], [{"product_name": "AOI"}])
