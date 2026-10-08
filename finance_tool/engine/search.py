"""One function, one index (§18).

Ranking is exact prefix, then word prefix, then substring, then fuzzy; ties broken by
recency. Every result carries its own target, so the palette can jump straight there
without re-deriving what the user must have meant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from finance_tool.store.store import StoreView

ITEM = "item"
TRANSACTION = "transaction"
PERSON = "person"
ACCOUNT = "account"
GOAL = "goal"
CONSUMABLE = "consumable"
RULE = "rule"
CANVAS = "canvas"

SCORE_EXACT = 1.00
SCORE_PREFIX = 0.90
SCORE_WORD_PREFIX = 0.80
SCORE_SUBSTRING = 0.60
SCORE_FUZZY = 0.35


@dataclass(frozen=True)
class Result:
    """One search hit, with enough context to render a row and navigate to it."""

    kind: str
    id: str
    title: str
    subtitle: str = ""
    score: float = 0.0
    recency: float = 0.0
    target: dict[str, Any] = field(default_factory=dict)

    @property
    def navigation(self) -> dict[str, Any]:
        return self.target or {"kind": self.kind, "id": self.id}


def match_score(text: str | None, query: str) -> float | None:
    """How well ``text`` answers ``query``, or ``None`` for no match at all.

    Ordered from strictest to loosest, so an exact title always outranks a fuzzy match
    buried in a note.
    """
    if not text or not query:
        return None
    haystack = text.casefold()
    needle = query.casefold()

    if haystack == needle:
        return SCORE_EXACT
    if haystack.startswith(needle):
        return SCORE_PREFIX
    if any(word.startswith(needle) for word in haystack.split()):
        return SCORE_WORD_PREFIX
    if needle in haystack:
        return SCORE_SUBSTRING
    if _subsequence(needle, haystack):
        return SCORE_FUZZY
    return None


def _subsequence(needle: str, haystack: str) -> bool:
    """Query characters appear in ``haystack`` in order, with gaps allowed."""
    cursor = 0
    for char in haystack:
        if cursor < len(needle) and char == needle[cursor]:
            cursor += 1
    return cursor == len(needle)


def _score(title: str | None, others: Iterable[str | None], query: str) -> float | None:
    """A row's score: its own name at full weight, everything else capped.

    The cap is what stops a buried tag from outranking a title. Capping the *others*
    rather than the whole row matters: a transaction whose description prefixes the query
    keeps its 0.9, even if its category happens to be an exact match on a weaker field.
    """
    primary = match_score(title, query) or 0.0
    secondary = max((match_score(text, query) or 0.0 for text in others), default=0.0)
    score = max(primary, min(secondary, SCORE_SUBSTRING))
    return score or None


def _recency(value: date | datetime | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        value = value.date()
    return value.toordinal()


def search(view: StoreView, query: str, *, limit: int = 40) -> list[Result]:
    """Match across item names, notes, tags, categories, transaction descriptions,
    people, accounts, goals, consumables, rules and canvas names.
    """
    text = (query or "").strip()
    if not text:
        return []

    found: list[Result] = []
    found.extend(_items(view, text))
    found.extend(_transactions(view, text))
    found.extend(_people(view, text))
    found.extend(_accounts(view, text))
    found.extend(_goals(view, text))
    found.extend(_consumables(view, text))
    found.extend(_rules(view, text))
    found.extend(_canvases(view, text))

    found.sort(key=lambda r: (-r.score, -r.recency, r.title.casefold(), r.id))
    return found[:limit] if limit else found


# --------------------------------------------------------------------------- sources


def _items(view: StoreView, query: str) -> list[Result]:
    results: list[Result] = []
    for item in view.items:
        score = _score(item.name, [item.category, item.note, *item.tags], query)
        if score is None:
            continue
        results.append(Result(
            kind=ITEM,
            id=item.id,
            title=item.name,
            subtitle=item.category or ("Income" if item.is_income else "Expense"),
            score=score,
            recency=_recency(item.updated_at or item.created_at),
            target={"kind": ITEM, "id": item.id},
        ))
    return results


def _transactions(view: StoreView, query: str) -> list[Result]:
    results: list[Result] = []
    for txn in view.transactions:
        score = _score(txn.description, [txn.category, *txn.tags], query)
        if score is None:
            continue
        results.append(Result(
            kind=TRANSACTION,
            id=txn.id,
            title=txn.description,
            subtitle=f"{txn.date.isoformat() if txn.date else '—'} · {txn.category or 'Uncategorised'}",
            score=score,
            recency=_recency(txn.date),
            target={"kind": TRANSACTION, "id": txn.id, "account": txn.account},
        ))
    return results


def _people(view: StoreView, query: str) -> list[Result]:
    results: list[Result] = []
    for person in view.people:
        score = _score(person.name, [person.email, person.phone], query)
        if score is None:
            continue
        results.append(Result(
            kind=PERSON, id=person.id, title=person.name,
            subtitle=person.email or person.phone or "Shared plan",
            score=score, target={"kind": PERSON, "id": person.id},
        ))
    return results


def _accounts(view: StoreView, query: str) -> list[Result]:
    results: list[Result] = []
    for account in view.accounts:
        score = _score(account.name, [account.kind, account.currency], query)
        if score is None:
            continue
        results.append(Result(
            kind=ACCOUNT, id=account.id, title=account.name,
            subtitle=f"{account.currency} · {account.kind}".strip(" ·"),
            score=score, target={"kind": ACCOUNT, "id": account.id},
        ))
    return results


def _goals(view: StoreView, query: str) -> list[Result]:
    return [
        Result(kind=GOAL, id=goal.id, title=goal.name,
               subtitle=f"Target {goal.target:,.2f} · saved {goal.saved:,.2f}",
               score=score, target={"kind": GOAL, "id": goal.id})
        for goal in view.goals
        if (score := match_score(goal.name, query)) is not None
    ]


def _consumables(view: StoreView, query: str) -> list[Result]:
    return [
        Result(kind=CONSUMABLE, id=item.id, title=item.name,
               subtitle=f"{item.quantity:g} uses at {item.per_use_amount:,.2f}",
               score=score, target={"kind": CONSUMABLE, "id": item.id})
        for item in view.tracker
        if (score := match_score(item.name, query)) is not None
    ]


def _rules(view: StoreView, query: str) -> list[Result]:
    results: list[Result] = []
    for rule in view.rules:
        score = _score(rule.match, [rule.category, *rule.tags], query)
        if score is None:
            continue
        results.append(Result(
            kind=RULE, id=rule.id, title=rule.match,
            subtitle=f"→ {rule.category}" if rule.category else "→ tags",
            score=score, target={"kind": RULE, "id": rule.id},
        ))
    return results


def _canvases(view: StoreView, query: str) -> list[Result]:
    return [
        Result(kind=CANVAS, id=canvas.id, title=canvas.name,
               subtitle=f"{len(canvas.widgets)} widgets",
               score=score, target={"kind": CANVAS, "id": canvas.id},
               recency=float(len(canvas.widgets)))
        for canvas in view.settings.canvases
        if (score := match_score(canvas.name, query)) is not None
    ]


def kinds_present(results: Iterable[Result]) -> list[str]:
    """The kinds in a result set, in first-appearance order — for grouping a palette."""
    seen: list[str] = []
    for result in results:
        if result.kind not in seen:
            seen.append(result.kind)
    return seen
