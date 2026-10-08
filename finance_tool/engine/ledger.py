"""Totals, summaries and budgets (§14).

Everything here is a fold over :func:`~finance_tool.engine.recurrence.occurrences`, so
the ledger, the hero figure and the charts are reading the same generator of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Sequence

from finance_tool.engine import periods as per
from finance_tool.engine import recurrence as rec
from finance_tool.store.entities import EXPENSE, INCOME, Item, Transaction
from finance_tool.store.store import StoreView

UNCATEGORISED = "Uncategorised"

DateRange = tuple[date, date]


# ------------------------------------------------------------------------- leaves


def is_leaf(items: Sequence[Item], item: Item) -> bool:
    """A node with children is a group, not something that fires on a date.

    Without this, a category node like "Utilities" would contribute one zero-amount
    occurrence on its own start date and quietly inflate every count in the app.
    """
    return not any(other.parent_id == item.id for other in items)


def leaf_items(items: Sequence[Item]) -> list[Item]:
    return [item for item in items if is_leaf(items, item)]


def subtree(items: Sequence[Item], root_id: str) -> list[Item]:
    """``root_id`` and everything beneath it, at any depth."""
    by_parent: dict[str | None, list[Item]] = {}
    for item in items:
        by_parent.setdefault(item.parent_id, []).append(item)

    found: list[Item] = []
    pending = [root_id]
    while pending:
        current = pending.pop()
        for child in by_parent.get(current, []):
            found.append(child)
            pending.append(child.id)
    return found


# ------------------------------------------------------------------------- totals


@dataclass(frozen=True)
class Totals:
    """Income and expenses as positive magnitudes; ``net`` carries the sign."""

    income: float = 0.0
    expenses: float = 0.0
    count: int = 0
    income_count: int = 0
    expense_count: int = 0

    @property
    def net(self) -> float:
        return self.income - self.expenses

    def plus(self, other: "Totals") -> "Totals":
        return Totals(
            income=self.income + other.income,
            expenses=self.expenses + other.expenses,
            count=self.count + other.count,
            income_count=self.income_count + other.income_count,
            expense_count=self.expense_count + other.expense_count,
        )

    def delta_from(self, other: "Totals") -> "Totals":
        """This period minus ``other`` — the hero widget's period-on-period change."""
        return Totals(
            income=self.income - other.income,
            expenses=self.expenses - other.expenses,
            count=self.count - other.count,
            income_count=self.income_count - other.income_count,
            expense_count=self.expense_count - other.expense_count,
        )

    @property
    def savings_rate(self) -> float | None:
        """Net as a share of income. ``None`` when there is no income to divide by."""
        if self.income <= 0:
            return None
        return self.net / self.income


EMPTY = Totals()


def totals_for_occurrences(occurrences: Iterable[rec.Occurrence]) -> Totals:
    income = expenses = 0.0
    income_count = expense_count = 0
    for occ in occurrences:
        if occ.type == INCOME:
            income += occ.amount
            income_count += 1
        else:
            expenses += occ.amount
            expense_count += 1
    return Totals(income=income, expenses=expenses,
                  count=income_count + expense_count,
                  income_count=income_count, expense_count=expense_count)


def totals_in_range(
    items: Sequence[Item],
    start: date,
    end: date,
    *,
    leaves_only: bool = True,
) -> Totals:
    """The plan's totals over a range, from occurrence generation rather than a stored
    row per period."""
    chosen = leaf_items(items) if leaves_only else list(items)
    found: list[rec.Occurrence] = []
    for item in chosen:
        found.extend(rec.occurrences(item, start, end))
    return totals_for_occurrences(found)


def plan_totals(view: StoreView, span: DateRange, *, items: Sequence[Item] | None = None) -> Totals:
    return totals_in_range(list(items if items is not None else view.items), *span)


def totals_for_transactions(
    transactions: Sequence[Transaction],
    start: date,
    end: date,
) -> Totals:
    """Reconciled totals. A transaction carries its own sign, so a positive is income."""
    income = expenses = 0.0
    income_count = expense_count = 0
    for txn in transactions:
        if txn.date is None or not (start <= txn.date <= end):
            continue
        if txn.amount >= 0:
            income += txn.amount
            income_count += 1
        else:
            expenses += abs(txn.amount)
            expense_count += 1
    return Totals(income=income, expenses=expenses,
                  count=income_count + expense_count,
                  income_count=income_count, expense_count=expense_count)


