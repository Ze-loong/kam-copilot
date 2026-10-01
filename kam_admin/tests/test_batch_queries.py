import sys
import unittest
from pathlib import Path


ADMIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_DIR))

import kam_admin_app as admin_module
from store_client.kam_store_api import KamStoreAPI


class RecordingBatchClient:
    def __init__(self):
        self.search_requests = []
        self.get_requests = []

    def batch_search(self, requests_):
        self.search_requests.append(requests_)
        return [
            [{"external_id": f"ext-{index}", "union_id": f"union-{index}"}]
            for index, _ in enumerate(requests_)
        ]

    def batch_get(self, requests_):
        self.get_requests.append(requests_)
        return [
            {"profile_items": {"goal": {"status": "confirmed"}}}
            for _ in requests_
        ]


class BatchQueryClientTests(unittest.TestCase):
    def setUp(self):
        self.api = KamStoreAPI.__new__(KamStoreAPI)
        self.api._client = RecordingBatchClient()

    def test_multiple_advisors_use_one_batch_search_call(self):
        customers = self.api.list_external_users_batch(["advisor-a", "advisor-b"])

        self.assertEqual(len(self.api._client.search_requests), 1)
        self.assertEqual(len(self.api._client.search_requests[0]), 2)
        self.assertEqual(
            [customer["follow_user_id"] for customer in customers],
            ["advisor-a", "advisor-b"],
        )

    def test_multiple_profiles_use_one_batch_get_call(self):
        customers = [
            {"follow_user_id": "advisor-a", "external_id": "ext-a"},
            {"follow_user_id": "advisor-b", "external_id": "ext-b"},
        ]

        profiles = self.api.get_customer_profiles_batch(customers)

        self.assertEqual(len(self.api._client.get_requests), 1)
        self.assertEqual(len(self.api._client.get_requests[0]), 2)
        self.assertEqual(len(profiles), 2)


class DashboardBatchQueryTests(unittest.TestCase):
    class FakeDashboardStore:
        def __init__(self):
            self.calls = {"customers": 0, "profiles": 0, "orders": 0}

        def list_employees(self):
            return [
                {"user_id": "advisor-a", "region": "华东"},
                {"user_id": "advisor-b", "region": "华北"},
            ]

        def list_external_users_batch(self, follow_user_ids):
            self.calls["customers"] += 1
            return [
                {
                    "external_id": f"ext-{index}",
                    "union_id": f"union-{index}",
                    "follow_user_id": follow_user_id,
                }
                for index, follow_user_id in enumerate(follow_user_ids)
            ]

        def get_customer_profiles_batch(self, customers):
            self.calls["profiles"] += 1
            return [
                {"profile_items": {"goal": {"status": "confirmed"}}}
                for _ in customers
            ]

        def list_orders_batch(self, union_ids):
            self.calls["orders"] += 1
            return {union_id: [{"_key": f"order-{union_id}"}] for union_id in union_ids}

    def setUp(self):
        self.original_client = admin_module.client
        self.fake_client = self.FakeDashboardStore()
        admin_module.client = self.fake_client
        admin_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.http = admin_module.app.test_client()
        with self.http.session_transaction() as session:
            session.update(
                {
                    "user_id": "admin",
                    "name": "管理员",
                    "role": "super_admin",
                    "region": "总部",
                }
            )

    def tearDown(self):
        admin_module.client = self.original_client

    def test_dashboard_uses_one_batch_per_data_type(self):
        response = self.http.get("/api/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.fake_client.calls,
            {"customers": 1, "profiles": 1, "orders": 1},
        )
        data = response.get_json()["data"]
        self.assertEqual(data["customer_count"], 2)
        self.assertEqual(data["order_count"], 2)


if __name__ == "__main__":
    unittest.main()
