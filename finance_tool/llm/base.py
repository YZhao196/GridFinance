"""Provider-neutral messages and tool definitions for the assistant (§24).

The chat loop, the classifier and the MCP server all speak this shape. A provider
translates it to its own wire format and back; nothing above this module knows which
vendor answered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """A provider could not answer. Never swallowed at the assistant boundary."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    thinking: str = ""
    raw: Any = None


@dataclass(frozen=True)
class Classification:
    index: int
    category: str
    confidence: float
    reason: str = ""


class Provider(Protocol):
    name: str

    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 8192,
    ) -> AssistantTurn: ...

    def classify(self, transactions: list[dict], categories: list[str]) -> list[Classification]: ...


def neutral_user(text: str) -> dict:
    return {"role": "user", "content": text}


def neutral_assistant(turn: AssistantTurn) -> dict:
    return {
        "role": "assistant",
        "content": turn.text,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in turn.tool_calls
        ],
        "thinking": turn.thinking,
        "raw": turn.raw,
    }


def neutral_tool_result(call: ToolCall, content: str, is_error: bool = False) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": content,
        "is_error": is_error,
    }


def json_object(text: str) -> dict:
    """The JSON object in a reply, tolerating fences and a sentence around it."""
    body = (text or "").strip()
    if body.startswith("```"):
        if "\n" in body:
            body = body.split("\n", 1)[1]
        else:
            body = body[3:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        body = body.strip()
    start = body.find("{")
    end = body.rfind("}")
    if start != -1 and end != -1 and end > start:
        body = body[start:end + 1]
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LLMError(f"the model returned invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise LLMError("the model returned JSON that was not an object")
    return data


def parse_classifications(data: Any) -> list[Classification]:
    """Tolerant parsing of the classifier's JSON, dropping entries it cannot read."""
    items = data.get("classifications") if isinstance(data, dict) else None
    out: list[Classification] = []
    for item in items or []:
        try:
            out.append(Classification(
                index=int(item["index"]),
                category=str(item["category"]),
                confidence=max(0.0, min(1.0, float(item["confidence"]))),
                reason=str(item.get("reason") or ""),
            ))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out
