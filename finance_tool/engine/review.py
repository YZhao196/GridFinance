"""The review queue (§24).

A transaction the rules and the merchant memory could not settle becomes a persistent
question, not a silent guess. Resolving it *always* clears ``needs_review`` — including
when the answer is "leave it uncategorised" — because a queued row whose flag survives its
resolution is a row that reappears forever.
"""

from __future__ import annotations

from datetime import date, datetime

from finance_tool.engine import categorise as cat
from finance_tool.engine import detect as det
from finance_tool.store.entities import ReviewItem, Rule, Transaction
from finance_tool.store.store import Store, StoreView, new_id

OPEN = "open"
RESOLVED = "resolved"
SKIPPED = "skipped"


def question_for(txn: Transaction, suggested: str | None = None) -> str:
    label = txn.merchant or txn.description or "this transaction"
    amount = f"${abs(txn.amount):,.2f}"
    when = txn.date.isoformat() if txn.date else "an unknown date"
    base = f"What was '{label}' ({amount}) on {when}?"
    if suggested:
        return f"{base} I guessed {suggested} — is that right?"
    return f"{base} I couldn't tell from the description."


def find_open(view: StoreView, transaction_id: str) -> ReviewItem | None:
    for item in view.doc.reviews:
        if item.transaction_id == transaction_id and item.status == OPEN:
            return item
    return None


def queue(
    store: Store,
    txn: Transaction,
    *,
    question: str | None = None,
    reason: str = "low_confidence",
    options: list[str] | None = None,
    suggested_category: str = "",
    suggested_confidence: float | None = None,
    now: datetime | None = None,
) -> ReviewItem:
    """Ask about one transaction, or replace the question already asked about it."""
    moment = now or datetime.now()
    item = next((entry for entry in store.doc.reviews
                 if entry.transaction_id == txn.id), None)
    changed = item is None
    if item is None:
        item = ReviewItem(id=new_id("reviews"), transaction_id=txn.id, created_at=moment)
        store.doc.reviews.append(item)

    updates = {
        "question": question or question_for(txn, suggested_category or None),
        "reason": reason,
        "options": list(options or []),
        "suggested_category": suggested_category or "",
        "suggested_confidence": suggested_confidence,
        "status": OPEN,
        "answer": "",
        "resolved_category": "",
        "resolved_at": None,
    }
    for name, value in updates.items():
        if getattr(item, name) != value:
            setattr(item, name, value)
            changed = True
    if not txn.needs_review:
        txn.needs_review = True
        changed = True
    if changed:
        store.version += 1
    return item


def open_items(view: StoreView) -> list[ReviewItem]:
    found = [item for item in view.doc.reviews if item.status == OPEN]

    def order(item: ReviewItem):
        txn = view.transaction(item.transaction_id)
        return (txn.date if txn and txn.date else date.min, item.transaction_id)

    found.sort(key=order, reverse=True)
    return found


def open_count(view: StoreView) -> int:
    return sum(1 for item in view.doc.reviews if item.status == OPEN)


def close_for_transaction(store: Store, transaction_id: str, category: str = "", *,
                          answer: str = "", now: datetime | None = None) -> int:
    """Resolve every open question about a row, without touching the row itself."""
    moment = now or datetime.now()
    changed = 0
    for item in store.doc.reviews:
        if item.transaction_id != transaction_id or item.status != OPEN:
            continue
        item.status = RESOLVED
        item.answer = answer
        item.resolved_category = category or ""
        item.resolved_at = moment
        changed += 1
    if changed:
        store.version += 1
    return changed


def categorise_transaction(
    store: Store,
    transaction_id: str,
    category: str,
    *,
    source: str = "user",
    confidence: float = 1.0,
    answer: str = "",
    now: datetime | None = None,
) -> Transaction | None:
    """Set a category on one row and settle any question about it."""
    category = str(category or "").strip()
    if not category:
        return None
    txn = store.view().transaction(transaction_id)
    if txn is None:
        return None
    changed = (txn.category != category or txn.category_source != source
               or txn.category_confidence != confidence or txn.needs_review)
    txn.category = category
    txn.category_source = source
    txn.category_confidence = confidence
    txn.needs_review = False
    if source == "user" and txn.rule_id is not None:
        txn.rule_id = None
        changed = True
    close_for_transaction(store, txn.id, category, answer=answer, now=now)
    if changed:
        store.version += 1
    return txn


def resolve(
    store: Store,
    review_id: str,
    category: str | None,
    *,
    answer: str = "",
    make_rule: bool = False,
    rule_pattern: str | None = None,
    now: datetime | None = None,
) -> ReviewItem | None:
    """Answer one question. A null category is a real answer: it stays uncategorised."""
    item = store.get("reviews", review_id)
    if item is None or item.status != OPEN:
        return None
    txn = store.view().transaction(item.transaction_id)
    category = str(category).strip() if category is not None else ""

    if txn is not None and category:
        categorise_transaction(store, txn.id, category, source="user", confidence=1.0,
                               answer=answer, now=now)
        if make_rule:
            if rule_pattern is not None and str(rule_pattern).strip():
                pattern = str(rule_pattern).strip()
                match_type = "contains"
            elif txn.merchant:
                # Match the merchant, not the padded description: "Acme Pty Ltd" becomes
                # the key the next statement line will also reduce to.
                pattern = det.merchant_key(txn.merchant)
                match_type = "merchant"
            else:
                pattern = (txn.description or "").strip()
                match_type = "contains"
            if not pattern:
                pattern = (txn.description or "").strip()
                match_type = "contains"
            if pattern:
                rule = Rule(id=new_id("rules"), match=pattern, category=category,
                            priority=10, name=pattern, match_type=match_type)
                store.doc.rules.append(rule)
                store.version += 1
                cat.apply_rule_to_history(store, rule)
    elif txn is not None:
        # "I don't know" is still an answer; leaving the flag on would ask again forever.
        # A provisional model guess is cleared with it — the person just said no.
        if txn.category_source == "llm":
            txn.category = None
            txn.category_source = ""
            txn.category_confidence = None
        txn.needs_review = False

    item.status = RESOLVED
    item.answer = answer
    item.resolved_category = category or ""
    item.resolved_at = now or datetime.now()
    store.version += 1
    return item


def skip(store: Store, review_id: str, *, now: datetime | None = None) -> bool:
    item = store.get("reviews", review_id)
    if item is None or item.status != OPEN:
        return False
    item.status = SKIPPED
    item.resolved_at = now or datetime.now()
    txn = store.view().transaction(item.transaction_id)
    if txn is not None:
        # Skipping a question about a provisional model guess must not enact the guess.
        if txn.category_source == "llm":
            txn.category = None
            txn.category_source = ""
            txn.category_confidence = None
        txn.needs_review = False
    store.version += 1
    return True


def resolve_transaction(store: Store, transaction_id: str, category: str, *,
                        source: str = "user", answer: str = "",
                        now: datetime | None = None) -> Transaction | None:
    """Categorise a row from outside the queue (an editor, or the assistant's tool)."""
    return categorise_transaction(store, transaction_id, category, source=source,
                                  confidence=1.0, answer=answer, now=now)