def actual_totals(view: StoreView, span: DateRange) -> Totals:
    return totals_for_transactions(view.transactions, *span)


# ------------------------------------------------------------------ ledger rows


def ledger_rows(
    view: StoreView,
    span: DateRange,
    *,
    kind: str = EXPENSE,
    include_cancelled: bool = False,
) -> list[rec.Occurrence]:
    """One row per occurrence of ``kind`` in the period, in the total order of §11."""
    found: list[rec.Occurrence] = []
    for item in leaf_items(view.items):
        if item.type != kind:
            continue
        found.extend(rec.occurrences(item, *span, include_cancelled=include_cancelled))
    found.sort(key=rec.sort_key)
    return found


@dataclass(frozen=True)
class LedgerGroup:
    """A category node and the occurrences under it, for the two-level ledger render."""

    item: Item | None
    occurrences: list[rec.Occurrence]
    totals: Totals

    @property
    def name(self) -> str:
        return self.item.name if self.item is not None else ""


def ledger_groups(
    view: StoreView,
    span: DateRange,
    *,
    kind: str = EXPENSE,
    include_cancelled: bool = False,
) -> list[LedgerGroup]:
    """Ledger rows nested one level: children under their parent, loose items together."""
    rows = ledger_rows(view, span, kind=kind, include_cancelled=include_cancelled)
    buckets: dict[str | None, list[rec.Occurrence]] = {}
    for row in rows:
        buckets.setdefault(row.definition.parent_id, []).append(row)

    groups: list[LedgerGroup] = []
    for parent_id, occurrences in buckets.items():
        occurrences.sort(key=rec.sort_key)
        groups.append(LedgerGroup(
            item=view.item(parent_id) if parent_id else None,
            occurrences=occurrences,
            totals=totals_for_occurrences(occurrences),
        ))

    # A group with a node leads with it; loose items keep their own position by due date.
    groups.sort(key=lambda group: (
        0 if group.item is not None else 1,
        rec.sort_key(group.occurrences[0]),
    ))
    return groups


# ------------------------------------------------------------------ plan vs actual


@dataclass(frozen=True)
class PlanSummary:
    """The plan and the reconciled reality, side by side and equally weighted (§1)."""

    plan: Totals
    actual: Totals
    occurrences: int = 0
    settled: int = 0

    @property
    def settled_ratio(self) -> float | None:
        if self.occurrences <= 0:
            return None
        return self.settled / self.occurrences


def plan_summary(view: StoreView, span: DateRange) -> PlanSummary:
    """``occurrences`` counts scheduled rows *including* cancelled ones, so it matches
    the number of rows the ledger renders; ``plan`` excludes them, so the money is
    right. A cancelled occurrence is a row with no amount, not a missing row.
    """
    rows = ledger_rows(view, span, kind=EXPENSE, include_cancelled=True)
    rows += ledger_rows(view, span, kind=INCOME, include_cancelled=True)
    return PlanSummary(
        plan=plan_totals(view, span),
        actual=actual_totals(view, span),
        occurrences=len(rows),
        settled=sum(1 for row in rows if row.paid),
    )


# ----------------------------------------------------------------------- budgets


@dataclass(frozen=True)
class BudgetRow:
    """One category: what the plan expects, what the bank says, and whether it holds."""

    category: str
    planned: float
    actual: float
    pace: float | None = None

    @property
    def variance(self) -> float:
        """Positive means under budget — the reassuring direction for an expense."""
        return self.planned - self.actual

    @property
    def on_track(self) -> bool:
        return self.actual <= self.planned

    @property
    def progress(self) -> float:
        """Actual as a share of planned, guarded against a zero denominator."""
        if self.planned <= 0:
            return 0.0 if self.actual <= 0 else 1.0
        return self.actual / self.planned

    @property
    def pace_ratio(self) -> float | None:
        """Actual against where the plan *should* be by today, for the pace widget."""
        if self.pace is None or self.pace <= 0:
            return None
        return self.actual / self.pace

    @property
    def over_pace(self) -> bool:
        ratio = self.pace_ratio
        return ratio is not None and ratio > 1.0


