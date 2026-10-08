"""History, summaries, projection and net worth (§14).

Two rules run through all of it.

* **The plan and the actual are computed for the same span, always.** Every structure
  here carries both, so nothing can quietly compare this month's plan to last month's
  bank statement.
* **A trend with one point has no trend.** Series are returned as they are, however
  short; suppressing a one-point sparkline is the widget's job (§32), not the engine's,
  because the *number* is still true.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from finance_tool.engine import periods as per
from finance_tool.engine import recurrence as rec
from finance_tool.engine.ledger import (
    EMPTY,
    Totals,
    UNCATEGORISED,
    actual_totals,
    leaf_items,
    plan_totals,
)
from finance_tool.store.entities import EXPENSE, INCOME, Snapshot
from finance_tool.store.store import StoreView

DateRange = tuple[date, date]

# Account kinds the app treats as spendable now. Anything else still counts towards net
# worth — a credit account carries a negative balance, which is the honest way to hold it.
LIQUID_KINDS = frozenset({"transaction", "savings", "cash", "checking", "everyday"})


# ---------------------------------------------------------------------- history


@dataclass(frozen=True)
class HistoryPoint:
    """One period of history: what was planned, what happened, and what it was worth."""

    date: date
    label: str
    span: DateRange
    plan: Totals = EMPTY
    actual: Totals = EMPTY
    net_worth: float | None = None

    @property
    def plan_net(self) -> float:
        return self.plan.net

    @property
    def actual_net(self) -> float:
        return self.actual.net

    @property
    def deviation(self) -> float:
        """Actual net minus planned net — how wrong the plan was about this month."""
        return self.actual_net - self.plan_net

    @property
    def has_actual(self) -> bool:
        return self.actual.count > 0


def history(
    view: StoreView,
    start: date,
    end: date,
    *,
    lens: str = per.MONTH,
    net_worth_points: Sequence[Snapshot] | None = None,
) -> list[HistoryPoint]:
    """One point per period of ``lens`` across ``[start, end]``."""
    snapshots = list(view.snapshots if net_worth_points is None else net_worth_points)
    points: list[HistoryPoint] = []
    for span in per.sub_ranges(lens, start, end):
        points.append(HistoryPoint(
            date=span[0],
            label=per.label_for(lens, span),
            span=span,
            plan=plan_totals(view, span),
            actual=actual_totals(view, span),
            net_worth=_net_worth_on(snapshots, span[1]),
        ))
    return points


def _net_worth_on(snapshots: Sequence[Snapshot], when: date) -> float | None:
    eligible = [s for s in snapshots if s.date <= when]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.date).value


# ------------------------------------------------------------------ record months


@dataclass(frozen=True)
class Records:
    """§29's record months.

    The engine reports the best and worst periods and whether they were *positive*; it
    does not rank them for colour. §29 is explicit that colour follows the sign, never
    the rank, or a bad month would be painted as an achievement for being first.
    """

    months: int = 0
    best: HistoryPoint | None = None
    worst: HistoryPoint | None = None
    months_positive: int = 0
    income_first: float = 0.0
    income_last: float = 0.0

    @property
    def all_positive(self) -> bool:
        return self.months > 0 and self.months_positive == self.months

    @property
    def income_growth(self) -> float | None:
        """Change in income from the first to the last point, or ``None`` for one point."""
        if self.months < 2:
            return None
        return self.income_last - self.income_first

    @property
    def income_growth_ratio(self) -> float | None:
        if self.months < 2 or self.income_first <= 0:
            return None
        return self.income_last / self.income_first


def records(points: Sequence[HistoryPoint]) -> Records:
    if not points:
        return Records()
    return Records(
        months=len(points),
        best=max(points, key=lambda p: p.plan_net),
        worst=min(points, key=lambda p: p.plan_net),
        months_positive=sum(1 for p in points if p.plan_net > 0),
        income_first=points[0].plan.income,
        income_last=points[-1].plan.income,
    )


@dataclass(frozen=True)
class SavingsRecord:
    """Total saved, average, best month and months tracked (§29)."""

    total: float = 0.0
    months: int = 0
    average: float = 0.0
    best: float = 0.0
    best_label: str = ""
    months_positive: int = 0

    @property
    def positive_ratio(self) -> float | None:
        if self.months <= 0:
            return None
        return self.months_positive / self.months


def savings_record(points: Sequence[HistoryPoint]) -> SavingsRecord:
    if not points:
        return SavingsRecord()
    nets = [(p.plan_net, p.label) for p in points]
    best_net, best_label = max(nets, key=lambda pair: pair[0])
    total = sum(net for net, _ in nets)
    return SavingsRecord(
        total=total,
        months=len(points),
        average=total / len(points),
        best=best_net,
        best_label=best_label,
        months_positive=sum(1 for net, _ in nets if net > 0),
    )


# ------------------------------------------------------------------------ metrics


@dataclass(frozen=True)
class Metrics:
    """The metric strip's numbers: averages over a trailing window, plus the points."""

    months: int = 0
    avg_income: float = 0.0
    avg_expenses: float = 0.0
    avg_net: float = 0.0
    points: list[HistoryPoint] = field(default_factory=list)

    @property
    def avg_savings_rate(self) -> float | None:
        if self.avg_income <= 0:
            return None
        return self.avg_net / self.avg_income

    def series(self, key: str) -> list[tuple[date, float]]:
        """A sparkline series for ``income``, ``expenses`` or ``net`` — plan side."""
        return [(p.date, _pick(p.plan, key)) for p in self.points]

    def actual_series(self, key: str) -> list[tuple[date, float]]:
        return [(p.date, _pick(p.actual, key)) for p in self.points]


