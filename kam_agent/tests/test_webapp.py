"""webapp 接口层的纯逻辑单元测试。"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException

from src.webapp.webapp import (
    StoreBatchGetRequest,
    StoreBatchSearchRequest,
    _normalize_chat_messages,
    _require_internal_api_key,
    _select_recreated_interrupt,
    _validated_store_namespace,
    store_batch_get,
    store_batch_search,
)


class SelectRecreatedInterruptTests(TestCase):
    def test_selects_matching_field_not_first_pending_interrupt(self):
        old_other_field = SimpleNamespace(
            id="old-project_stage-id",
            value={"field": "project_stage", "value": "方案评估中"},
        )
        recreated_field = SimpleNamespace(
            id="new-price-id",
            value={"field": "price_sensitivity", "value": "待核实"},
        )

        selected = _select_recreated_interrupt(
            {"__interrupt__": [old_other_field, recreated_field]},
            "price_sensitivity",
        )

        self.assertEqual(selected.id, "new-price-id")

    def test_raises_when_matching_field_is_missing(self):
        result = {
            "__interrupt__": [
                SimpleNamespace(id="old-project_stage-id", value={"field": "project_stage"}),
            ],
        }

        with self.assertRaises(HTTPException) as caught:
            _select_recreated_interrupt(result, "price_sensitivity")

        self.assertEqual(caught.exception.status_code, 500)


class NormalizeChatMessagesTests(TestCase):
    def test_normalizes_wxqy_and_sorts_by_time(self):
        items = [
            SimpleNamespace(value={"from_id": "advisor-1", "content": "后发", "msg_time": "2026-08-22 10:02:00"}),
            SimpleNamespace(value={"from_id": "customer-1", "content": "先发", "msg_time": "2026-08-22 10:01:00"}),
        ]

        result = _normalize_chat_messages(items, "wxqy_msg", "advisor-1")

        self.assertEqual([item["content"] for item in result], ["先发", "后发"])
        self.assertEqual([item["sender"] for item in result], ["customer", "advisor"])

    def test_maps_wxkf_staff_to_advisor(self):
        items = [
            SimpleNamespace(value={"origin": "staff", "content": "您好", "msg_time": "2026-08-22 10:00:00"}),
        ]

        result = _normalize_chat_messages(items, "wxkf_msg", "unused")

        self.assertEqual(result[0]["sender"], "advisor")


class InternalStoreSecurityTests(TestCase):
    def test_internal_api_key_accepts_only_matching_value(self):
        with patch.dict("os.environ", {"INTERNAL_API_KEY": "test-internal-key"}):
            self.assertIsNone(_require_internal_api_key("test-internal-key"))
            with self.assertRaises(HTTPException) as caught:
                _require_internal_api_key("wrong-key")

        self.assertEqual(caught.exception.status_code, 401)

    def test_store_namespace_whitelist(self):
        self.assertEqual(
            _validated_store_namespace(["external_user", "advisor-1"]),
            ("external_user", "advisor-1"),
        )
        with self.assertRaises(HTTPException) as caught:
            _validated_store_namespace(["arbitrary_namespace"])

        self.assertEqual(caught.exception.status_code, 403)

    def test_batch_get_and_search_preserve_request_order(self):
        class FakeStore:
            def get(self, namespace, key):
                return SimpleNamespace(key=key, value={"namespace": namespace[0]})

            def search(self, namespace, filter=None, limit=None):
                return [SimpleNamespace(key=namespace[-1], value={"limit": limit})]

        with patch("src.webapp.webapp.get_store_instance", return_value=FakeStore()):
            get_result = store_batch_get(
                StoreBatchGetRequest(
                    items=[
                        {"namespace": ["employee"], "key": "u1"},
                        {"namespace": ["tags_setting"], "key": "tag1"},
                    ]
                ),
                None,
            )
            search_result = store_batch_search(
                StoreBatchSearchRequest(
                    items=[
                        {"namespace": ["external_user", "advisor-a"], "limit": 5},
                        {"namespace": ["wxxd_order", "union-a"], "limit": 10},
                    ]
                ),
                None,
            )

        self.assertEqual([item["key"] for item in get_result["items"]], ["u1", "tag1"])
        self.assertEqual(
            [group[0]["key"] for group in search_result["results"]],
            ["advisor-a", "union-a"],
        )
