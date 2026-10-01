import os
from unittest import TestCase
from unittest.mock import patch

from src.kb import kb_ingest


class FakeCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True


class KbIngestBatchEmbeddingTests(TestCase):
    def test_ingest_encodes_all_chunks_in_one_batch(self):
        connection = FakeConnection()
        batches = []

        def fake_embed_many(texts):
            batches.append(list(texts))
            return [[float(index)] for index, _ in enumerate(texts)]

        document = "第一段\n\n第二段\n\n第三段"
        with (
            patch.dict(os.environ, {"KAM_VECTOR_POSTGRES_URL": "test-url"}),
            patch.object(kb_ingest, "_embed_many", side_effect=fake_embed_many),
            patch.object(kb_ingest.psycopg, "connect", return_value=connection),
        ):
            count = kb_ingest.ingest_document(document, "测试文档", "faq")

        self.assertEqual(count, 3)
        self.assertEqual(batches, [["第一段", "第二段", "第三段"]])
        insert_calls = [call for call in connection.cursor_instance.calls if "INSERT" in call[0]]
        self.assertEqual(len(insert_calls), 3)
        self.assertTrue(connection.committed)