def _expected_to_date(planned: float, span: DateRange, today: date | None) -> float | None:
    if today is None or today > span[1]:
        return None
    elapsed = per.day_count_elapsed(span, today)
    total = per.days_in(span)
    return planned * (elapsed / total) if total else None


def budget_vs_actual(
    view: StoreView,
    span: DateRange,
    *,
    today: date | None = None,
) -> list[BudgetRow]:
    """Expenses by category: the plan's occurrences against the reconciled transactions.

    Only expenses: a budget is a control on spending, and rolling income into the same
    bullet bar would make every category read as an overspend. Income is compared in
    :func:`plan_summary` instead.

    Uncategorised spend gets a row of its own rather than being dropped — a budget view
    that quietly omits a third of real spend cannot answer "am I on track" (§23, §24).
    """
    planned: dict[str, float] = {}
    for item in leaf_items(view.items):
        if item.type != EXPENSE or not item.category:
            # A budget is a *named* category. An expense the plan has not categorised is
            # not a budget line, and bucketing it under "Uncategorised" would invent a
            # target nobody set — and would mean a store with no categories could never
            # reach the empty state §29 asks the widget to show.
            continue
        amount = sum(occ.amount for occ in rec.occurrences(item, *span))
        if amount:
            planned[item.category] = planned.get(item.category, 0.0) + amount

    actual: dict[str, float] = {}
    for txn in view.transactions:
        if txn.date is None or not (span[0] <= txn.date <= span[1]) or txn.amount >= 0:
            continue
        key = txn.category or UNCATEGORISED
        actual[key] = actual.get(key, 0.0) + abs(txn.amount)

    # Uncategorised *spend* keeps a row of its own: it has no plan to sit beside, and
    # dropping it would let a third of real spend leave a budget view unremarked (§23).
    rows = [
        BudgetRow(
            category=name,
            planned=planned.get(name, 0.0),
            actual=actual.get(name, 0.0),
            pace=_expected_to_date(planned.get(name, 0.0), span, today),
        )
        for name in sorted(set(planned) | set(actual))
    ]
    rows.sort(key=lambda row: (-row.planned, -row.actual, row.category.casefold()))
    return rows


@dataclass(frozen=True)
class Coverage:
    """§24's measured bar: how much of the reconciled spend is actually categorised."""

    categorised_count: int = 0
    total_count: int = 0
    categorised_value: float = 0.0
    total_value: float = 0.0

    @property
    def by_count(self) -> float:
        if self.total_count == 0:
            return 1.0
        return self.categorised_count / self.total_count

    @property
    def by_value(self) -> float:
        if self.total_value <= 0:
            return 1.0
        return self.categorised_value / self.total_value

    def meets(self, bar: float = 0.80) -> bool:
        return self.by_count >= bar and self.by_value >= bar


def budget_coverage(
    view: StoreView,
    span: DateRange | None = None,
    *,
    expenses_only: bool = True,
) -> Coverage:
    """Coverage by count *and* by dollar value, because a count-only bar can pass while
    the largest transactions sit in Uncategorised."""
    count = total = 0
    value = total_value = 0.0
    for txn in view.transactions:
        if txn.date is None:
            continue
        if span is not None and not (span[0] <= txn.date <= span[1]):
            continue
        if expenses_only and txn.amount >= 0:
            continue
        magnitude = abs(txn.amount)
        total += 1
        total_value += magnitude
        if txn.category:
            count += 1
            value += magnitude
    return Coverage(categorised_count=count, total_count=total,
                    categorised_value=value, total_value=total_value)


def uncategorised_transactions(
    view: StoreView,
    span: DateRange | None = None,
) -> list[Transaction]:
    found = [t for t in view.transactions if not t.category and t.date is not None]
    if span is not None:
        found = [t for t in found if span[0] <= t.date <= span[1]]
    found.sort(key=lambda t: (t.date, t.description.casefold()))
    return found


# --------------------------------------------------------------------------- bills


@dataclass(frozen=True)
class DueSummary:
    overdue_count: int = 0
    overdue_amount: float = 0.0
    due_soon_count: int = 0
    due_soon_amount: float = 0.0
    upcoming_count: int = 0
    upcoming_amount: float = 0.0

    @property
    def total_amount(self) -> float:
        return self.overdue_amount + self.due_soon_amount + self.upcoming_amount


