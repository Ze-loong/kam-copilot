import copy
import threading
import time
from types import SimpleNamespace
from unittest import TestCase

from src.store.store_client import upsert_employee, upsert_external_user_profile


class FakeStore:
    def __init__(self):
        self.saved_value = None

    def get(self, namespace, key):
        return None

    def put(self, namespace, key, value):
        self.saved_value = value


class UpsertExternalUserProfileTests(TestCase):
    def test_confirmed_low_confidence_field_is_not_changed_back(self):
        store = FakeStore()

        upsert_external_user_profile(
            follow_user_id="u001",
            external_id="ext_001",
            new_profile_items={
                "tech_goal": {
                    "value": "提升产线良率",
                    "confidence": 2,
                    "source": "模拟聊天记录",
                    "status": "confirmed",
                }
            },
            store=store,
        )

        saved_tech_goal = store.saved_value["profile_items"]["tech_goal"]

        self.assertEqual(saved_tech_goal["status"], "confirmed")

    def test_concurrent_updates_for_same_customer_keep_both_fields(self):
        class ConcurrentFakeStore:
            def __init__(self):
                self.saved_value = None
                self.guard = threading.Lock()

            def get(self, namespace, key):
                with self.guard:
                    snapshot = copy.deepcopy(self.saved_value)
                # 放大读改写竞态窗口，未加客户锁时两个线程会读到同一旧值。
                time.sleep(0.03)
                return SimpleNamespace(value=snapshot) if snapshot is not None else None

            def put(self, namespace, key, value):
                with self.guard:
                    self.saved_value = copy.deepcopy(value)

        store = ConcurrentFakeStore()
        start = threading.Barrier(3)

        def write_field(field_name):
            start.wait()
            upsert_external_user_profile(
                "u_concurrent",
                "ext_concurrent",
                {
                    field_name: {
                        "value": field_name,
                        "confidence": 5,
                        "source": "test",
                        "status": "confirmed",
                    }
                },
                store=store,
            )

        threads = [
            threading.Thread(target=write_field, args=("tech_goal",)),
            threading.Thread(target=write_field, args=("budget",)),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(set(store.saved_value["profile_items"]), {"tech_goal", "budget"})

    def test_agent_employee_upsert_writes_hash(self):
        store = FakeStore()

        upsert_employee(
            "u001", "测试顾问", "consultant", "华东", "plain-secret", store=store
        )

        self.assertNotEqual(store.saved_value["password"], "plain-secret")
        self.assertTrue(store.saved_value["password"].startswith("pbkdf2_sha256$"))
