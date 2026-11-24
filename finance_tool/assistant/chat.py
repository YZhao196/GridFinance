"""The assistant conversation loop (§24).

The model may look things up and, when the user has allowed it, annotate the ledger.
Every tool result is persisted too, so a reopened conversation shows what was asked and
what came back rather than a reply with no evidence.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from finance_tool.assistant.context import finance_context
from finance_tool.assistant.tools import TOOL_SPECS, WRITE_TOOLS, execute_tool
from finance_tool.engine import exports
from finance_tool.llm.base import (
    LLMError,
    Provider,
    neutral_assistant,
    neutral_tool_result,
    neutral_user,
)
from finance_tool.store.entities import ChatMessage
from finance_tool.store.store import Store, new_id

MAX_TOOL_ROUNDS = 8
HISTORY_LIMIT = 40

SYSTEM_TEMPLATE = """You are the user's private personal finance assistant, running locally on their own machine.

Today is {today}. Amounts are in {currency}; negative means money left the account.

How to work:
- Use the tools to look things up. Never invent a balance, merchant, category or amount.
- When asked "how much did I spend", use the summary tool, then drill in with search.
- Be concise and concrete. Give the number, then the one thing that explains it.
- If a description is ambiguous, say so and use the review tools rather than guessing.
- You may assign categories and create rules, but if a change would touch more than a
  handful of transactions, explain what you are about to do first.
- Never move money, and never claim to have contacted a bank.
- Markdown is rendered by the chat window, so bold, italics and lists are fine.

The user's profile:
{profile}

Current snapshot:
{context}
"""


def _profile_text(store: Store) -> str:
    if store.data_dir is None:
        return ""
    path = exports.ensure_profile(store.data_dir / "profile.md")
    text = path.read_text(encoding="utf-8").strip()
    return text[:6000] + ("\n(truncated)" if len(text) > 6000 else "")


def build_system(store: Store, *, today: date) -> str:
    return SYSTEM_TEMPLATE.format(
        today=today.isoformat(),
        currency=store.settings.currency or "AUD",
        profile=_profile_text(store) or "(empty)",
        context=finance_context(store.view(), today=today),
    )


def load_history(store: Store, limit: int = HISTORY_LIMIT) -> list[dict]:
    messages: list[dict] = []
    for entry in store.doc.messages[-limit:]:
        if entry.role == "tool":
            meta = entry.tool_calls[0] if entry.tool_calls else {}
            messages.append({
                "role": "tool",
                "tool_call_id": meta.get("tool_call_id", ""),
                "name": meta.get("name", ""),
                "content": entry.content,
                "is_error": bool(meta.get("is_error")),
            })
        elif entry.role == "assistant":
            messages.append({"role": "assistant", "content": entry.content,
                             "tool_calls": list(entry.tool_calls),
                             "thinking": entry.thinking,
                             "raw": entry.raw or None})
        else:
            messages.append({"role": "user", "content": entry.content})

    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    # An interrupted turn can leave an assistant tool call without its results, or a
    # provider failure can leave two user messages in a row. Both are rejected by the
    # providers, so the transcript is repaired before it is replayed.
    while messages and (
            messages[-1]["role"] == "tool"
            or (messages[-1]["role"] == "assistant" and messages[-1].get("tool_calls"))):
        messages.pop()
        while messages and messages[-1]["role"] == "tool":
            messages.pop()
    merged: list[dict] = []
    for message in messages:
        if (message["role"] == "user" and merged and merged[-1]["role"] == "user"):
            merged[-1] = {"role": "user",
                          "content": f"{merged[-1]['content']}\n\n{message['content']}"}
        else:
            merged.append(message)
    return merged


def _persist(store: Store, role: str, content: str, *, thinking: str = "",
             tool_calls: list[dict] | None = None, raw: list[dict] | None = None,
             now: datetime | None = None) -> None:
    store.doc.messages.append(ChatMessage(
        id=new_id("messages"), role=role, content=content, thinking=thinking,
        tool_calls=list(tool_calls or []), raw=list(raw or []),
        created_at=now or datetime.now(),
    ))
    store.version += 1


def clear(store: Store) -> int:
    count = len(store.doc.messages)
    store.doc.messages.clear()
    if count:
        store.version += 1
    return count


def send(
    store: Store,
    user_text: str,
    *,
    provider: Provider | None,
    today: date,
    now: datetime | None = None,
    save: bool = False,
) -> dict:
    """One user turn, including whatever tool rounds the model needs.

    ``today`` is required so a conversation is reproducible; ``now`` only stamps rows.
    """
    if provider is None:
        raise LLMError("No AI provider is configured. Choose one in Settings.")

    moment = now or datetime.now()
    revision_taken = False
    messages = load_history(store)
    if messages and messages[-1]["role"] == "user":
        # The previous turn never got an answer; the provider requires alternation.
        messages[-1] = {"role": "user",
                        "content": f"{messages[-1]['content']}\n\n{user_text}"}
    else:
        messages.append(neutral_user(user_text))
    _persist(store, "user", user_text, now=moment)

    system = build_system(store, today=today)
    reply_text = ""
    used: list[str] = []

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        final = round_index >= MAX_TOOL_ROUNDS
        # The last call is made without tools, so the model has to answer rather than
        # asking for work nobody can do.
        turn = provider.complete(system=system, messages=messages,
                                 tools=None if final else TOOL_SPECS)
        messages.append(neutral_assistant(turn))
        if turn.text:
            reply_text = turn.text

        if not turn.tool_calls:
            _persist(store, "assistant", turn.text, thinking=turn.thinking,
                     raw=turn.raw if isinstance(turn.raw, list) else None, now=moment)
            break
        if final:
            # Do not record a tool call that will never receive its result.
            _persist(store, "assistant", turn.text, thinking=turn.thinking,
                     raw=turn.raw if isinstance(turn.raw, list) else None, now=moment)
            break

        results = []
        for call in turn.tool_calls:
            used.append(call.name)
            if (not revision_taken and call.name in WRITE_TOOLS
                    and store.settings.ai_writes and store.data_dir is not None):
                try:
                    store.revision("before-ai")
                    revision_taken = True
                except Exception:
                    pass                 # a scratch or read-only store has no history
            if (call.name == "query_sql" and not store.read_only
                    and store.data_dir is not None):
                # SQL reads the database. The current round's transcript is not written
                # yet, so this save cannot leave a tool call without its result.
                store.save()
            try:
                result = execute_tool(store, call.name, call.arguments,
                                      today=today, now=moment)
                is_error = False
            except Exception as exc:  # a failed tool is reported to the model
                result = json.dumps({"error": str(exc)})
                is_error = True
            results.append((call, result, is_error))
            messages.append(neutral_tool_result(call, result, is_error))

        _persist(store, "assistant", turn.text, thinking=turn.thinking,
                 tool_calls=[{"id": call.id, "name": call.name,
                              "arguments": call.arguments}
                             for call in turn.tool_calls],
                 raw=turn.raw if isinstance(turn.raw, list) else None, now=moment)
        for call, result, is_error in results:
            _persist(store, "tool", result, tool_calls=[
                {"tool_call_id": call.id, "name": call.name, "is_error": is_error}
            ], now=moment)

    if save:
        store.save()
    return {"reply": reply_text or "(the model returned no text)", "tools_used": used}
