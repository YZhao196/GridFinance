"""The assistant loop, tools and write gate (§24)."""

from datetime import date, datetime

import pytest

from finance_tool.assistant import chat
from finance_tool.assistant.tools import TOOL_SPECS, execute_tool
from finance_tool.llm.base import AssistantTurn, ToolCall
from finance_tool.store.entities import (
    Account,
    ChatMessage,
    Settings,
    StoreDocument,
    Transaction,
)
from finance_tool.store.store import Store

TODAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 12, 0, 0)


class FakeProvider:
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def complete(self, *, system, messages, tools=None, max_tokens=8192):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        return self.turns.pop(0)

    def classify(self, transactions, categories):
        return []


def _store(tmp_path, **settings):
    store = Store(data_dir=tmp_path, doc=StoreDocument(
        accounts=[Account(id="a_1", name="Everyday", kind="transaction",
                          balance=100.0, currency="AUD")],
        transactions=[Transaction(id="t1", date=date(2026, 10, 1), amount=-42.0,
                                  description="MYSTERY SHOP", account="a_1")],
    ), settings=Settings(**settings))
    return store


def test_a_plain_turn_is_persisted_with_its_thinking(tmp_path):
    store = _store(tmp_path)
    provider = FakeProvider([AssistantTurn("You spent **$42.00**.", thinking="checked")])

    result = chat.send(store, "what did I spend?", provider=provider, today=TODAY, now=NOW)

    assert result["reply"] == "You spent **$42.00**."
    assert result["tools_used"] == []
    assert [message.role for message in store.doc.messages] == ["user", "assistant"]
    assert store.doc.messages[1].thinking == "checked"
    assert "Today is 2026-11-05" in provider.calls[0]["system"]
    assert "Accounts:" in provider.calls[0]["system"]
    store.close()


def test_a_tool_round_is_executed_and_recorded(tmp_path):
    store = _store(tmp_path)
    provider = FakeProvider([
        AssistantTurn("", tool_calls=[ToolCall("c1", "get_finance_context", {})]),
        AssistantTurn("Your account has $100.00."),
    ])

    result = chat.send(store, "how much is there?", provider=provider, today=TODAY,
                       now=NOW)

    assert result["tools_used"] == ["get_finance_context"]
    assert result["reply"] == "Your account has $100.00."
    assert [message.role for message in store.doc.messages] == \
        ["user", "assistant", "tool", "assistant"]
    assert "Accounts:" in store.doc.messages[2].content
    store.close()


def test_writes_are_refused_when_the_switch_is_off(tmp_path):
    store = _store(tmp_path, ai_writes=False)
    provider = FakeProvider([
        AssistantTurn("", tool_calls=[ToolCall("c1", "set_category", {
            "transaction_id": "t1", "category": "Housing"})]),
        AssistantTurn("I could not change it."),
    ])

    chat.send(store, "categorise it", provider=provider, today=TODAY, now=NOW)

    assert store.view().transaction("t1").category is None
    assert "read-only" in store.doc.messages[2].content
    store.close()


def test_writes_are_applied_and_snapshotted_when_allowed(tmp_path):
    store = _store(tmp_path, ai_writes=True)
    provider = FakeProvider([
        AssistantTurn("", tool_calls=[ToolCall("c1", "set_category", {
            "transaction_id": "t1", "category": "Housing"})]),
        AssistantTurn("Done."),
    ])

    chat.send(store, "categorise it", provider=provider, today=TODAY, now=NOW)

    assert store.view().transaction("t1").category == "Housing"
    assert store.view().transaction("t1").category_source == "ai"
    assert any("before-ai" in path.name for path in (tmp_path / "backups").iterdir())
    store.close()


def test_an_unknown_transaction_is_an_error_result(tmp_path):
    store = _store(tmp_path, ai_writes=True)

    result = execute_tool(store, "set_category",
                          {"transaction_id": "nope", "category": "Housing"},
                          today=TODAY)

    assert "no transaction" in result
    store.close()


def test_an_unknown_category_is_refused_by_set_category(tmp_path):
    store = _store(tmp_path, ai_writes=True)

    result = execute_tool(store, "set_category",
                          {"transaction_id": "t1", "category": "Not A Category"},
                          today=TODAY)

    assert "unknown category" in result
    assert store.view().transaction("t1").category is None
    store.close()


def test_create_rule_and_retro_apply_through_the_tool(tmp_path):
    store = _store(tmp_path, ai_writes=True)

    result = execute_tool(store, "create_rule",
                          {"pattern": "MYSTERY SHOP", "category": "Shopping"},
                          today=TODAY)

    assert '"ok": true' in result
    assert store.view().transaction("t1").category == "Shopping"
    assert store.doc.rules[0].name.startswith("AI:")
    store.close()


