"""
主图节点函数的单元测试。

generate_all_field_drafts 是从画像子图的 generate_all_drafts
搬到主图层的（见 kam_node.py 文件头说明），这里补上对应的单元测试，覆盖
原来在 test_sub_profile_node.py 里的同款用例。
"""

from unittest import TestCase
from unittest.mock import patch

from src.graphs.kam_graph.kam_node import generate_all_field_drafts


class GenerateAllFieldDraftsTests(TestCase):
    @patch(
        "src.graphs.kam_graph.kam_node.suggest_profile_draft"
    )
    def test_low_confidence_field_is_marked_need_verify(self, mock_suggest):
        mock_suggest.return_value = {
            "project_stage": {
                "value": "方案评估中",
                "confidence": 2,
                "source": "模拟聊天记录",
                "status": "draft",
            },
            "tech_goal": {
                "value": "提升产线良率",
                "confidence": 8,
                "source": "模拟聊天记录",
                "status": "draft",
            },
        }

        draft = generate_all_field_drafts(
            wxqy_msgs=[],
            wxkf_msgs=[],
            orders=[],
        )

        self.assertEqual(draft["project_stage"]["status"], "need_verify")
        self.assertEqual(draft["tech_goal"]["status"], "draft")

        mock_suggest.assert_called_once_with(
            wxqy_msgs=[],
            wxkf_msgs=[],
            orders=[],
        )


class CallProfileSubgraphTests(TestCase):
    """
    call_profile_subgraph 现在的职责：生成全部字段草稿 + 对每个字段各自发起
    独立 thread_id 的子图 invoke，收集每个字段的 interrupt 拼进
    profile_field_interrupts。这里用一个假的 profile_graph（mock）验证
    子 thread_id 命名规则和字段循环逻辑，不连真实数据库。
    """

    @patch("src.graphs.kam_graph.kam_node.generate_all_field_drafts")
    def test_dispatches_one_independent_thread_per_field(self, mock_generate):
        from src.graphs.kam_graph import kam_node

        mock_generate.return_value = {
            "project_stage": {
                "value": "方案评估中",
                "confidence": 8,
                "source": "模拟聊天记录",
                "status": "draft",
            },
            "tech_goal": {
                "value": "提升产线良率",
                "confidence": 2,
                "source": "模拟聊天记录",
                "status": "need_verify",
            },
        }

        invoked_configs = []

        class FakeInterrupt:
            def __init__(self, id_, value):
                self.id = id_
                self.value = value

        class FakeProfileGraph:
            def invoke(self, sub_input, config):
                invoked_configs.append((sub_input["field_name"], config))
                return {
                    "__interrupt__": (
                        FakeInterrupt(
                            id_=f"irq_{sub_input['field_name']}",
                            value={
                                "field": sub_input["field_name"],
                                "value": sub_input["value"],
                                "confidence": sub_input["confidence"],
                                "source": sub_input["source"],
                                "status": sub_input["status"],
                            },
                        ),
                    )
                }

        kam_node.set_profile_graph(FakeProfileGraph())
        try:
            result = kam_node.call_profile_subgraph(
                {
                    "_main_thread_id": "thr_test001",
                    "follow_user_id": "u001",
                    "external_id": "ext_001",
                    "wxqy_msgs": [],
                    "wxkf_msgs": [],
                    "orders": [],
                }
            )
        finally:
            kam_node.set_profile_graph(None)

        # 每个字段各自用了独立的子 thread_id，命名规则是 f"{主thread_id}:{field_name}"
        thread_ids_by_field = {
            field_name: config["configurable"]["thread_id"]
            for field_name, config in invoked_configs
        }
        self.assertEqual(thread_ids_by_field["project_stage"], "thr_test001:project_stage")
        self.assertEqual(thread_ids_by_field["tech_goal"], "thr_test001:tech_goal")

        # 两个字段各自的 interrupt 都被收集进 profile_field_interrupts
        interrupts_by_field = {
            item["field"]: item for item in result["profile_field_interrupts"]
        }
        self.assertEqual(interrupts_by_field["project_stage"]["thread_id"], "thr_test001:project_stage")
        self.assertEqual(interrupts_by_field["project_stage"]["interrupt_id"], "irq_project_stage")
        self.assertEqual(interrupts_by_field["tech_goal"]["thread_id"], "thr_test001:tech_goal")
        self.assertEqual(interrupts_by_field["tech_goal"]["status"], "need_verify")