def _pick(totals: Totals, key: str) -> float:
    return {"income": totals.income, "expenses": totals.expenses, "net": totals.net}.get(key, 0.0)


def metrics(
    view: StoreView,
    span: DateRange,
    *,
    months: int = 12,
    lens: str = per.MONTH,
) -> Metrics:
    """Averages over the ``months`` periods ending with the one that contains ``span``.

    A trailing window rather than the current period, because "average income" over one
    month is not an average.
    """
    window_end = span[1]
    window_start = per.add_months(per.month_start(window_end), -(max(1, months) - 1))
    points = history(view, window_start, window_end, lens=lens)
    count = len(points)
    if count == 0:
        return Metrics()
    return Metrics(
        months=count,
        avg_income=sum(p.plan.income for p in points) / count,
        avg_expenses=sum(p.plan.expenses for p in points) / count,
        avg_net=sum(p.plan_net for p in points) / count,
        points=points,
    )


# ----------------------------------------------------------------- period summary


@dataclass(frozen=True)
class PeriodSummary:
    """Everything the glance widgets need about one span, already paired up."""

    span: DateRange
    label: str
    lens: str
    plan: Totals = EMPTY
    actual: Totals = EMPTY
    prev_plan: Totals = EMPTY
    prev_actual: Totals = EMPTY
    series: list[tuple[date, float]] = field(default_factory=list)
    window: Metrics | None = None

    @property
    def plan_delta(self) -> Totals:
        return self.plan.delta_from(self.prev_plan)

    @property
    def actual_delta(self) -> Totals:
        return self.actual.delta_from(self.prev_actual)

    @property
    def net(self) -> float:
        return self.plan.net

    @property
    def actual_net(self) -> float:
        return self.actual.net

    @property
    def savings_rate(self) -> float | None:
        return self.plan.savings_rate

    @property
    def has_actual(self) -> bool:
        return self.actual.count > 0


def period_summary(
    view: StoreView,
    span: DateRange,
    *,
    lens: str = per.MONTH,
    window_months: int = 12,
) -> PeriodSummary:
    previous = per.previous_range(lens, span)
    window = metrics(view, span, months=window_months)
    return PeriodSummary(
        span=span,
        label=per.label_for(lens, span),
        lens=per.normalise_lens(lens),
        plan=plan_totals(view, span),
        actual=actual_totals(view, span),
        prev_plan=plan_totals(view, previous),
        prev_actual=actual_totals(view, previous),
        # The hero's sparkline: five *months*, whatever the lens, because §29 asks for a
        # 5-month sparkline beside the period figure. Five points is a trend and one is a
        # fact; the widget decides which it has (§32).
        series=[(p.date, p.plan_net) for p in window.points[-5:]],
        window=window,
    )


# ------------------------------------------------------------------------ forecast