def test_sql_in_the_same_turn_sees_an_earlier_tool_edit(tmp_path):
    store = _store(tmp_path, ai_writes=True)
    provider = FakeProvider([
        AssistantTurn("", tool_calls=[
            ToolCall("c1", "set_category",
                     {"transaction_id": "t1", "category": "Housing"}),
            ToolCall("c2", "query_sql",
                     {"sql": "SELECT category FROM transactions WHERE id = 't1'"}),
        ]),
        AssistantTurn("Housing."),
    ])

    chat.send(store, "categorise and check", provider=provider, today=TODAY, now=NOW)

    tool_results = [message.content for message in store.doc.messages
                    if message.role == "tool"]
    assert '"category": "Housing"' in tool_results[1]
    store.close()


def test_a_note_and_a_summary_are_written(tmp_path):
    store = _store(tmp_path, ai_writes=True)

    assert '"ok": true' in execute_tool(
        store, "add_note", {"transaction_id": "t1", "note": "weekly"},
        today=TODAY)
    assert store.view().transaction("t1").note == "weekly"

    result = execute_tool(store, "write_summaries", {}, today=TODAY)
    assert "FY2026-27" in result
    store.close()


def test_history_drops_an_incomplete_round_and_merges_consecutive_users(tmp_path):
    store = _store(tmp_path)
    store.doc.messages.extend([
        ChatMessage(id="m1", role="assistant", content="orphan"),
        ChatMessage(id="m2", role="user", content="hello"),
        ChatMessage(id="m3", role="user", content="again"),
        ChatMessage(id="m4", role="assistant", content="",
                    tool_calls=[{"id": "c1", "name": "x", "arguments": {}}]),
        ChatMessage(id="m5", role="tool", content="{}",
                    tool_calls=[{"tool_call_id": "c1", "name": "x"}]),
    ])

    history = chat.load_history(store)

    assert history == [{"role": "user", "content": "hello\n\nagain"}]
    store.close()


class BoomProvider:
    name = "boom"

    def complete(self, **kwargs):
        raise RuntimeError("offline")

    def classify(self, transactions, categories):
        return []


def test_a_failed_provider_call_does_not_leave_two_user_turns(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(RuntimeError):
        chat.send(store, "first", provider=BoomProvider(), today=TODAY, now=NOW)

    provider = FakeProvider([AssistantTurn("second answer")])
    chat.send(store, "second", provider=provider, today=TODAY, now=NOW)

    sent = provider.calls[0]["messages"]
    assert [message["role"] for message in sent] == ["user"]
    assert sent[0]["content"] == "first\n\nsecond"
    store.close()


def test_the_last_tool_round_gets_a_final_answer_call(tmp_path):
    store = _store(tmp_path)
    turns = [AssistantTurn("", tool_calls=[ToolCall(f"c{i}", "get_finance_context", {})])
             for i in range(8)]
    turns.append(AssistantTurn("Here is the answer."))
    provider = FakeProvider(turns)

    result = chat.send(store, "go", provider=provider, today=TODAY, now=NOW)

    assert result["reply"] == "Here is the answer."
    assert len(provider.calls) == 9
    assert provider.calls[-1]["tools"] is None
    store.close()


def test_a_mid_turn_query_does_not_leave_a_dangling_tool_call_on_disk(tmp_path):
    store = _store(tmp_path, ai_writes=True)
    provider = FakeProvider([
        AssistantTurn("", tool_calls=[
            ToolCall("c1", "set_category",
                     {"transaction_id": "t1", "category": "Housing"}),
            ToolCall("c2", "query_sql",
                     {"sql": "SELECT category FROM transactions WHERE id = 't1'"}),
        ]),
        AssistantTurn("done"),
    ])

    chat.send(store, "go", provider=provider, today=TODAY, now=NOW)
    store.close()

    reloaded = Store.load(tmp_path)
    assert not any(message.role == "assistant" and message.tool_calls
                   for message in reloaded.doc.messages)
    reloaded.close()


def test_clear_removes_the_transcript(tmp_path):
    store = _store(tmp_path)
    chat.send(store, "hello", provider=FakeProvider([AssistantTurn("hi")]),
              today=TODAY, now=NOW)

    assert chat.clear(store) == 2
    assert store.doc.messages == []
    store.close()


def test_the_tool_catalogue_has_fifteen_tools():
    assert len(TOOL_SPECS) == 15
    assert {spec.name for spec in TOOL_SPECS} >= {
        "get_finance_context", "search_transactions", "set_category",
        "create_rule", "query_sql", "write_summaries"}
