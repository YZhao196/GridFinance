"""Pattern finding in the transaction history (§17).

Detection **proposes; it never writes.** Every result here is a suggestion the user
accepts or dismisses, so nothing in this module returns something the caller is expected
to persist unchecked.

Everything groups by :func:`merchant_key`, which is what makes a bank description a
stable identity across the incidental noise — card references, store numbers, dates.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Sequence

from finance_tool.engine import recurrence as rec
from finance_tool.store.entities import EXPENSE, Item, Transaction
from finance_tool.store.store import StoreView

# Tokens that carry no identity: payment rails, jurisdictions, and legal suffixes a bank
# prints but a person never means.
NOISE = frozenset({
    "PURCHASE", "PAYMENT", "PAYMENTS", "CARD", "VISA", "MASTERCARD", "AMEX", "EFTPOS",
    "DEBIT", "CREDIT", "TRANSFER", "DIRECT", "POS", "ATM", "AUTH", "REF", "TXN",
    "WITHDRAWAL", "DEPOSIT", "ONLINE", "MOBILE", "BPAY", "OSKO", "PAYID",
    "AU", "AUS", "AUD", "NZ", "USA", "PTY", "LTD", "LIMITED", "INC", "CO", "THE",
    "AND", "SUB", "COM", "WWW", "NET", "ORG", "LLC",
})

# A payment counts as regular when its gaps vary by less than this much.
REGULARITY_TOLERANCE = 0.35
MIN_INTERVAL_DAYS = 5
MAX_KEY_TOKENS = 4


def merchant_key(description: str) -> str:
    """A bank description normalised to a stable merchant identity.

    Card references, store numbers and dates are digits and go; so does the payment-rail
    vocabulary. What is left is the merchant. An all-noise description yields ``""``,
    and callers skip those rather than grouping everything unknown together.
    """
    if not description:
        return ""
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", description.upper())
    tokens = [
        token for token in text.split()
        if not any(ch.isdigit() for ch in token) and token not in NOISE
    ]
    return " ".join(tokens[:MAX_KEY_TOKENS])


def _interval_stats(dates: Sequence[date]) -> tuple[float | None, float | None]:
    """Median gap in days and its coefficient of variation."""
    ordered = sorted(dates)
    if len(ordered) < 2:
        return None, None
    gaps = [(b - a).days for a, b in zip(ordered, ordered[1:])]
    median = statistics.median(gaps)
    if median <= 0:
        return median, None
    spread = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
    return median, spread / median


def is_regular(dates: Sequence[date], tolerance: float = REGULARITY_TOLERANCE) -> bool:
    median, variation = _interval_stats(dates)
    if median is None or variation is None:
        return False
    return median >= MIN_INTERVAL_DAYS and variation <= tolerance


def monthly_estimate(amount: float, interval_days: float | None) -> float:
    """What a charge of ``amount`` every ``interval_days`` costs per month."""
    if not interval_days or interval_days <= 0:
        return 0.0
    return amount * (365.25 / 12) / interval_days


# -------------------------------------------------------------------------- grouping


@dataclass(frozen=True)
class RecurringGroup:
    """Historical occurrences of one merchant, and whether they look like a schedule."""

    merchant: str
    transactions: list[Transaction] = field(default_factory=list)
    interval_days: float | None = None
    average_amount: float = 0.0
    regular: bool = False
    tracked: bool = False
    matching_def_ids: list[str] = field(default_factory=list)

    @property
    def occurrences(self) -> int:
        return len(self.transactions)

    @property
    def first_date(self) -> date | None:
        return min((t.date for t in self.transactions if t.date), default=None)

    @property
    def last_date(self) -> date | None:
        return max((t.date for t in self.transactions if t.date), default=None)

    @property
    def monthly_estimate(self) -> float:
        return monthly_estimate(self.average_amount, self.interval_days)

    @property
    def total(self) -> float:
        return sum(abs(t.amount) for t in self.transactions)


def group_by_merchant(
    transactions: Iterable[Transaction],
    *,
    expenses_only: bool = True,
) -> dict[str, list[Transaction]]:
    groups: dict[str, list[Transaction]] = {}
    for txn in transactions:
        if txn.date is None:
            continue
        if expenses_only and txn.amount >= 0:
            continue
        key = merchant_key(txn.description)
        if not key:
            continue
        groups.setdefault(key, []).append(txn)
    for bucket in groups.values():
        bucket.sort(key=lambda t: t.date)
    return groups


def matching_definitions(view: StoreView, key: str) -> list[str]:
    """Ledger items that plausibly *are* this merchant.

    Containment both ways, because banks pad and users abbreviate: a transaction key of
    "INTERNET PROVIDER" should find the item "Internet", and an item "Rent" should find
    a transaction key of "RENT".
    """
    if not key:
        return []
    found: list[str] = []
    for item in view.items:
        if item.type != EXPENSE:
            continue
        item_key = merchant_key(item.name)
        if not item_key:
            continue
        if item_key == key or item_key in key or key in item_key:
            found.append(item.id)
    return found


def recurring_payments(
    view: StoreView,
    merchant: str | None = None,
    *,
    min_occurrences: int = 2,
) -> list[RecurringGroup]:
    """Grouped historical occurrences, most expensive first."""
    groups = group_by_merchant(view.transactions)
    found: list[RecurringGroup] = []
    for key, txns in groups.items():
        if merchant is not None and key != merchant:
            continue
        if len(txns) < min_occurrences:
            continue
        dates = [t.date for t in txns if t.date]
        median, _ = _interval_stats(dates)
        matches = matching_definitions(view, key)
        found.append(RecurringGroup(
            merchant=key,
            transactions=list(txns),
            interval_days=median,
            average_amount=sum(abs(t.amount) for t in txns) / len(txns),
            regular=is_regular(dates),
            tracked=bool(matches),
            matching_def_ids=matches,
        ))
    found.sort(key=lambda group: (-group.monthly_estimate, group.merchant))
    return found


# ----------------------------------------------------------------- detection proper


@dataclass(frozen=True)
class UntrackedCharge:
    """A recurring charge the ledger does not know about (§17)."""

    merchant: str
    occurrences: int
    average_amount: float
    interval_days: float | None
    first_date: date | None
    last_date: date | None
    transaction_ids: list[str] = field(default_factory=list)

    @property
    def monthly_estimate(self) -> float:
        return monthly_estimate(self.average_amount, self.interval_days)

    @property
    def yearly_estimate(self) -> float:
        return self.monthly_estimate * 12

    @property
    def suggested_name(self) -> str:
        return self.merchant.title()


def detect_subscriptions(
    view: StoreView,
    *,
    today: date | None = None,
    min_occurrences: int = 2,
    tolerance: float = REGULARITY_TOLERANCE,
) -> list[UntrackedCharge]:
    """Merchant keys recurring on a regular interval with no matching ledger item.

    Surfaced as "this looks like a subscription you haven't tracked", and nothing more —
    the suggestion is a row the user accepts or dismisses, never an item that appears.
    """
    found: list[UntrackedCharge] = []
    for key, txns in group_by_merchant(view.transactions).items():
        if len(txns) < min_occurrences:
            continue
        dates = [t.date for t in txns if t.date]
        if not is_regular(dates, tolerance):
            continue
        if matching_definitions(view, key):
            continue
        median, _ = _interval_stats(dates)
        found.append(UntrackedCharge(
            merchant=key,
            occurrences=len(txns),
            average_amount=sum(abs(t.amount) for t in txns) / len(txns),
            interval_days=median,
            first_date=min(dates),
            last_date=max(dates),
            transaction_ids=[t.id for t in txns],
        ))
    found.sort(key=lambda charge: -charge.monthly_estimate)
    return found


@dataclass(frozen=True)
class UntrackedSpend:
    charges: list[UntrackedCharge] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.charges)

    @property
    def monthly_total(self) -> float:
        return sum(c.monthly_estimate for c in self.charges)

    @property
    def yearly_total(self) -> float:
        return self.monthly_total * 12

    @property
    def is_empty(self) -> bool:
        return not self.charges


def untracked_spend(view: StoreView, *, today: date | None = None) -> UntrackedSpend:
    return UntrackedSpend(charges=detect_subscriptions(view, today=today))


# ------------------------------------------------------------------- renewal timeline


def upcoming_renewals(
    view: StoreView,
    today: date,
    *,
    days: int = 60,
    subscriptions_only: bool = False,
) -> list[rec.Occurrence]:
    """Expense occurrences landing in the next ``days``, in due-date order.

    Leaves only, as every other plan total is: a category node holding children is a
    group, and counting it here as well would make the timeline disagree with the ledger
    about the same plan.
    """
    from finance_tool.engine.ledger import leaf_items

    horizon = today + timedelta(days=max(0, days))
    found: list[rec.Occurrence] = []
    for item in leaf_items(view.items):
        if item.type != EXPENSE:
            continue
        if subscriptions_only and not item.subscription:
            continue
        if item.recurrence is None and not item.subscription:
            # A one-off is not a renewal; the timeline is for things that come back.
            continue
        found.extend(rec.occurrences(item, today, horizon))
    found.sort(key=rec.sort_key)
    return found


def renewal_count(view: StoreView, today: date, *, days: int = 60) -> int:
    return len(upcoming_renewals(view, today, days=days))


def days_until_next(item: Item, today: date) -> int | None:
    nxt = rec.next_occurrence(item, today)
    return None if nxt is None else (nxt - today).days
