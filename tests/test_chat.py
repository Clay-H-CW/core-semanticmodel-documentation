"""Tests for the chat feature.

Two things matter here: the deterministic tool functions must return exactly what is in
the model (this is what keeps the assistant from inventing field names), and the tool
loop must actually resolve tool calls before returning — not just pass them through.
"""

import json
import pathlib

import pytest

from semdoc import chat
from semdoc.ir.build import tmsl_to_model
from semdoc.ir.schema import ModelIR

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sample_tmsl.json"


@pytest.fixture(scope="module")
def ir():
    tmsl = json.loads(FIXTURE.read_text(encoding="utf-8"))
    model = tmsl_to_model(tmsl, name="Case Services Analytics", workspace="Analytics")
    return ModelIR(model=model)


# -- schema index / system prompt -------------------------------------------------------


def test_schema_index_lists_visible_tables_and_measures(ir):
    index = chat._schema_index(ir.model)
    assert "Service Fact [fact]" in index
    assert "Date [dimension] (date table)" in index
    assert "Total Units" in index
    assert "Avg Units per Service" in index


def test_system_prompt_includes_model_name_and_grounding_rules(ir):
    prompt = chat._system_prompt(ir.model)
    assert "Case Services Analytics" in prompt
    assert "Never invent a table, column, or measure name" in prompt


# -- deterministic tools ------------------------------------------------------------------


def test_lookup_table_returns_columns_and_relationships(ir):
    result = chat.run_tool(ir.model, "lookup_table", {"name": "Service Fact"})
    assert result["kind"] == "fact"
    assert {c["name"] for c in result["columns"]} >= {"Units", "ClientKey"}
    assert any(r["to"] == "Client[ClientKey]" for r in result["relationships"])
    # Inactive relationships must be visible to the assistant, not filtered out — this is
    # exactly the kind of thing it needs to warn a user about.
    assert any(not r["active"] for r in result["relationships"])


def test_lookup_table_is_case_insensitive(ir):
    result = chat.run_tool(ir.model, "lookup_table", {"name": "service fact"})
    assert result["name"] == "Service Fact"


def test_lookup_table_unknown_name_reports_error_not_none(ir):
    result = chat.run_tool(ir.model, "lookup_table", {"name": "Nonexistent"})
    assert "error" in result


def test_lookup_measure_returns_verbatim_dax_and_dependencies(ir):
    result = chat.run_tool(ir.model, "lookup_measure", {"name": "Avg Units per Service"})
    assert "DIVIDE" in result["expression"]
    assert set(result["depends_on"]) == {"[Total Units]", "[Service Count]"}


def test_lookup_measure_unknown_name_reports_error(ir):
    result = chat.run_tool(ir.model, "lookup_measure", {"name": "Made Up Measure"})
    assert "error" in result


def test_search_schema_finds_across_tables_measures_columns(ir):
    result = chat.run_tool(ir.model, "search_schema", {"query": "unit"})
    assert "Total Units" in result["measures"]
    assert "Service Fact[Units]" in result["columns"]


def test_search_schema_empty_query_is_rejected(ir):
    result = chat.run_tool(ir.model, "search_schema", {"query": ""})
    assert "error" in result


def test_unknown_tool_name_reports_error_not_crash(ir):
    result = chat.run_tool(ir.model, "not_a_real_tool", {})
    assert "error" in result


# -- tool-use loop, against a fake client (no network) ------------------------------------


class _FakeBlock:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def test_answer_resolves_a_tool_call_before_returning(ir):
    tool_call = _FakeResponse(
        "tool_use",
        [_FakeBlock("tool_use", id="t1", name="lookup_measure", input={"name": "Total Units"})],
    )
    final = _FakeResponse("end_turn", [_FakeBlock("text", text="It sums the Units column.")])
    client = _FakeClient([tool_call, final])

    reply = chat.answer(ir, client, [{"role": "user", "content": "What does Total Units do?"}])

    assert reply == "It sums the Units column."
    assert len(client.messages.calls) == 2

    # The tool result actually fed back must reflect the real model, not a placeholder.
    second_call_messages = client.messages.calls[1]["messages"]
    tool_result_message = second_call_messages[-1]
    assert tool_result_message["role"] == "user"
    payload = json.loads(tool_result_message["content"][0]["content"])
    assert "SUM" in payload["expression"]


def test_answer_returns_text_directly_when_no_tool_use(ir):
    final = _FakeResponse("end_turn", [_FakeBlock("text", text="Use the Client table.")])
    client = _FakeClient([final])

    reply = chat.answer(ir, client, [{"role": "user", "content": "Where do I find client name?"}])

    assert reply == "Use the Client table."
    assert len(client.messages.calls) == 1


def test_answer_gives_up_after_max_tool_rounds(ir):
    loop_forever = _FakeResponse(
        "tool_use",
        [_FakeBlock("tool_use", id="t1", name="lookup_table", input={"name": "Client"})],
    )
    client = _FakeClient([loop_forever] * chat.MAX_TOOL_ROUNDS)

    reply = chat.answer(ir, client, [{"role": "user", "content": "..."}])

    assert "wasn't able to finish" in reply
    assert len(client.messages.calls) == chat.MAX_TOOL_ROUNDS


def test_answer_rejects_history_not_ending_in_user_turn(ir):
    client = _FakeClient([])
    with pytest.raises(chat.ChatError):
        chat.answer(ir, client, [{"role": "assistant", "content": "hi"}])


def test_answer_truncates_long_history(ir):
    final = _FakeResponse("end_turn", [_FakeBlock("text", text="ok")])
    client = _FakeClient([final])

    long_history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
        for i in range(31)  # ends on i=30, an even index -> a user turn
    ]
    chat.answer(ir, client, long_history)

    sent = client.messages.calls[0]["messages"]
    assert len(sent) == chat.MAX_HISTORY_MESSAGES
    assert sent[-1]["content"] == "30"
