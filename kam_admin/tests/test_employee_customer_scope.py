import sys
import unittest
from pathlib import Path


ADMIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_DIR))

import kam_admin_app as admin_module


class FakeStoreAPI:
    def __init__(self):
        self.customer_queries = []

    def list_employees(self):
        return [
            {"user_id": "east_emp", "region": "华东"},
            {"user_id": "north_emp", "region": "华北"},
        ]

    def list_external_users_batch(self, follow_user_ids):
        self.customer_queries.extend(follow_user_ids)
        return [
            {
                "external_id": f"customer_of_{follow_user_id}",
                "union_id": f"union_of_{follow_user_id}",
                "name": "测试客户",
                "follow_user_id": follow_user_id,
            }
            for follow_user_id in follow_user_ids
        ]

    def list_external_users(self, follow_user_id):
        return self.list_external_users_batch([follow_user_id])


class EmployeeCustomerScopeTests(unittest.TestCase):
    def setUp(self):
        self.original_client = admin_module.client
        self.fake_client = FakeStoreAPI()
        admin_module.client = self.fake_client
        admin_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.http = admin_module.app.test_client()

    def tearDown(self):
        admin_module.client = self.original_client

    def login_as(self, user_id, role, region):
        with self.http.session_transaction() as session:
            session.update({
                "user_id": user_id,
                "name": "测试员工",
                "role": role,
                "region": region,
            })

    def test_super_admin_gets_only_selected_employee_customers(self):
        self.login_as("admin", "super_admin", "总部")

        page = self.http.get("/employee_customers/east_emp")
        response = self.http.get("/api/employee_customers/east_emp")

        self.assertEqual(page.status_code, 200)
        self.assertIn("east_emp", page.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake_client.customer_queries, ["east_emp"])
        data = response.get_json()["data"]
        self.assertEqual([item["follow_user_id"] for item in data], ["east_emp"])

    def test_regional_manager_cannot_view_employee_in_other_region(self):
        self.login_as("manager", "regional_manager", "华东")

        page = self.http.get("/employee_customers/north_emp")
        response = self.http.get("/api/employee_customers/north_emp")

        self.assertEqual(page.status_code, 403)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fake_client.customer_queries, [])

    def test_consultant_cannot_use_employee_customer_route(self):
        self.login_as("east_emp", "consultant", "华东")

        self.assertEqual(self.http.get("/employee_customers/east_emp").status_code, 403)
        self.assertEqual(self.http.get("/api/employee_customers/east_emp").status_code, 403)

    def test_customer_and_order_pages_enforce_region_scope(self):
        self.login_as("north_manager", "regional_manager", "华北")
        self.assertEqual(self.http.get("/customers/customer_of_east_emp").status_code, 403)
        self.assertEqual(self.http.get("/orders/union_of_east_emp").status_code, 403)
        self.login_as("east_manager", "regional_manager", "华东")
        self.assertEqual(self.http.get("/customers/customer_of_east_emp").status_code, 200)
        self.assertEqual(self.http.get("/orders/union_of_east_emp").status_code, 200)
        self.login_as("admin", "super_admin", "总部")
        self.assertEqual(self.http.get("/customers/customer_of_east_emp").status_code, 200)


if __name__ == "__main__":
    unittest.main()
