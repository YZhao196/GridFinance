"""Any OpenAI-compatible endpoint: OpenAI, DeepSeek, OpenRouter, Ollama, LM Studio."""

from __future__ import annotations

import json

from finance_tool.llm.anthropic_provider import CLASSIFY_SYSTEM
from finance_tool.llm.base import (
    AssistantTurn,
    Classification,
    LLMError,
    ToolCall,
    ToolSpec,
    json_object,
    parse_classifications,
)


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, base_url: str | None = None,
                 label: str = "openai"):
        import openai

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key or "not-needed",
                                     base_url=base_url or None)
        self._model = model
        self.name = label

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for message in messages:
            role = message.get("role")
            if role == "user":
                out.append({"role": "user", "content": message.get("content") or ""})
            elif role == "assistant":
                entry: dict = {"role": "assistant",
                               "content": message.get("content") or ""}
                calls = message.get("tool_calls") or []
                if calls:
                    entry["tool_calls"] = [
                        {"id": call["id"], "type": "function",
                         "function": {"name": call["name"],
                                      "arguments": json.dumps(call["arguments"],
                                                              ensure_ascii=False)}}
                        for call in calls
                    ]
                out.append(entry)
            elif role == "tool":
                out.append({"role": "tool",
                            "tool_call_id": message.get("tool_call_id", ""),
                            "content": message.get("content") or "(no output)"})
        return out

    def complete(self, *, system: str, messages: list[dict],
                 tools: list[ToolSpec] | None = None,
                 max_tokens: int = 8192) -> AssistantTurn:
        kwargs: dict = {"model": self._model, "max_tokens": max_tokens,
                        "messages": self._messages(system, messages)}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {
                    "name": spec.name, "description": spec.description,
                    "parameters": spec.input_schema}}
                for spec in tools
            ]
        try:
            response = self._client.chat.completions.create(**kwargs)
        except self._openai.APIStatusError as exc:
            raise LLMError(f"{self.name} API error ({exc.status_code}): {exc.message}") from exc
        except self._openai.APIConnectionError as exc:
            raise LLMError(f"Could not reach {self.name}: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            calls.append(ToolCall(id=call.id or f"call_{index}",
                                  name=call.function.name, arguments=arguments))
        thinking = (getattr(message, "reasoning_content", None)
                    or getattr(message, "reasoning", "") or "")
        return AssistantTurn(text=message.content or "", tool_calls=calls,
                             stop_reason=choice.finish_reason or "end_turn",
                             thinking=str(thinking))

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
            response = self._client.chat.completions.create(
                model=self._model, max_tokens=4096,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": CLASSIFY_SYSTEM},
                          {"role": "user", "content": prompt}],
            )
        except self._openai.APIStatusError as exc:
            raise LLMError(f"{self.name} API error ({exc.status_code}): {exc.message}") from exc
        except self._openai.APIConnectionError as exc:
            raise LLMError(f"Could not reach {self.name}: {exc}") from exc

        text = response.choices[0].message.content or "{}"
        return parse_classifications(json_object(text))