def bills_in_range(
    view: StoreView,
    start: date,
    end: date,
    *,
    include_paid: bool = False,
) -> list[rec.Occurrence]:
    """Unpaid expense occurrences landing in a window, in total order (§11)."""
    found: list[rec.Occurrence] = []
    for item in leaf_items(view.items):
        if item.type != EXPENSE:
            continue
        for occ in rec.occurrences(item, start, end):
            if include_paid or not occ.paid:
                found.append(occ)
    found.sort(key=rec.sort_key)
    return found


def due_summary(
    bills: Sequence[rec.Occurrence],
    today: date,
    *,
    soon_days: int = 7,
) -> DueSummary:
    overdue_count = due_soon_count = upcoming_count = 0
    overdue = due_soon = upcoming = 0.0
    for occ in bills:
        if occ.is_overdue(today):
            overdue_count += 1
            overdue += occ.amount
        elif occ.is_due_soon(today, soon_days):
            due_soon_count += 1
            due_soon += occ.amount
        else:
            upcoming_count += 1
            upcoming += occ.amount
    return DueSummary(overdue_count, overdue, due_soon_count, due_soon,
                      upcoming_count, upcoming)


def overdue_bills(
    view: StoreView,
    today: date,
    *,
    lookback_days: int = 180,
) -> list[rec.Occurrence]:
    """Unpaid expense occurrences whose due date has already passed.

    Its own function rather than a wider :func:`bills_in_range` window, because an
    overdue bill is a different signal from an upcoming one and the widget pairs them
    rather than blending them.
    """
    window = bills_in_range(view, today - timedelta(days=lookback_days), today - timedelta(days=1))
    found = [occ for occ in window if occ.due < today]
    found.sort(key=rec.sort_key)
    return found


def due_soon_bills(
    view: StoreView,
    today: date,
    *,
    days: int = 7,
) -> list[rec.Occurrence]:
    """Unpaid expense occurrences due within ``days`` of today."""
    window = bills_in_range(view, today, today + timedelta(days=days))
    found = [occ for occ in window if today <= occ.due <= today + timedelta(days=days)]
    found.sort(key=rec.sort_key)
    return found


def next_due(view: StoreView, today: date) -> rec.Occurrence | None:
    """The soonest unpaid expense occurrence on or after today."""
    horizon = today + timedelta(days=366)
    bills = bills_in_range(view, today, horizon)
    return bills[0] if bills else None


# --------------------------------------------------------------------- subscriptions


@dataclass(frozen=True)
class SubscriptionTotals:
    count: int = 0
    monthly: float = 0.0
    yearly: float = 0.0
    items: list[Item] = field(default_factory=list)


def active_on(item: Item, when: date) -> bool:
    if item.start is not None and item.start > when:
        return False
    return item.end is None or item.end >= when


def subscription_totals(view: StoreView, *, when: date) -> SubscriptionTotals:
    """Subscription count and cost, from the items *active* on a date.

    Activity rather than existence: a subscription cancelled last year should not keep
    paying into this month's cost summary.
    """
    chosen = [
        item for item in leaf_items(view.items)
        if item.subscription and item.type == EXPENSE and active_on(item, when)
    ]
    monthly = sum(rec.monthly_equiv(item) for item in chosen)
    chosen.sort(key=lambda item: (-rec.monthly_equiv(item), item.name.casefold()))
    return SubscriptionTotals(count=len(chosen), monthly=monthly, yearly=monthly * 12,
                              items=chosen)


def monthly_plan_by_category(items: Sequence[Item]) -> dict[str, float]:
    """Each category's monthly equivalent — the steady-state cost of the plan."""
    planned: dict[str, float] = {}
    for item in leaf_items(items):
        if item.type != EXPENSE:
            continue
        key = item.category or UNCATEGORISED
        planned[key] = planned.get(key, 0.0) + rec.monthly_equiv(item)
    return planned


# ------------------------------------------------------------------ income projection


@dataclass(frozen=True)
class IncomeProjection:
    totals: Totals
    sources: list[rec.Occurrence]

    @property
    def is_empty(self) -> bool:
        return self.totals.count == 0


def predicted_income(view: StoreView, span: DateRange) -> IncomeProjection:
    """The next period's income and where it comes from (§29)."""
    rows = ledger_rows(view, span, kind=INCOME)
    return IncomeProjection(totals=totals_for_occurrences(rows), sources=rows)
