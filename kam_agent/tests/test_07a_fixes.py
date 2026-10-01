import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_node import (
    generate_chat_suggestion,
    retrieve_solution_refs,
)
from src.llm.json_retry import invoke_and_parse_json
from src.llm.llm_suggest_chat import suggest_chat_reply


def _state():
    return {
        "external_id": "customer_1",
        "wxqy_msgs": [
            {"from_id": "customer_1", "content": "需要焊装线方案", "msg_time": "2026-09-01"},
            {"from_id": "advisor_1", "content": "收到", "msg_time": "2026-09-02"},
            {"from_id": "customer_1", "content": "交期多少", "msg_time": "2026-09-03"},
        ],
    }


@patch("src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_node.search_knowledge_base")
def test_solution_search_filters_low_scores(search):
    search.return_value = [
        {"content": "方案A", "source_doc": "产品资料", "score": 0.8},
        {"content": "无关", "source_doc": "其他资料", "score": 0.1},
    ]
    result = retrieve_solution_refs(_state())
    assert len(result["solution_refs"]) == 1
    search.assert_called_once_with(query="需要焊装线方案\n交期多少", top_k=3, category="solution")


@patch("src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_node.search_knowledge_base")
def test_solution_search_failure_degrades(search):
    search.side_effect = ConnectionError("vector store unavailable")
    assert retrieve_solution_refs(_state()) == {"solution_refs": []}


@patch("src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_node.search_knowledge_base")
def test_no_customer_message_skips_search(search):
    state = _state()
    state["wxqy_msgs"] = [{"from_id": "advisor_1", "content": "你好"}]
    assert retrieve_solution_refs(state) == {"solution_refs": []}
    search.assert_not_called()


@patch("src.graphs.kam_graph.kam_sub_graph_chat_suggestion.sub_chat_suggestion_node.suggest_chat_reply")
def test_solution_refs_reach_generator(suggest):
    suggest.return_value = {"suggestion_text": "建议", "reasoning": "依据"}
    refs = [{"content": "方案A", "source_doc": "产品资料", "score": 0.8}]
    generate_chat_suggestion({**_state(), "solution_refs": refs})
    assert suggest.call_args.kwargs["solution_refs"] == refs


@patch("src.llm.llm_suggest_chat.get_llm")
def test_solution_refs_reach_prompt(get_llm):
    get_llm.return_value.invoke.return_value = SimpleNamespace(
        content='{"suggestion_text":"建议","reasoning":"依据"}'
    )
    suggest_chat_reply({}, {}, [], [], [{"content": "方案A", "source_doc": "产品资料"}])
    assert "方案A" in get_llm.return_value.invoke.call_args.args[0][1][1]


def test_json_retry_succeeds_on_second_response():
    model = Mock()
    model.invoke.side_effect = [SimpleNamespace(content="bad"), SimpleNamespace(content='{"ok":true}')]
    assert invoke_and_parse_json([], json.loads, model=model) == {"ok": True}
    assert model.invoke.call_count == 2


def test_json_retry_raises_after_second_invalid_response():
    model = Mock()
    model.invoke.side_effect = [SimpleNamespace(content="bad"), SimpleNamespace(content="also bad")]
    with pytest.raises(ValueError):
        invoke_and_parse_json([], json.loads, model=model)
    assert model.invoke.call_count == 2


def test_json_retry_does_not_retry_network_error():
    model = Mock()
    model.invoke.side_effect = ConnectionError("network unavailable")
    with pytest.raises(ConnectionError):
        invoke_and_parse_json([], json.loads, model=model)
    assert model.invoke.call_count == 1
