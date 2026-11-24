"""Provider-neutral LLM plumbing, with fake SDKs and no network."""

import sys
import types

import pytest

from finance_tool.llm import base
from finance_tool.llm import factory
from finance_tool.store.entities import Settings, StoreDocument
from finance_tool.store.secrets import Secrets
from finance_tool.store.store import Store


def _store(**settings):
    return Store(doc=StoreDocument(), settings=Settings(**settings), data_dir=None)


def test_the_provider_is_inferred_from_the_key_that_exists():
    store = _store()

    config = factory.resolve(store, Secrets(llm_anthropic="k"))

    assert config["provider"] == "anthropic"
    assert config["model"] == factory.DEFAULT_MODELS["anthropic"]
    assert config["configured"] is True


def test_a_stored_provider_wins_and_ollama_needs_no_key():
    store = _store(ai_provider="ollama", ai_model="llama3.1")

    config = factory.resolve(store, Secrets())

    assert config["provider"] == "ollama"
    assert config["configured"] is True


def test_no_provider_configured_has_a_reason():
    store = _store()

    status = factory.provider_status(store, Secrets())

    assert status["configured"] is False
    assert "Settings" in status["reason"]


def test_a_cloud_provider_without_a_key_is_not_built():
    config = {"provider": "openai", "model": "gpt", "api_key": None, "base_url": ""}
    assert factory.build(config) is None


def test_test_key_refuses_an_unknown_provider():
    ok, detail = factory.test_key("mystery", "model", "key")
    assert ok is False
    assert "Unknown provider" in detail


def test_json_object_tolerates_fences_and_prose_but_rejects_nonsense():
    assert base.json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert base.json_object('```json {"a": 1}```') == {"a": 1}
    assert base.json_object('Here is the answer: {"a": 1}') == {"a": 1}
    with pytest.raises(base.LLMError):
        base.json_object("not json")


def test_openrouter_without_a_model_asks_for_one():
    store = _store(ai_provider="openrouter")

    status = factory.provider_status(store, Secrets(llm_openrouter="key"))

    assert status["configured"] is False
    assert "Choose a model" in status["reason"]


def test_ollama_test_key_does_not_claim_a_missing_key():
    ok, detail = factory.test_key("ollama", "", "")
    assert ok is False
    assert "Choose a model" in detail


def test_a_missing_sdk_is_not_reported_as_configured(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    config = {"provider": "anthropic", "model": "m", "api_key": "k",
              "base_url": "", "effort": "high"}
    assert factory.build(config) is None


def test_classifications_are_parsed_tolerantly():
    parsed = base.parse_classifications({"classifications": [
        {"index": 0, "category": "Groceries", "confidence": 1.7, "reason": "ok"},
        {"index": "bad", "category": "X", "confidence": 1},
        {"index": 2, "category": "Housing", "confidence": "0.4"},
    ]})

    assert [(item.index, item.category, item.confidence) for item in parsed] == [
        (0, "Groceries", 1.0), (2, "Housing", 0.4)]


# ----------------------------------------------------------- fake Anthropic SDK


def _install_anthropic(monkeypatch, response):
    class Messages:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return response

    class Client:
        def __init__(self, api_key):
            self.messages = Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = Client
    module.APIStatusError = RuntimeError
    module.APIConnectionError = RuntimeError
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module


class _Block:
    def __init__(self, kind, **kwargs):
        self.type = kind
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def test_anthropic_complete_captures_text_tools_and_thinking(monkeypatch):
    response = _Response([
        _Block("thinking", thinking="reasoning here"),
        _Block("text", text="Hello"),
        _Block("tool_use", id="call_1", name="search_transactions",
               input={"query": "coles"}),
    ], stop_reason="tool_use")
    _install_anthropic(monkeypatch, response)
    from finance_tool.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider("key", "model")
    turn = provider.complete(system="sys", messages=[base.neutral_user("hi")],
                             tools=[base.ToolSpec("search_transactions", "find", {})])

    assert turn.text == "Hello"
    assert turn.thinking == "reasoning here"
    assert turn.tool_calls[0].name == "search_transactions"
    assert turn.tool_calls[0].arguments == {"query": "coles"}
    kwargs = provider._client.messages.kwargs
    assert kwargs["tools"][0]["name"] == "search_transactions"
    assert kwargs["thinking"]["type"] == "enabled"


def test_an_anthropic_thinking_rejection_falls_back_and_a_retry_failure_is_an_llm_error(
        monkeypatch):
    class StatusError(Exception):
        status_code = 400
        message = "thinking not supported"

    class ConnectionError_(Exception):
        pass

    class Messages:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise StatusError()
            raise ConnectionError_("offline")

    class Client:
        def __init__(self, api_key):
            self.messages = Messages()

    module = types.ModuleType("anthropic")
    module.Anthropic = Client
    module.APIStatusError = StatusError
    module.APIConnectionError = ConnectionError_
    monkeypatch.setitem(sys.modules, "anthropic", module)
    from finance_tool.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider("key", "model", effort="high")
    with pytest.raises(base.LLMError, match="Could not reach"):
        provider.complete(system="sys", messages=[base.neutral_user("hi")])
    assert provider._client.messages.calls == 2


def test_anthropic_classify_reads_json(monkeypatch):
    response = _Response([_Block("text", text=(
        '{"classifications":[{"index":0,"category":"Groceries",'
        '"confidence":0.9,"reason":"coles"}]}'))])
    _install_anthropic(monkeypatch, response)
    from finance_tool.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider("key", "model")
    found = provider.classify([{"description": "COLES", "amount": -10}],
                              ["Groceries"])

    assert found == [base.Classification(0, "Groceries", 0.9, "coles")]


# -------------------------------------------------------------- fake OpenAI SDK


def _install_openai(monkeypatch, message, finish_reason="stop"):
    class Completions:
        def create(self, **kwargs):
            choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
            return types.SimpleNamespace(choices=[choice])

    class Chat:
        def __init__(self):
            self.completions = Completions()

    class Client:
        def __init__(self, api_key, base_url=None):
            self.chat = Chat()

    module = types.ModuleType("openai")
    module.OpenAI = Client
    module.APIStatusError = RuntimeError
    module.APIConnectionError = RuntimeError
    monkeypatch.setitem(sys.modules, "openai", module)


def test_openai_complete_reads_tool_calls_and_reasoning(monkeypatch):
    call = types.SimpleNamespace(
        id="call_1", function=types.SimpleNamespace(
            name="get_finance_context", arguments="{}"))
    message = types.SimpleNamespace(content="", tool_calls=[call],
                                    reasoning_content="thought")
    _install_openai(monkeypatch, message, finish_reason="tool_calls")
    from finance_tool.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider("key", "gpt", label="openai")
    turn = provider.complete(system="sys", messages=[base.neutral_user("hi")])

    assert turn.tool_calls[0].name == "get_finance_context"
    assert turn.thinking == "thought"
    assert turn.stop_reason == "tool_calls"

    replayed = provider._messages("sys", [base.neutral_assistant(
        base.AssistantTurn("", tool_calls=[]))])
    assert replayed[1]["content"] == ""      # OpenAI rejects a null content here
