"""Shared costs, splits and settle-up (§16).

The subtlety is that sharing is **per occurrence**, not per item. "Has Sam paid me back"
is a question about a specific bill, so the paid state lives per person per occurrence
and this module never answers it in aggregate without saying which occurrences it summed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

from finance_tool.engine import recurrence as rec
from finance_tool.store.entities import EXPENSE, Item, Person
from finance_tool.store.store import StoreView

EVEN = "even"


# ------------------------------------------------------------------ split arithmetic


def split_members(defn: Item) -> list[str]:
    shared = defn.shared or {}
    return [str(member) for member in (shared.get("members") or [])]


def is_shared(defn: Item) -> bool:
    return bool(split_members(defn))


def member_shares(defn: Item, occ: date) -> dict[str, float]:
    """What each named member owes for one occurrence of ``defn``.

    An **even** split divides by the members *plus the user*, because the user is one of
    the people consuming the subscription. Splitting $12.99 three ways across two
    housemates and yourself gives $4.33 each, not $6.50 each — and it is the difference
    between "owed to you" being right and being double.
    """
    members = split_members(defn)
    if not members:
        return {}

    amount = rec.effective(defn, occ).amount
    split: Any = (defn.shared or {}).get("split", EVEN)

    if isinstance(split, Mapping):
        return {person_id: float(split.get(person_id, 0.0) or 0.0) for person_id in members}

    each = amount / (len(members) + 1)
    return {person_id: each for person_id in members}


def user_share(defn: Item, occ: date) -> float:
    """The part of an occurrence the user carries themselves."""
    amount = rec.effective(defn, occ).amount
    return amount - sum(member_shares(defn, occ).values())


def settled_members(defn: Item, occ: date) -> list[str]:
    return list(defn.shares_paid.get(occ) or [])


def unpaid_members(defn: Item, occ: date) -> list[str]:
    settled = set(settled_members(defn, occ))
    return [person_id for person_id in split_members(defn) if person_id not in settled]


def all_paid(defn: Item, occ: date) -> bool:
    """Whether everyone has settled *this* occurrence (§16)."""
    members = split_members(defn)
    if not members:
        return True
    return not unpaid_members(defn, occ)


def occurrence_outstanding(defn: Item, occ: date) -> float:
    """What is still owed on one occurrence by the members who have not settled."""
    shares = member_shares(defn, occ)
    return sum(shares.get(person_id, 0.0) for person_id in unpaid_members(defn, occ))


@dataclass(frozen=True)
class ShareRow:
    """One occurrence's split state, for the subscription list widget (§29)."""

    occurrence: rec.Occurrence
    shares: dict[str, float]
    unpaid: list[str]
    outstanding: float

    @property
    def settled(self) -> bool:
        return not self.unpaid


def share_rows(view: StoreView, start: date, end: date, *, include_paid: bool = True) -> list[ShareRow]:
    """Per-occurrence split state for every shared expense in a range."""
    rows: list[ShareRow] = []
    for defn in view.items:
        if defn.type != EXPENSE or not is_shared(defn):
            continue
        for occ_date in rec.occurrence_dates(defn, start, end):
            unpaid = unpaid_members(defn, occ_date)
            if not include_paid and not unpaid:
                continue
            rows.append(ShareRow(
                occurrence=rec.effective(defn, occ_date),
                shares=member_shares(defn, occ_date),
                unpaid=unpaid,
                outstanding=occurrence_outstanding(defn, occ_date),
            ))
    rows.sort(key=lambda row: rec.sort_key(row.occurrence))
    return rows


def default_span(view: StoreView, today: date, *, ahead_days: int = 0) -> tuple[date, date]:
    """What "everything billed so far" means: the shared items' own history, up to today.

    Anchored on the items rather than a fixed 12 months, so a subscription started three
    years ago is still fully accounted for.
    """
    starts = [
        defn.start for defn in view.items
        if defn.type == EXPENSE and is_shared(defn) and defn.start is not None
    ]
    earliest = min(starts) if starts else today
    return earliest, today + timedelta(days=ahead_days)