@dataclass(frozen=True)
class ForecastPoint:
    span: DateRange
    label: str
    balance: float
    plan_net: float
    spread: float = 0.0

    @property
    def date(self) -> date:
        return self.span[1]

    @property
    def low(self) -> float:
        return self.balance - self.spread

    @property
    def high(self) -> float:
        return self.balance + self.spread


@dataclass(frozen=True)
class Forecast:
    """A point projection of the liquid balance (§29's liquid-balance forecast)."""

    opening: float = 0.0
    points: list[ForecastPoint] = field(default_factory=list)
    sigma: float = 0.0

    @property
    def months(self) -> int:
        return len(self.points)

    @property
    def final(self) -> float:
        return self.points[-1].balance if self.points else self.opening

    @property
    def final_low(self) -> float:
        return self.points[-1].low if self.points else self.opening

    @property
    def final_high(self) -> float:
        return self.points[-1].high if self.points else self.opening

    @property
    def is_flat(self) -> bool:
        return not self.points

    @property
    def first_negative(self) -> date | None:
        for point in self.points:
            if point.balance < 0:
                return point.date
        return None

    @property
    def is_banded(self) -> bool:
        return self.sigma > 0


def forecast(
    view: StoreView,
    span: DateRange,
    *,
    lens: str = per.MONTH,
    history_months: int = 12,
) -> Forecast:
    """The liquid balance rolled forward by the plan's net for each period.

    Straight-line on the plan, deliberately: this is what the *plan* implies, and mixing
    reconciled actuals into a forward projection would make the number neither.
    """
    return _project(view, span, lens=lens, history_months=history_months, banded=False)


def forecast_band(
    view: StoreView,
    span: DateRange,
    *,
    lens: str = per.MONTH,
    history_months: int = 12,
    confidence: float = 1.0,
) -> Forecast:
    """The same projection with a band that widens the further out it goes.

    The half-width is ``sigma * sqrt(periods_ahead)``, where ``sigma`` is the observed
    month-to-month gap between what was planned and what actually happened. That is the
    honest uncertainty: not "the future is random" but "this plan has been wrong by about
    this much, and being wrong compounds".

    With fewer than two months of actuals there is nothing to measure, so the fallback is
    15% of the planned monthly spend — a wide band, because a guess should look like one.
    """
    return _project(view, span, lens=lens, history_months=history_months,
                    banded=True, confidence=confidence)


def _project(
    view: StoreView,
    span: DateRange,
    *,
    lens: str,
    history_months: int,
    banded: bool,
    confidence: float = 1.0,
) -> Forecast:
    opening = liquid_balance(view)
    points: list[ForecastPoint] = []
    sigma = _sigma(view, span, lens=lens, history_months=history_months) if banded else 0.0
    balance = opening

    for index, sub in enumerate(per.sub_ranges(lens, span[0], span[1]), start=1):
        net = plan_totals(view, sub).net
        balance += net
        points.append(ForecastPoint(
            span=sub,
            label=per.label_for(lens, sub),
            balance=balance,
            plan_net=net,
            spread=sigma * math.sqrt(index) * confidence,
        ))
    return Forecast(opening=opening, points=points, sigma=sigma)


def _sigma(view: StoreView, span: DateRange, *, lens: str, history_months: int) -> float:
    window_end = per.month_start(span[1])
    window_start = per.add_months(window_end, -max(2, history_months))
    deviations: list[float] = []
    for point in history(view, window_start, span[1], lens=lens):
        if point.has_actual:
            deviations.append(point.deviation)

    if len(deviations) >= 2:
        mean = sum(deviations) / len(deviations)
        variance = sum((d - mean) ** 2 for d in deviations) / (len(deviations) - 1)
        return math.sqrt(variance)

    monthly_spend = sum(rec.monthly_equiv(i) for i in leaf_items(view.items)
                        if i.type == EXPENSE)
    return 0.15 * abs(monthly_spend)


# --------------------------------------------------------------------- net worth


def net_worth(view: StoreView) -> float:
    """Every account in the app's currency. A debt is an account with a negative balance."""
    return sum(a.balance for a in view.accounts if _matches_currency(view, a))


