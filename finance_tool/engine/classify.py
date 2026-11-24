"""Staged categorisation: rules, then merchant memory, then (optionally) a model (§24).

The first two stages are local and decide most rows. Only what remains is sent to a
classifier, in chunks, and a low-confidence answer is stored as a *suggestion* with the
row queued rather than presented as a decision. With no classifier configured every
remaining row is queued with reason ``no_ai`` — the app still works, it just asks.

Nothing here saves. The caller decides when the store becomes durable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from finance_tool.engine import categories as cats
from finance_tool.engine import categorise as cat
from finance_tool.engine import review
from finance_tool.store.entities import Transaction
from finance_tool.store.store import Store, StoreView

CHUNK_SIZE = 40
QUEUE_OPTIONS = 8


class ClassifyError(RuntimeError):
    """A classifier could not answer. The rows are queued, not lost."""


@dataclass(frozen=True)
class Classification:
    index: int
    category: str
    confidence: float
    reason: str = ""


class Classifier(Protocol):
    def classify(self, transactions: list[dict], categories: list[str]) -> list[Classification]:
        ...


@dataclass
class ClassifyStats:
    rules: int = 0
    memory: int = 0
    ai: int = 0
    queued: int = 0
    remaining: int = 0
    error: str | None = None


def category_options(view: StoreView) -> list[str]:
    """What a classifier may choose from.

    Transfer kinds and the uncategorised bucket are not categories to *decide*: the first
    two vanish from spending totals, and filing a row as "Uncategorised" marks it done
    without answering anything.
    """
    names = set(cats.names()) | set(cat.categories_in_use(view))
    return sorted((name for name in names
                   if name != cats.UNCATEGORISED and not cats.is_transfer(name)),
                  key=str.casefold)


def _dismissed(view: StoreView, txn: Transaction) -> bool:
    """A question answered with "leave it" must not be asked again."""
    return any(item.transaction_id == txn.id
               and item.status in {review.RESOLVED, review.SKIPPED}
               and not item.resolved_category
               for item in view.doc.reviews)


def pending(view: StoreView, *, import_id: str | None = None,
            limit: int = 200) -> list[Transaction]:
    found = []
    for txn in view.transactions:
        if import_id is not None and txn.import_id != import_id:
            continue
        fresh = not txn.category and not txn.needs_review
        retry = txn.needs_review and not txn.category_source
        if (fresh or retry) and not _dismissed(view, txn):
            found.append(txn)
    found.sort(key=lambda txn: (txn.date is not None, txn.date or date.min, txn.id),
               reverse=True)
    return found[:limit] if limit else found


def _apply(store: Store, txn: Transaction, category: str, source: str,
           confidence: float, *, rule_id: str | None = None,
           tags: list[str] | None = None, now: datetime | None) -> None:
    txn.category = category
    txn.category_source = source
    txn.category_confidence = confidence
    txn.rule_id = rule_id
    if tags is not None:
        txn.tags = list(tags)
    txn.needs_review = False
    review.close_for_transaction(store, txn.id, category, now=now)
    store.version += 1


def _queue(store: Store, txn: Transaction, *, reason: str, options: list[str],
           suggested: str = "", confidence: float | None = None,
           now: datetime | None) -> None:
    review.queue(store, txn, reason=reason, options=options,
                 suggested_category=suggested, suggested_confidence=confidence, now=now)


def classify_pending(
    store: Store,
    *,
    classifier: Classifier | None = None,
    limit: int = 200,
    import_id: str | None = None,
    now: datetime | None = None,
) -> ClassifyStats:
    """Run rules → memory → the classifier over the rows that still need an answer."""
    stats = ClassifyStats()
    options = category_options(store.view())
    candidates = pending(store.view(), import_id=import_id, limit=limit)

    remaining: list[Transaction] = []
    for txn in candidates:
        decision = cat.categorise(txn.description, store.doc.rules,
                                  merchant=txn.merchant, amount=txn.amount,
                                  count_hit=True)
        if decision.tags:
            # Tags from category-less rules apply even when a category is still unknown.
            txn.tags = list(decision.tags)
        if decision.categorised:
            _apply(store, txn, decision.category, "rule", 1.0,
                   rule_id=decision.rule_id, tags=decision.tags, now=now)
            stats.rules += 1
            continue
        memory = cat.memory_category(store.view(), txn.description, txn.merchant,
                                     exclude_id=txn.id)
        if memory is not None:
            _apply(store, txn, memory.category, "memory", memory.confidence, now=now)
            stats.memory += 1
            continue
        remaining.append(txn)

    if classifier is None:
        for txn in remaining:
            _queue(store, txn, reason="no_ai", options=options[:QUEUE_OPTIONS], now=now)
            stats.queued += 1
    else:
        for start in range(0, len(remaining), CHUNK_SIZE):
            chunk = remaining[start:start + CHUNK_SIZE]
            payload = [{
                "date": txn.date.isoformat() if txn.date else None,
                "description": txn.description,
                "merchant": txn.merchant,
                "amount": txn.amount,
            } for txn in chunk]
            try:
                results = classifier.classify(payload, options)
            except Exception as exc:  # a provider failure must not lose the chunk
                stats.error = str(exc)
                for txn in chunk:
                    _queue(store, txn, reason="ai_error",
                           options=options[:QUEUE_OPTIONS], now=now)
                    stats.queued += 1
                continue

            by_index = {item.index: item for item in results}
            for index, txn in enumerate(chunk):
                item = by_index.get(index)
                if item is None or item.category not in options:
                    _queue(store, txn, reason="unknown",
                           options=options[:QUEUE_OPTIONS], now=now)
                    stats.queued += 1
                    continue
                if item.confidence >= store.settings.review_threshold:
                    _apply(store, txn, item.category, "llm", item.confidence, now=now)
                    stats.ai += 1
                    continue
                # Keep the guess visible, but ask: a wrong low-confidence category is
                # worse than an honest question.
                txn.category = item.category
                txn.category_source = "llm"
                txn.category_confidence = item.confidence
                txn.rule_id = None
                alternatives = [item.category] + [name for name in options
                                                  if name != item.category]
                _queue(store, txn, reason="low_confidence",
                       options=alternatives[:QUEUE_OPTIONS],
                       suggested=item.category, confidence=item.confidence, now=now)
                stats.queued += 1

    view = store.view()
    stats.remaining = sum(
        1 for txn in view.transactions
        if not txn.category and not txn.needs_review and not _dismissed(view, txn))
    return stats