# ------------------------------------------------------------------------ rollups


@dataclass(frozen=True)
class PersonBalance:
    person: Person
    owed: float = 0.0
    settled: float = 0.0
    occurrences: int = 0
    oldest: date | None = None

    @property
    def outstanding(self) -> float:
        return self.owed - self.settled

    @property
    def settled_ratio(self) -> float | None:
        if self.owed <= 0:
            return None
        return self.settled / self.owed


def people_roster(
    view: StoreView,
    span: tuple[date, date],
) -> list[PersonBalance]:
    """Per person: what they owe, what they have settled, and what is left.

    Ordered by outstanding, largest first — the unsettled money is what the widget is
    for, so it leads.
    """
    owed: dict[str, float] = {}
    settled: dict[str, float] = {}
    counts: dict[str, int] = {}
    oldest: dict[str, date] = {}

    for defn in view.items:
        if defn.type != EXPENSE or not is_shared(defn):
            continue
        for occ_date in rec.occurrence_dates(defn, *span):
            shares = member_shares(defn, occ_date)
            paid = set(settled_members(defn, occ_date))
            for person_id, amount in shares.items():
                owed[person_id] = owed.get(person_id, 0.0) + amount
                counts[person_id] = counts.get(person_id, 0) + 1
                if person_id in paid:
                    settled[person_id] = settled.get(person_id, 0.0) + amount
                if person_id not in oldest or occ_date < oldest[person_id]:
                    oldest[person_id] = occ_date

    balances = [
        PersonBalance(
            person=person,
            owed=owed.get(person.id, 0.0),
            settled=settled.get(person.id, 0.0),
            occurrences=counts.get(person.id, 0),
            oldest=oldest.get(person.id),
        )
        for person in view.people
        if person.id in owed
    ]
    balances.sort(key=lambda b: (-b.outstanding, b.person.name.casefold()))
    return balances


def who_owes(view: StoreView, span: tuple[date, date]) -> dict[str, float]:
    """Outstanding amount per person — the "owed to you" rollup (§29)."""
    return {
        balance.person.id: balance.outstanding
        for balance in people_roster(view, span)
        if balance.outstanding
    }


def owed_total(view: StoreView, span: tuple[date, date]) -> float:
    return sum(who_owes(view, span).values())


@dataclass(frozen=True)
class OwedSummary:
    total: float = 0.0
    people: list[PersonBalance] = field(default_factory=list)
    count: int = 0

    @property
    def is_empty(self) -> bool:
        return self.count == 0


def owed_summary(view: StoreView, span: tuple[date, date], *, top: int = 3) -> OwedSummary:
    balances = [b for b in people_roster(view, span) if b.outstanding > 0]
    return OwedSummary(
        total=sum(b.outstanding for b in balances),
        people=balances[:top] if top else balances,
        count=len(balances),
    )


def settle_up(view: StoreView, span: tuple[date, date]) -> dict[str, float]:
    """Each person's outstanding balance — what a settle-up action would clear."""
    return who_owes(view, span)


def all_settled(view: StoreView, span: tuple[date, date]) -> bool:
    return not who_owes(view, span)


def share_of_total(defn: Item, occ: date, person_id: str) -> float:
    """One person's share as a fraction of the occurrence, guarded against zero."""
    amount = rec.effective(defn, occ).amount
    if amount <= 0:
        return 0.0
    return member_shares(defn, occ).get(person_id, 0.0) / amount


def even_share(amount: float, members: Sequence[str]) -> float:
    """A member's even cut of an amount, including the user's own share."""
    if not members:
        return 0.0
    return amount / (len(members) + 1)


def splits_are_exact(defn: Item, occ: date, tolerance: float = 0.011) -> bool:
    """Whether the members' shares fit inside the occurrence.

    An even split always does by construction. An explicit one can be edited to anything
    — including amounts that add up to more than the bill, which would have the app
    claiming a person owes money nobody was charged. This is what the UI checks before
    showing a settle-up figure it cannot justify.
    """
    amount = rec.effective(defn, occ).amount
    return sum(member_shares(defn, occ).values()) <= amount + tolerance