def liquid_balance(view: StoreView) -> float:
    """What is spendable now: matching-currency accounts of a liquid kind."""
    return sum(
        a.balance for a in view.accounts
        if _matches_currency(view, a) and (a.kind or "").lower() in LIQUID_KINDS
    )


def _matches_currency(view: StoreView, account) -> bool:
    """§22: a non-matching account is flagged, never summed."""
    return not account.currency or account.currency == view.currency


@dataclass(frozen=True)
class NetWorthSeries:
    points: list[tuple[date, float]] = field(default_factory=list)
    current: float = 0.0
    change: float | None = None
    foreign: list[str] = field(default_factory=list)

    @property
    def has_series(self) -> bool:
        """False when there is a single reading — a number, not yet a trend (§32)."""
        return len(self.points) >= 2

    @property
    def span(self) -> DateRange | None:
        if len(self.points) < 2:
            return None
        return self.points[0][0], self.points[-1][0]


def net_worth_series(
    view: StoreView,
    start: date | None = None,
    end: date | None = None,
    *,
    today: date | None = None,
) -> NetWorthSeries:
    """Recorded snapshots plus the live reading, ordered by date.

    Whether snapshots are captured automatically is an open decision (§38); this reports
    what exists and says how many readings there are, rather than inventing a curve.
    """
    readings = [(s.date, s.value) for s in view.snapshots]
    if today is not None:
        readings = [r for r in readings if r[0] <= today]
        readings.append((today, net_worth(view)))

    if start is not None:
        readings = [r for r in readings if r[0] >= start]
    if end is not None:
        readings = [r for r in readings if r[0] <= end]

    readings.sort(key=lambda r: r[0])
    current = net_worth(view)
    change = readings[-1][1] - readings[0][1] if len(readings) >= 2 else None

    return NetWorthSeries(
        points=readings,
        current=current,
        change=change,
        foreign=[a.name for a in view.foreign_accounts()],
    )


# ------------------------------------------------------------------ cashflow links


@dataclass(frozen=True)
class CashflowLink:
    source: str
    target: str
    amount: float


@dataclass(frozen=True)
class Cashflow:
    """Income sources and the categories they fund.

    Income arrives as one pool per source, so a source-to-category link cannot be
    observed — only allocated. The convention here is **proportional**: a source funds
    each target in proportion to that target's size, so every source has the same
    composition as the whole. It is a stated convention rather than a discovered fact,
    and the widget says so.

    The flow **balances**, which is what makes it drawable at all. What income does not
    spend is unallocated, so a surplus appears as an ``Unallocated`` target; a shortfall
    appears as a ``Deficit`` source. With that in place, each source's links sum to its
    income and each target's links sum to its amount — both, not one or the other.

    ``Unallocated`` rather than ``Savings`` on purpose: a user's own category may well be
    called Savings (the fixture's transfer is), and merging the two would silently fold
    the plan's savings line into every dollar the plan has not yet assigned anywhere.
    """

    sources: list[tuple[str, float]] = field(default_factory=list)
    targets: list[tuple[str, float]] = field(default_factory=list)
    links: list[CashflowLink] = field(default_factory=list)
    surplus: float = 0.0

    @property
    def total_in(self) -> float:
        return sum(amount for _, amount in self.sources)

    @property
    def total_out(self) -> float:
        return sum(amount for _, amount in self.targets)

    @property
    def is_empty(self) -> bool:
        return not self.links

    def links_from(self, source: str) -> list[CashflowLink]:
        return [link for link in self.links if link.source == source]

    def links_to(self, target: str) -> list[CashflowLink]:
        return [link for link in self.links if link.target == target]


UNALLOCATED = "Unallocated"
DEFICIT = "Deficit"


