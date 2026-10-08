"""Savings goals and their projected completion (§14, §29).

Where the contribution comes from is the whole design question. A goal linked to
expenses is funded by *those* expenses — the "Savings transfer" line is the money — and
a goal with no links is funded by whatever surplus the plan leaves. Unlinked goals share
that surplus in proportion to what they still need, so two of them cannot both claim the
same dollar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from finance_tool.engine import recurrence as rec
from finance_tool.engine import periods as per
from finance_tool.store.entities import EXPENSE, INCOME, Goal, Item
from finance_tool.store.store import StoreView

# How far ahead the schedule walk looks before deciding a goal will not complete.
HORIZON_YEARS = 40


# ------------------------------------------------------------------- the surplus


def monthly_income(view: StoreView) -> float:
    return sum(rec.monthly_equiv(item) for item in view.items
               if item.type == INCOME and _is_leaf(view, item))


def monthly_expenses(view: StoreView) -> float:
    return sum(rec.monthly_equiv(item) for item in view.items
               if item.type == EXPENSE and _is_leaf(view, item))


def monthly_surplus(view: StoreView) -> float:
    """The plan's steady-state monthly net.

    One-offs contribute nothing here: a concert ticket is not a monthly cost, and
    averaging it in would understate what is genuinely available every month.
    """
    return monthly_income(view) - monthly_expenses(view)


def savings_rate(view: StoreView, span: tuple[date, date]) -> float | None:
    """Net as a share of income over a period. ``None`` when there is no income."""
    from finance_tool.engine.ledger import plan_totals

    return plan_totals(view, span).savings_rate


def _is_leaf(view: StoreView, item: Item) -> bool:
    return not any(other.parent_id == item.id for other in view.items)


def linked_monthly(view: StoreView, goal: Goal) -> float:
    """The monthly equivalent of the items a goal is linked to."""
    total = 0.0
    for item_id in goal.linked_def_ids:
        item = view.item(item_id)
        if item is not None:
            total += rec.monthly_equiv(item)
    return total


# ------------------------------------------------------------------- contributions


def contributions(view: StoreView) -> dict[str, float]:
    """Each goal's monthly contribution, with the unlinked ones sharing the surplus.

    The pool for unlinked goals is the whole monthly surplus, *not* the surplus minus the
    linked commitments. `monthly_surplus` is income minus every leaf expense, and a linked
    goal's savings transfer is one of those expenses — so it is already out of the figure.
    Subtracting it a second time made the linked amount vanish from the plan entirely.
    """
    goals = list(view.goals)
    if not goals:
        return {}

    taken: dict[str, float] = {}
    unlinked: list[Goal] = []
    for goal in goals:
        if goal.linked_def_ids:
            taken[goal.id] = linked_monthly(view, goal)
        else:
            unlinked.append(goal)

    if unlinked:
        pool = max(0.0, monthly_surplus(view))
        needs = {goal.id: max(0.0, goal.target - goal.saved) for goal in unlinked}
        total_need = sum(needs.values())
        for goal in unlinked:
            if total_need <= 0:
                taken[goal.id] = 0.0
            else:
                taken[goal.id] = pool * (needs[goal.id] / total_need)

    return taken


# ---------------------------------------------------------------------- projections


@dataclass(frozen=True)
class GoalProjection:
    goal_id: str
    name: str
    target: float
    saved: float
    monthly: float = 0.0
    months: int | None = None
    eta: date | None = None
    linked: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.target - self.saved)

    @property
    def complete(self) -> bool:
        return self.remaining <= 0

    @property
    def progress(self) -> float:
        """Saved as a share of target. A zero target is complete, not undefined (§15.4)."""
        if self.target <= 0:
            return 1.0
        return min(1.0, max(0.0, self.saved / self.target))

    @property
    def on_track(self) -> bool:
        return self.complete or self.eta is not None


def _scheduled_amounts(view: StoreView, goal: Goal, today: date) -> list[tuple[date, float]]:
    """Every remaining contribution date for a linked goal, with its amount."""
    dated: list[tuple[date, float]] = []
    for item_id in goal.linked_def_ids:
        item = view.item(item_id)
        if item is None:
            continue
        for occ in rec.occurrences(item, today, per.add_months(today, HORIZON_YEARS * 12)):
            dated.append((occ.date, occ.amount))
    dated.sort()
    return dated


def goal_eta(
    view: StoreView,
    goal: Goal,
    today: date,
    *,
    monthly: float | None = None,
) -> GoalProjection:
    """When a goal completes, given its contribution rate.

    ``monthly`` is resolved from :func:`contributions` when not supplied, so a goal asked
    about on its own and the same goal inside the full projection always agree.

    For a **linked** goal the schedule is the truth, and it is walked first: the goal
    completes on the date the money actually lands. That matters for the shapes a rate
    alone gets wrong — a goal funded by a single one-off payment has a monthly equivalent
    of zero but a real completion date, and a goal whose linked item ends before the
    target is reached has a rate that says "eleven more payments" and a schedule that
    says "never".
    """
    rate = contributions(view).get(goal.id, 0.0) if monthly is None else float(monthly)
    remaining = max(0.0, goal.target - goal.saved)
    linked = list(goal.linked_def_ids)
    make = lambda **kw: GoalProjection(  # noqa: E731 - one shape, one construction
        goal_id=goal.id, name=goal.name, target=goal.target, saved=goal.saved,
        monthly=rate, linked=linked, **kw)

    if remaining <= 0:
        return make(months=0, eta=today)

    if linked:
        accumulated = 0.0
        taken = 0
        for when, amount in _scheduled_amounts(view, goal, today + timedelta(days=1)):
            accumulated += amount
            taken += 1
            if accumulated >= remaining:
                return make(months=taken, eta=when)
        # The schedule ran out before the target: no date, and no pretending otherwise.
        return make(months=_months_needed(remaining, rate), eta=None)

    if rate <= 0:
        return make(months=None, eta=None)

    months = _months_needed(remaining, rate)
    return make(months=months, eta=per.add_months(today, months))


def _months_needed(remaining: float, rate: float) -> int | None:
    if rate <= 0:
        return None
    return int(math.ceil(remaining / rate))


def goal_projections(view: StoreView, today: date) -> list[GoalProjection]:
    """Every goal's projection, soonest completion first; unreachable ones last."""
    rates = contributions(view)
    found = [goal_eta(view, goal, today, monthly=rates.get(goal.id, 0.0)) for goal in view.goals]
    found.sort(key=lambda p: (
        p.eta is None or not p.on_track,
        p.eta or per.add_months(today, HORIZON_YEARS * 12),
        p.name.casefold(),
    ))
    return found


@dataclass(frozen=True)
class SavingsRecord:
    """§29's savings record: the arithmetic of the goal effort, not a pep talk."""

    total_saved: float = 0.0
    goals: int = 0
    complete: int = 0
    monthly_rate: float = 0.0

    @property
    def average_saved(self) -> float:
        if self.goals <= 0:
            return 0.0
        return self.total_saved / self.goals

    @property
    def completion_ratio(self) -> float | None:
        if self.goals <= 0:
            return None
        return self.complete / self.goals


def savings_record(view: StoreView, today: date) -> SavingsRecord:
    projections = goal_projections(view, today)
    return SavingsRecord(
        total_saved=sum(goal.saved for goal in view.goals),
        goals=len(view.goals),
        complete=sum(1 for p in projections if p.complete),
        monthly_rate=sum(p.monthly for p in projections),
    )


def goal_for_item(view: StoreView, item_id: str) -> list[Goal]:
    return [goal for goal in view.goals if item_id in goal.linked_def_ids]
