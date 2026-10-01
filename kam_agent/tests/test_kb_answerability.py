"""F16 模型判定答不上时的固定兜底与旧格式兼容测试。"""

import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.llm.llm_answer_kb import answer_from_kb


class KbAnswerabilityTests(TestCase):
    def setUp(self):
        self.chunks = [{"source_doc": "常见问题FAQ", "score": 0.6, "content": "质保期为12个月"}]

    def answer(self, payload):
        with (
            patch("src.llm.llm_answer_kb.get_llm") as mock_llm,
            patch("src.llm.llm_answer_kb.get_employee", return_value={"name": "小张"}),
        ):
            mock_llm.return_value.invoke.return_value = SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False)
            )
            return answer_from_kb("设备质保期是多久？", self.chunks, True, "u001")

    def test_unanswerable_uses_named_fallback_without_sources(self):
        result = self.answer({"answerable": False, "answer_text": "", "cited_sources": []})
        self.assertIn("小张", result["answer_text"])
        self.assertEqual(result["cited_sources"], [])

    def test_answerable_keeps_supported_answer(self):
        result = self.answer({"answerable": True, "answer_text": "质保期为12个月", "cited_sources": ["常见问题FAQ"]})
        self.assertEqual(result, {"answer_text": "质保期为12个月", "cited_sources": ["常见问题FAQ"]})

    def test_missing_answerable_preserves_old_response(self):
        result = self.answer({"answer_text": "质保期为12个月", "cited_sources": ["常见问题FAQ"]})
        self.assertEqual(result, {"answer_text": "质保期为12个月", "cited_sources": ["常见问题FAQ"]})