def cashflow_links(view: StoreView, span: DateRange, *, basis: str = "plan") -> Cashflow:
    """The flow from income to spending categories.

    ``basis="plan"`` reads the recurring items; ``basis="actual"`` reads the reconciled
    transactions. Neither is the "real" one — §1's whole premise is that they are peers.
    """
    if basis == "actual":
        sources, targets = _actual_sides(view, span)
    else:
        sources, targets = _plan_sides(view, span)

    total_in = sum(amount for _, amount in sources)
    total_out = sum(amount for _, amount in targets)
    surplus = total_in - total_out

    # Added to a bucket of that name rather than replacing it: a user is allowed to have a
    # category called "Unallocated", and silently absorbing their figure would be worse
    # than an odd-looking total.
    if surplus > 0:
        merged = dict(targets)
        merged[UNALLOCATED] = merged.get(UNALLOCATED, 0.0) + surplus
        targets = _sorted_pairs(merged)
    elif surplus < 0:
        merged = dict(sources)
        merged[DEFICIT] = merged.get(DEFICIT, 0.0) + (-surplus)
        sources = _sorted_pairs(merged)

    pool = max(sum(a for _, a in sources), sum(a for _, a in targets))
    links: list[CashflowLink] = []
    if pool > 0:
        for source, income in sources:
            for target, spend in targets:
                links.append(CashflowLink(source, target, income * (spend / pool)))
        links.sort(key=lambda link: (-link.amount, link.source.casefold(), link.target.casefold()))
    return Cashflow(sources=sources, targets=targets, links=links, surplus=surplus)


def _plan_sides(view: StoreView, span: DateRange) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    income: dict[str, float] = {}
    spending: dict[str, float] = {}
    for item in leaf_items(view.items):
        amount = sum(occ.amount for occ in rec.occurrences(item, *span))
        if not amount:
            continue
        if item.type == INCOME:
            income[item.name] = income.get(item.name, 0.0) + amount
        else:
            key = item.category or UNCATEGORISED
            spending[key] = spending.get(key, 0.0) + amount
    return (_sorted_pairs(income), _sorted_pairs(spending))


def _actual_sides(view: StoreView, span: DateRange) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    inside = [t for t in view.transactions if t.date is not None and span[0] <= t.date <= span[1]]
    income: dict[str, float] = {}
    spending: dict[str, float] = {}
    for txn in inside:
        key = txn.category or (UNCATEGORISED if txn.amount < 0 else "Income")
        if txn.amount >= 0:
            income[key] = income.get(key, 0.0) + txn.amount
        else:
            spending[key] = spending.get(key, 0.0) + abs(txn.amount)
    return (_sorted_pairs(income), _sorted_pairs(spending))


def _sorted_pairs(bucket: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(bucket.items(), key=lambda pair: (-pair[1], pair[0].casefold()))


# --------------------------------------------------------------------------- spend


def spend_by_category(
    view: StoreView,
    span: DateRange,
    *,
    basis: str = "actual",
    limit: int = 0,
) -> list[tuple[str, float]]:
    """Spending composition for the stacked bar and the donut."""
    _, targets = _actual_sides(view, span) if basis == "actual" else _plan_sides(view, span)
    return targets[:limit] if limit else targets


def spend_by_tag(view: StoreView, span: DateRange, *, limit: int = 0) -> list[tuple[str, float]]:
    """Spend grouped by transaction tag (§29). An untagged transaction is omitted."""
    bucket: dict[str, float] = {}
    for txn in view.transactions:
        if txn.date is None or not (span[0] <= txn.date <= span[1]) or txn.amount >= 0:
            continue
        for tag in txn.tags:
            bucket[tag] = bucket.get(tag, 0.0) + abs(txn.amount) / len(txn.tags)
    pairs = _sorted_pairs(bucket)
    return pairs[:limit] if limit else pairs


def daily_spend(view: StoreView, span: DateRange) -> list[tuple[date, float]]:
    """Day-resolution spend, with every day in the span present and zero-filled.

    Zero-filled on purpose: a calendar or a line chart with missing days lies about the
    shape of the month.
    """
    totals: dict[date, float] = {day: 0.0 for day in per.each_day(*span)}
    for txn in view.transactions:
        if txn.date is None or not (span[0] <= txn.date <= span[1]) or txn.amount >= 0:
            continue
        totals[txn.date] = totals.get(txn.date, 0.0) + abs(txn.amount)
    return sorted(totals.items())


def month_over_month(
    view: StoreView,
    span: DateRange,
    *,
    lens: str = per.MONTH,
    periods: int = 6,
) -> list[HistoryPoint]:
    """The last ``periods`` periods ending with ``span``, for the month-by-month table."""
    start = span[1]
    for _ in range(max(1, periods) - 1):
        start = per.previous_range(lens, (start, start))[0]
    first = per.range_for_lens(lens, start)[0]
    return history(view, first, span[1], lens=lens)
