import sys
import unittest
from pathlib import Path


ADMIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_DIR))

import kam_admin_app as admin_module
from store_client.kam_store_api import KamStoreAPI
from store_client.passwords import hash_password, verify_password


class FakeEmployeeStoreAPI:
    def __init__(self):
        self.disabled_user_ids = []

    def list_employees(self):
        return [
            {
                "user_id": "east_emp",
                "name": "华东顾问",
                "role": "consultant",
                "region": "华东",
                "password": "must-not-leak",
                "internal_note": "must-not-leak",
            },
            {
                "user_id": "north_emp",
                "name": "华北顾问",
                "role": "consultant",
                "region": "华北",
                "password": "must-not-leak",
            },
        ]

    def disable_employee(self, user_id):
        if user_id not in {"east_emp", "north_emp"}:
            return False
        self.disabled_user_ids.append(user_id)
        return True


class EmployeeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.original_client = admin_module.client
        self.fake_client = FakeEmployeeStoreAPI()
        admin_module.client = self.fake_client
        admin_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.http = admin_module.app.test_client()

    def tearDown(self):
        admin_module.client = self.original_client

    def login_as(self, role, region):
        with self.http.session_transaction() as session:
            session.update(
                {
                    "user_id": "viewer",
                    "name": "测试用户",
                    "role": role,
                    "region": region,
                }
            )

    def test_new_password_hash_round_trip_and_plaintext_rejection(self):
        encoded = hash_password("correct-password")

        self.assertNotEqual(encoded, "correct-password")
        self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password("correct-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertFalse(verify_password("legacy-password", "legacy-password"))

    def test_employee_upsert_writes_hash_instead_of_plaintext(self):
        class RecordingClient:
            def put(self, namespace, key, value):
                self.namespace = namespace
                self.key = key
                self.value = value

        api = KamStoreAPI.__new__(KamStoreAPI)
        api._client = RecordingClient()

        api.upsert_employee("east_emp", "华东顾问", "consultant", "华东", "plain-secret")

        self.assertNotEqual(api._client.value["password"], "plain-secret")
        self.assertTrue(verify_password("plain-secret", api._client.value["password"]))

    def test_super_admin_list_contains_only_safe_fields(self):
        self.login_as("super_admin", "总部")

        response = self.http.get("/api/employees")

        self.assertEqual(response.status_code, 200)
        items = response.get_json()["data"]
        self.assertEqual(len(items), 2)
        self.assertEqual(set(items[0]), {"user_id", "name", "role", "region"})
        self.assertNotIn("must-not-leak", response.get_data(as_text=True))

    def test_regional_manager_sees_only_own_region(self):
        self.login_as("regional_manager", "华东")

        response = self.http.get("/api/employees")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["user_id"] for item in response.get_json()["data"]],
            ["east_emp"],
        )

    def test_regional_manager_does_not_see_account_management_buttons(self):
        self.login_as("regional_manager", "华东")

        page = self.http.get("/employees").get_data(as_text=True)

        self.assertNotIn('data-bs-target="#addEmployeeModal"', page)
        self.assertIn("const canManageEmployees = false", page)

    def test_only_super_admin_can_disable_employee(self):
        self.login_as("regional_manager", "华东")
        forbidden = self.http.delete("/api/employees/east_emp")
        self.assertEqual(forbidden.status_code, 403)

        self.login_as("super_admin", "总部")
        response = self.http.delete("/api/employees/east_emp")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.fake_client.disabled_user_ids, ["east_emp"])


if __name__ == "__main__":
    unittest.main()
