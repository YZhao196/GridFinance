"""Money moving between the user's own accounts (§24).

A transfer leaves one account and lands in another within a few days for the same amount.
Left alone it is counted twice — once as spending, once as income — so a matched pair is
filed under the transfer-kind category the analytics already exclude.
"""

from __future__ import annotations

from dataclasses import dataclass

from finance_tool.engine import categories as cats
from finance_tool.engine import review
from finance_tool.store.store import Store, StoreView

WINDOW_DAYS = 3
MIN_AMOUNT = 1.0
PROTECTED_SOURCES = {"user", "rule"}


@dataclass(frozen=True)
class TransferPair:
    debit_id: str
    credit_id: str
    amount: float
    day_gap: int
    from_account: str
    to_account: str


def _eligible(debit, credit) -> bool:
    if cats.is_transfer(debit.category) and cats.is_transfer(credit.category):
        # Pairing two rows that are already filed does no work and can consume the
        # credit an unclaimed counterpart needed.
        return False
    return (credit.id != debit.id
            and credit.account != debit.account
            and debit.date is not None and credit.date is not None
            and abs((credit.date - debit.date).days) <= WINDOW_DAYS)


def _pair_amount(debits: list, credits: list) -> list[tuple]:
    """A maximum one-to-one matching for one amount, not first-fit.

    First-fit loses pairs: a debit that could take either of two credits can grab the only
    one a third debit was able to use. Augmenting paths find the largest set, so the
    amount of money counted as both spending and income is as small as the data allows.
    """
    match: dict[str, object] = {}          # credit id -> debit

    def augment(debit, seen: set[str]) -> bool:
        for credit in credits:
            if credit.id in seen or not _eligible(debit, credit):
                continue
            seen.add(credit.id)
            holder = match.get(credit.id)
            if holder is None or augment(holder, seen):
                match[credit.id] = debit
                return True
        return False

    for debit in debits:
        augment(debit, set())
    return [(match[credit.id], credit) for credit in credits if credit.id in match]


def detect_transfers(view: StoreView) -> list[TransferPair]:
    """One-to-one pairs of equal-and-opposite rows across different accounts."""
    debits: dict[float, list] = {}
    credits: dict[float, list] = {}

    for txn in view.transactions:
        if txn.date is None or txn.amount == 0:
            continue
        # A person's or a rule's non-transfer category is protected. A row already filed
        # as a transfer stays eligible: its counterpart may still be unclaimed, and
        # pairing it is what stops that counterpart counting as income or spending.
        if (txn.category_source in PROTECTED_SOURCES
                and not cats.is_transfer(txn.category)):
            continue
        if abs(txn.amount) < MIN_AMOUNT:
            continue
        amount = round(abs(txn.amount), 2)
        bucket = debits if txn.amount < 0 else credits
        bucket.setdefault(amount, []).append(txn)

    pairs: list[TransferPair] = []
    for amount in sorted(set(debits) & set(credits)):
        left = sorted(debits[amount], key=lambda txn: (txn.date, txn.id))
        right = sorted(credits[amount], key=lambda txn: (txn.date, txn.id))
        for debit, credit in _pair_amount(left, right):
            pairs.append(TransferPair(
                debit_id=debit.id, credit_id=credit.id, amount=amount,
                day_gap=abs((credit.date - debit.date).days),
                from_account=debit.account, to_account=credit.account,
            ))
    pairs.sort(key=lambda pair: (pair.debit_id, pair.credit_id))
    return pairs


def apply_transfers(store: Store, pairs: list[TransferPair] | None = None) -> int:
    """File matched pairs as transfers, never overwriting a person or a rule."""
    chosen = detect_transfers(store.view()) if pairs is None else list(pairs)
    by_id = {txn.id: txn for txn in store.doc.transactions}
    applied = 0

    for pair in chosen:
        sides = [by_id.get(pair.debit_id), by_id.get(pair.credit_id)]
        if any(txn is None for txn in sides):
            continue
        if any(txn.category_source in PROTECTED_SOURCES
               and not cats.is_transfer(txn.category) for txn in sides):
            # A person's or a rule's non-transfer category is not ours to rewrite.
            continue
        changed = False
        for txn in sides:
            if cats.is_transfer(txn.category):
                continue                 # already filed; leave its source alone
            txn.category = cats.TRANSFERS
            txn.category_source = "detected"
            txn.category_confidence = 0.9
            txn.needs_review = False
            review.close_for_transaction(store, txn.id, cats.TRANSFERS)
            changed = True
        if changed:
            applied += 1

    if applied:
        store.version += 1
    return applied
