"""Anthropic provider, imported lazily so the app runs without the SDK."""

from __future__ import annotations

import json
from typing import Any

from finance_tool.llm.base import (
    AssistantTurn,
    Classification,
    LLMError,
    ToolCall,
    ToolSpec,
    json_object,
    parse_classifications,
)

CLASSIFY_SYSTEM = (
    "You categorise transactions from an Australian bank account. Pick exactly one "
    "category from the allowed list for each transaction. Confidence is 0 to 1: use "
    "below 0.6 when the merchant is genuinely ambiguous — a transfer, an unfamiliar "
    "acronym, or a description that could be several different businesses. Never invent "
    "a category. If an item has a `note`, that is the user's own explanation; treat it as "
    "authoritative and use a confidence of 0.9 or higher."
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str = "high"):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._effort = effort

    def _messages(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        pending_results: list[dict] = []

        def flush() -> None:
            if pending_results:
                out.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for message in messages:
            role = message.get("role")
            if role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": message.get("content") or "(no output)",
                    **({"is_error": True} if message.get("is_error") else {}),
                })
                continue

            flush()
            if role == "user":
                out.append({"role": "user",
                            "content": [{"type": "text", "text": message.get("content") or ""}]})
            elif role == "assistant":
                if message.get("raw") is not None:
                    out.append({"role": "assistant", "content": message["raw"]})
                    continue
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                for call in message.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": call["id"],
                                   "name": call["name"], "input": call["arguments"]})
                out.append({"role": "assistant",
                            "content": blocks or [{"type": "text", "text": ""}]})
        flush()
        return out

    def complete(self, *, system: str, messages: list[dict],
                 tools: list[ToolSpec] | None = None,
                 max_tokens: int = 8192) -> AssistantTurn:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": spec.name, "description": spec.description,
                 "input_schema": spec.input_schema}
                for spec in tools
            ]

        budgets = {"low": 1024, "medium": 2048, "high": 4096,
                   "xhigh": 8192, "max": 16384}
        if self._effort in budgets:
            budget = min(budgets[self._effort], max(1024, max_tokens - 1024))
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            kwargs["max_tokens"] = max(max_tokens, budget + 1024)

        try:
            response = self._client.messages.create(**kwargs)
        except self._anthropic.APIStatusError as exc:
            if "thinking" in kwargs and getattr(exc, "status_code", None) == 400:
                # Not every model accepts extended thinking; fall back rather than
                # failing the whole conversation on a capability probe.
                kwargs.pop("thinking", None)
                kwargs["max_tokens"] = max_tokens
                try:
                    response = self._client.messages.create(**kwargs)
                except self._anthropic.APIStatusError as retry:
                    raise LLMError(
                        f"anthropic API error ({retry.status_code}): {retry.message}"
                    ) from retry
                except self._anthropic.APIConnectionError as retry:
                    raise LLMError(f"Could not reach anthropic: {retry}") from retry
            else:
                raise LLMError(
                    f"anthropic API error ({exc.status_code}): {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach anthropic: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            detail = getattr(response, "stop_details", None)
            reason = (getattr(detail, "explanation", None)
                      or getattr(detail, "category", None) or "no detail")
            raise LLMError(f"The model declined this request ({reason}).")

        text = "".join(block.text for block in response.content
                       if getattr(block, "type", "") == "text")
        thinking = "".join(
            getattr(block, "thinking", "") for block in response.content
            if getattr(block, "type", "") == "thinking")
        calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content if getattr(block, "type", "") == "tool_use"
        ]
        raw = []
        for block in response.content:
            if hasattr(block, "model_dump"):
                raw.append(block.model_dump())
            else:
                data = {"type": getattr(block, "type", "")}
                for name in ("text", "thinking", "signature", "id", "name", "input"):
                    if hasattr(block, name):
                        data[name] = getattr(block, name)
                raw.append(data)
        return AssistantTurn(text=text, tool_calls=calls,
                             stop_reason=getattr(response, "stop_reason", None) or "end_turn",
                             thinking=thinking, raw=raw)

    def classify(self, transactions: list[dict], categories: list[str]) -> list[Classification]:
        if not transactions:
            return []
        lines = [json.dumps({
            "index": index,
            "date": txn.get("date"),
            "description": txn.get("description"),
            "merchant": txn.get("merchant"),
            "amount": txn.get("amount"),
            **({"note": txn["note"]} if txn.get("note") else {}),
        }, ensure_ascii=False) for index, txn in enumerate(transactions)]
        prompt = (
            "Allowed categories: " + ", ".join(categories) + "\n\n"
            "Return JSON shaped exactly like "
            '{"classifications":[{"index":0,"category":"...","confidence":0.9,"reason":"..."}]}'
            "\n\nTransactions:\n" + "\n".join(lines)
        )
        try:
            response = self._client.messages.create(
                model=self._model, max_tokens=4096, system=CLASSIFY_SYSTEM,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            )
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic API error ({exc.status_code}): {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach anthropic: {exc}") from exc

        text = next((block.text for block in response.content
                     if getattr(block, "type", "") == "text"), "")
        return parse_classifications(json_object(text))
