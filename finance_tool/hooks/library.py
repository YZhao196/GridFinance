"""The catalogue's data hooks (§29).

One function per widget, each a thin reading of the engine. That thinness is the point:
if a figure here needed arithmetic of its own, the arithmetic would belong in the engine
where it can be tested without a display (§6).

Hooks are registered by id and looked up in code at build time. There is no plugins
directory and no ``importlib`` (§28) — a figure the catalogue does not ship is a function
someone adds to this file, not a script loaded against a live ledger.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypeVar

from finance_tool.engine import analytics as an
from finance_tool.engine import categorise as cat
from finance_tool.engine import detect as det
from finance_tool.engine import goals as gl
from finance_tool.engine import ledger as led
from finance_tool.engine import periods as per
from finance_tool.engine.recurrence import monthly_equiv
from finance_tool.engine import shared as shr
from finance_tool.engine import tracker as trk
from finance_tool.hooks.types import Hook, HookContext, Row, WidgetData, widget_data

HOOKS: dict[str, Hook] = {}

H = TypeVar("H", bound=Hook)


def hook(hook_id: str) -> Any:
    def register(function: H) -> H:
        HOOKS[hook_id] = function
        return function

    return register


def get_hook(hook_id: str) -> Hook | None:
    return HOOKS.get(hook_id)


def hook_ids() -> list[str]:
    return sorted(HOOKS)


def _totals_summary(ctx: HookContext) -> an.PeriodSummary:
    return an.period_summary(ctx.store, ctx.span, lens=ctx.lens)


def _ledger_rows(ctx: HookContext, kind: str) -> list[Row]:
    rows: list[Row] = []
    for occ in led.ledger_rows(ctx.store, ctx.span, kind=kind, include_cancelled=True):
        rows.append(Row(
            label=occ.name,
            value=occ.signed_amount,
            detail=occ.due.isoformat(),
            sign=-1 if occ.cancelled else (0 if occ.paid else None),
            tags=occ.tags,
            reference={"kind": "item", "id": occ.item_id, "date": occ.date,
                       "paid": occ.paid, "cancelled": occ.cancelled,
                       "subscription": occ.subscription, "priority": occ.priority},
        ))
    return rows


# ----------------------------------------------------------------------- glance


@hook("hero_pl")
def hero_pl(ctx: HookContext, config: dict) -> WidgetData | None:
    """The loudest figure: the period's net, with the change that produced it."""
    summary = _totals_summary(ctx)
    if summary.plan.count == 0 and not summary.has_actual:
        return None
    return widget_data(
        summary.net,
        delta=summary.plan_delta.net,
        series=[(when, value) for when, value in summary.series] or None,
        note=summary.label,
        income=summary.plan.income,
        expenses=summary.plan.expenses,
        actual_net=summary.actual_net,
        savings_rate=summary.savings_rate,
    )


@hook("period_summary")
def period_summary_hook(ctx: HookContext, config: dict) -> WidgetData | None:
    summary = _totals_summary(ctx)
    if summary.plan.count == 0 and not summary.has_actual:
        return None
    return widget_data(
        summary.net,
        delta=summary.plan_delta.net,
        note=summary.label,
        income=summary.plan.income,
        expenses=summary.plan.expenses,
        actual_income=summary.actual.income,
        actual_expenses=summary.actual.expenses,
        actual_net=summary.actual_net,
        lens=summary.lens,
    )


@hook("incoming_ledger")
def incoming_ledger(ctx: HookContext, config: dict) -> WidgetData | None:
    """The period's income, with a row for each occurrence.

    The total comes from the engine rather than from the rows, because the rows include
    cancelled occurrences — struck through, so the user can see what they cancelled —
    and a cancelled occurrence "doesn't happen" (§9). Summing the rows would quietly add
    back money the plan says is not coming.
    """
    rows = _ledger_rows(ctx, "income")
    if not rows:
        return None
    total = led.plan_totals(ctx.store, ctx.span).income
    return widget_data(total, rows=rows, income=total)


@hook("outgoing_ledger")
def outgoing_ledger(ctx: HookContext, config: dict) -> WidgetData | None:
    rows = _ledger_rows(ctx, "expense")
    if not rows:
        return None
    total = led.plan_totals(ctx.store, ctx.span).expenses
    return widget_data(total, rows=rows, expenses=total)


@hook("weekly_pl")
def weekly_pl(ctx: HookContext, config: dict) -> WidgetData | None:
    weeks = per.week_ranges(*ctx.span)
    if len(weeks) < 2:
        return None
    bars: list[dict[str, Any]] = []
    nets: list[tuple[date, float]] = []
    for index, span in enumerate(weeks):
        totals = led.plan_totals(ctx.store, span)
        bars.append({"index": index, "label": per.label_for(per.WEEK, span),
                     "start": span[0], "income": totals.income, "expenses": totals.expenses,
                     "net": totals.net, "is_current": per.contains(span, ctx.today)})
        nets.append((span[0], totals.net))
    if not any(bar["income"] or bar["expenses"] for bar in bars):
        return None
    return widget_data(sum(b["net"] for b in bars), series=nets, weeks=bars)


@hook("due_soon")
def due_soon(ctx: HookContext, config: dict) -> WidgetData | None:
    soon_days = int(config.get("days", 7))
    overdue = led.overdue_bills(ctx.store, ctx.today, lookback_days=90)
    upcoming = led.due_soon_bills(ctx.store, ctx.today, days=soon_days)
    if not overdue and not upcoming:
        return None

    rows = [
        Row(label=occ.name, value=occ.signed_amount, detail=f"overdue · {occ.due:%d %b}",
            target={"kind": "item", "id": occ.item_id},
            reference={"kind": "item", "id": occ.item_id, "date": occ.date,
                       "overdue": True, "paid": occ.paid})
        for occ in overdue[:6]
    ]
    rows += [
        Row(label=occ.name, value=occ.signed_amount, detail=f"due {occ.due:%d %b}",
            target={"kind": "item", "id": occ.item_id},
            reference={"kind": "item", "id": occ.item_id, "date": occ.date,
                       "overdue": False, "paid": occ.paid})
        for occ in upcoming[:6]
    ]
    return widget_data(
        -sum(occ.amount for occ in overdue),
        rows=rows,
        note=f"{len(overdue)} overdue · {len(upcoming)} due in {soon_days} days",
        overdue_count=len(overdue),
        overdue_amount=sum(occ.amount for occ in overdue),
        due_soon_count=len(upcoming),
        due_soon_amount=sum(occ.amount for occ in upcoming),
        next_due=upcoming[0].date if upcoming else (overdue[0].date if overdue else None),
    )


@hook("savings_goals")
def savings_goals(ctx: HookContext, config: dict) -> WidgetData | None:
    projections = gl.goal_projections(ctx.store, ctx.today)
    if not projections:
        return None
    rows = [
        Row(label=p.name, value=p.saved,
            detail=_goal_detail(p),
            reference={"kind": "goal", "id": p.goal_id, "progress": p.progress,
                       "target": p.target, "monthly": p.monthly, "months": p.months,
                       "eta": p.eta, "complete": p.complete, "on_track": p.on_track})
        for p in projections
    ]
    total_target = sum(p.target for p in projections)
    return widget_data(
        sum(p.saved for p in projections),
        rows=rows,
        note=f"{sum(1 for p in projections if p.complete)} of {len(projections)} complete",
        progress=(sum(p.saved for p in projections) / total_target) if total_target else 0.0,
        target=total_target,
    )


def _goal_detail(projection) -> str:
    """ "1,500 of 6,000 · Oct 2027" — the amount first, because that is the figure."""
    saved = f"{projection.saved:,.0f} of {projection.target:,.0f}"
    if projection.complete:
        return f"{saved} · complete"
    if projection.eta is not None:
        return f"{saved} · {projection.eta:%b %Y}"
    if projection.months:
        return f"{saved} · {projection.months} months needed"
    return f"{saved} · not on track"


@hook("cost_summary")
def cost_summary(ctx: HookContext, config: dict) -> WidgetData | None:
    subs = led.subscription_totals(ctx.store, when=ctx.today)
    owed = shr.owed_summary(ctx.store, shr.default_span(ctx.store, ctx.today))
    if subs.count == 0 and owed.is_empty:
        return None
    return widget_data(
        subs.monthly,
        note=f"{subs.count} subscriptions",
        count=subs.count,
        yearly=subs.yearly,
        owed=owed.total,
        owed_people=owed.count,
        rows=[Row(label=item.name, value=-monthly_equiv(item), detail="monthly")
              for item in subs.items[:6]],
    )


@hook("spending_calendar")
def spending_calendar(ctx: HookContext, config: dict) -> WidgetData | None:
    """Spend and due dates by day, for the borderless calendar."""
    window = _calendar_span(ctx, config)
    days = dict(an.daily_spend(ctx.store, window))
    if not any(days.values()):
        return None
    due: dict[date, float] = {}
    for occ in led.ledger_rows(ctx.store, window, kind="expense"):
        due[occ.date] = due.get(occ.date, 0.0) + occ.amount
    return widget_data(
        sum(days.values()),
        days={when: value for when, value in days.items()},
        due=due,
        month=(window[0].year, window[0].month),
        span=window,
        peak=max(days.values()) if days else 0.0,
    )


def _calendar_span(ctx: HookContext, config: dict) -> tuple[date, date]:
    """The month containing the anchor's period — a calendar needs a whole month."""
    return per.range_for_lens(per.MONTH, ctx.start)


@hook("subscription_list")
def subscription_list(ctx: HookContext, config: dict) -> WidgetData | None:
    span = per.range_for_lens(ctx.lens, ctx.start)
    rows = shr.share_rows(ctx.store, *span)
    if not rows:
        return None
    return widget_data(
        sum(row.outstanding for row in rows),
        rows=[
            Row(label=row.occurrence.name,
                value=-row.occurrence.amount,
                detail=("settled" if row.settled else
                        f"{len(row.unpaid)} waiting · {row.outstanding:,.2f}"),
                reference={"kind": "item", "id": row.occurrence.item_id,
                           "date": row.occurrence.date, "unpaid": list(row.unpaid),
                           "outstanding": row.outstanding, "shares": dict(row.shares)})
            for row in rows
        ],
        note=f"{len(rows)} shared occurrences",
    )


@hook("uncategorised_review")
def uncategorised_review(ctx: HookContext, config: dict) -> WidgetData | None:
    count = len(cat.uncategorised(list(ctx.store.transactions)))
    if count == 0:
        return None
    suggestions = cat.suggest_rules(ctx.store, min_occurrences=2, limit=6)
    rows = [
        Row(label=suggestion.label, value=-suggestion.total,
            detail=f"{suggestion.occurrences} transactions",
            reference={"kind": "suggestion", "merchant": suggestion.merchant,
                       "category": suggestion.category, "tags": list(suggestion.tags),
                       "ids": list(suggestion.transaction_ids)})
        for suggestion in suggestions
    ]
    coverage = led.budget_coverage(ctx.store, ctx.span)
    return widget_data(
        count,
        rows=rows,
        note=cat.suggestions_summary(ctx.store),
        coverage=coverage.by_count,
        coverage_by_value=coverage.by_value,
    )


@hook("renewal_timeline")
def renewal_timeline(ctx: HookContext, config: dict) -> WidgetData | None:
    horizon_days = int(config.get("days", 60))
    renewals = det.upcoming_renewals(
        ctx.store, ctx.today, days=horizon_days,
        subscriptions_only=bool(config.get("subscriptions_only", False)))
    if not renewals:
        return None
    return widget_data(
        len(renewals),
        renewals=[{"date": occ.date, "name": occ.name, "amount": occ.amount,
                   "subscription": occ.subscription, "item_id": occ.item_id}
                  for occ in renewals],
        note=f"next {horizon_days} days",
        days=horizon_days,
        total=sum(occ.amount for occ in renewals),
    )


@hook("untracked_charges")
def untracked_charges(ctx: HookContext, config: dict) -> WidgetData | None:
    found = det.untracked_spend(ctx.store, today=ctx.today)
    if found.is_empty:
        return None
    return widget_data(
        found.monthly_total,
        rows=[
            Row(label=charge.suggested_name, value=-charge.monthly_estimate,
                detail=f"{charge.occurrences}× · about {charge.average_amount:,.2f}",
                reference={"kind": "charge", "merchant": charge.merchant,
                           "average": charge.average_amount,
                           "interval_days": charge.interval_days,
                           "ids": list(charge.transaction_ids)})
            for charge in found.charges[:6]
        ],
        note=f"{found.count} recurring charges not in the ledger",
        yearly=found.yearly_total,
    )


@hook("people_roster")
def people_roster(ctx: HookContext, config: dict) -> WidgetData | None:
    span = shr.default_span(ctx.store, ctx.today)
    roster = shr.people_roster(ctx.store, span)
    if not roster:
        return None
    return widget_data(
        sum(b.outstanding for b in roster),
        rows=[
            Row(label=balance.person.name, value=balance.outstanding,
                detail=(f"{balance.settled:,.2f} settled of {balance.owed:,.2f}"
                        if balance.settled else f"{balance.occurrences} occurrences"),
                reference={"kind": "person", "id": balance.person.id,
                           "owed": balance.owed, "settled": balance.settled,
                           "oldest": balance.oldest})
            for balance in roster
        ],
        note=f"{len(roster)} people",
    )


@hook("owed_to_you")
def owed_to_you(ctx: HookContext, config: dict) -> WidgetData | None:
    summary = shr.owed_summary(ctx.store, shr.default_span(ctx.store, ctx.today))
    if summary.is_empty:
        return None
    return widget_data(
        summary.total,
        rows=[Row(label=balance.person.name, value=balance.outstanding,
                  reference={"kind": "person", "id": balance.person.id})
              for balance in summary.people],
        note=f"{summary.count} people",
    )


@hook("consumables")
def consumables(ctx: HookContext, config: dict) -> WidgetData | None:
    items = list(ctx.store.tracker)
    if not items:
        return None
    costs = trk.tracker_costs(items, ctx.today)
    return widget_data(
        trk.tracker_total_spend(items),
        rows=[
            Row(label=cost.name, value=-cost.total,
                detail=(f"{cost.quantity:g} uses · {cost.per_use:,.2f} each"
                        if cost.quantity else f"{cost.per_use:,.2f} each"),
                reference={"kind": "consumable", "id": cost.item_id,
                           "per_use": cost.per_use, "per_day": cost.per_day,
                           "per_year": cost.per_year, "purchased_on": cost.purchased_on})
            for cost in costs
        ],
        note=f"{len(items)} tracked",
    )


@hook("cost_per_use")
def cost_per_use(ctx: HookContext, config: dict) -> WidgetData | None:
    items = list(ctx.store.tracker)
    if not items:
        return None
    costs = trk.tracker_costs(items, ctx.today)
    annual = trk.tracker_annualised_spend(items, ctx.today)
    return widget_data(
        annual,
        rows=[
            Row(label=cost.name,
                value=cost.per_use,
                detail=(f"{cost.per_day:,.2f}/day · {cost.per_year:,.0f}/year"
                        if cost.per_day else "bought today"),
                reference={"kind": "consumable", "id": cost.item_id,
                           "per_use": cost.per_use, "per_day": cost.per_day,
                           "per_year": cost.per_year})
            for cost in costs
        ],
        note="per year, at the pace so far",
        per_year=annual,
    )


@hook("predicted_income")
def predicted_income(ctx: HookContext, config: dict) -> WidgetData | None:
    ahead = int(config.get("periods", 1))
    span = per.step_lens(ctx.lens, ctx.start, ahead)
    projection = led.predicted_income(ctx.store, span)
    if projection.is_empty:
        return None
    return widget_data(
        projection.totals.income,
        rows=[Row(label=occ.name, value=occ.amount, detail=occ.date.isoformat(),
                  reference={"kind": "item", "id": occ.item_id, "date": occ.date})
              for occ in projection.sources],
        note=per.label_for(ctx.lens, span),
    )


@hook("net_worth")
def net_worth(ctx: HookContext, config: dict) -> WidgetData | None:
    series = an.net_worth_series(ctx.store, today=ctx.today)
    if not series.points and series.current == 0:
        return None
    return widget_data(
        series.current,
        delta=series.change,
        series=series.points if series.has_series else None,
        note=(f"including {', '.join(series.foreign)}" if series.foreign
              else f"in {ctx.store.currency}"),
        has_series=series.has_series,
        foreign=series.foreign,
    )


@hook("budget_pace")
def budget_pace(ctx: HookContext, config: dict) -> WidgetData | None:
    rows = led.budget_vs_actual(ctx.store, ctx.span, today=ctx.today)
    rows = [row for row in rows if row.planned or row.actual]
    if not rows:
        return None
    return widget_data(
        sum(row.variance for row in rows),
        rows=[
            Row(label=row.category, value=row.variance,
                detail=(f"{row.actual:,.0f} of {row.planned:,.0f}"
                        + (" · over pace" if row.over_pace else "")),
                reference={"kind": "category", "category": row.category,
                           "planned": row.planned, "actual": row.actual,
                           "variance": row.variance, "progress": row.progress,
                           "pace": row.pace, "pace_ratio": row.pace_ratio,
                           "on_track": row.on_track, "over_pace": row.over_pace})
            for row in rows
        ],
        note=f"{sum(1 for r in rows if not r.on_track)} of {len(rows)} over",
    )


# ------------------------------------------------------------------------ depth


@hook("metric_strip")
def metric_strip(ctx: HookContext, config: dict) -> WidgetData | None:
    summary = _totals_summary(ctx)
    window = summary.window
    if window is None or window.months == 0 or window.avg_income == 0:
        return None
    return widget_data(
        window.avg_net,
        metrics=[
            {"key": "income", "label": "Avg income", "value": window.avg_income,
             "series": window.series("income"), "sign": 1},
            {"key": "expenses", "label": "Avg expenses", "value": window.avg_expenses,
             "series": window.series("expenses"), "sign": -1},
            {"key": "net", "label": "Avg P&L", "value": window.avg_net,
             "series": window.series("net"), "sign": None},
            {"key": "rate", "label": "Avg saved",
             "value": window.avg_savings_rate, "series": [], "sign": None, "unit": "share"},
        ],
        months=window.months,
        note=f"over {window.months} months",
    )


@hook("record_months")
def record_months(ctx: HookContext, config: dict) -> WidgetData | None:
    count = int(config.get("months", 12))
    points = an.month_over_month(ctx.store, ctx.span, lens=per.MONTH, periods=count)
    if len(points) < 2:
        return None
    found = an.records(points)
    return widget_data(
        found.months_positive,
        records=[
            {"key": "best", "label": "Best month",
             "value": found.best.plan_net if found.best else None,
             "when": found.best.label if found.best else "", "sign": None},
            {"key": "worst", "label": "Worst month",
             "value": found.worst.plan_net if found.worst else None,
             "when": found.worst.label if found.worst else "", "sign": None},
            {"key": "positive", "label": "Months positive",
             "value": found.months_positive, "when": f"of {found.months}",
             "sign": None, "count": True},
            {"key": "growth", "label": "Income growth",
             "value": found.income_growth, "when": "first to last", "sign": None},
        ],
        note=f"{found.months_positive} of {found.months} positive",
        months=found.months,
    )


@hook("pl_trend")
def pl_trend(ctx: HookContext, config: dict) -> WidgetData | None:
    count = int(config.get("months", 12))
    points = an.month_over_month(ctx.store, ctx.span, lens=per.MONTH, periods=count)
    if len(points) < 2:
        return None
    selected = None
    peer = ctx.peer("category_donut")
    if peer is not None:
        selected = (peer.extra or {}).get("selected")
    return widget_data(
        sum(p.plan_net for p in points),
        series=[(p.date, p.plan_net) for p in points],
        labels=[p.label for p in points],
        selected=selected,
        points=[{"date": p.date, "label": p.label, "net": p.plan_net,
                 "income": p.plan.income, "expenses": p.plan.expenses} for p in points],
        note="click a month to filter",
    )


@hook("category_donut")
def category_donut(ctx: HookContext, config: dict) -> WidgetData | None:
    basis = config.get("basis", "actual")
    span = _donut_span(ctx, config)
    slices = an.spend_by_category(ctx.store, span, basis=basis)
    if not slices:
        return None
    selected = config.get("selected") or (slices[0][0] if slices else None)
    total = sum(value for _, value in slices)
    return widget_data(
        total,
        slices=[{"label": label, "value": value,
                 "share": value / total if total else 0.0}
                for label, value in slices],
        selected=selected,
        basis=basis,
        span=list(span),
        note=per.label_for(per.MONTH, span) if basis == "actual" else "recurring plan",
    )


def _donut_span(ctx: HookContext, config: dict) -> tuple[date, date]:
    months = int(config.get("months", 1))
    if months <= 1:
        return per.range_for_lens(per.MONTH, ctx.start)
    first = per.step_lens(per.MONTH, ctx.start, -(months - 1))[0]
    start = per.range_for_lens(per.MONTH, first)[0]
    return start, per.range_for_lens(per.MONTH, ctx.start)[1]


@hook("month_table")
def month_table(ctx: HookContext, config: dict) -> WidgetData | None:
    count = int(config.get("months", 12))
    points = an.month_over_month(ctx.store, ctx.span, lens=per.MONTH, periods=count)
    if not points:
        return None
    return widget_data(
        sum(p.plan_net for p in points),
        rows=[
            Row(label=p.label, value=p.plan_net,
                detail=f"{p.plan.income:,.0f} in · {p.plan.expenses:,.0f} out",
                reference={"kind": "month", "date": p.date,
                           "income": p.plan.income, "expenses": p.plan.expenses,
                           "actual_net": p.actual_net, "deviation": p.deviation})
            for p in reversed(points)
        ],
        note=f"{len(points)} months",
    )


@hook("budget_vs_actual")
def budget_vs_actual_hook(ctx: HookContext, config: dict) -> WidgetData | None:
    rows = led.budget_vs_actual(ctx.store, ctx.span, today=ctx.today)
    if not rows:
        # A considered empty state: a budget view with no budgets says so (§29).
        return widget_data(0.0, rows=[], note="no budgets yet")
    return widget_data(
        sum(row.actual for row in rows),
        rows=[
            Row(label=row.category, value=row.planned, detail=f"{row.actual:,.2f} actual",
                reference={"kind": "category", "category": row.category,
                           "planned": row.planned, "actual": row.actual,
                           "variance": row.variance, "progress": row.progress,
                           "pace": row.pace, "on_track": row.on_track})
            for row in rows
        ],
        planned=sum(row.planned for row in rows),
        actual=sum(row.actual for row in rows),
        note=ctx.label,
    )


@hook("liquid_forecast")
def liquid_forecast(ctx: HookContext, config: dict) -> WidgetData | None:
    months = int(config.get("months", 6))
    span = (ctx.start, per.range_for_lens(per.MONTH, per.step_lens(
        per.MONTH, ctx.start, months - 1)[0])[1])
    band = an.forecast_band(ctx.store, span, lens=per.MONTH)
    if band.is_flat:
        return None
    return widget_data(
        band.final,
        series=[(point.date, point.balance) for point in band.points],
        points=[{"label": point.label, "date": point.date, "balance": point.balance,
                 "low": point.low, "high": point.high, "net": point.plan_net}
                for point in band.points],
        opening=band.opening,
        low=band.final_low,
        high=band.final_high,
        sigma=band.sigma,
        first_negative=band.first_negative,
        note=f"in {ctx.store.currency}",
    )


@hook("spending_composition")
def spending_composition(ctx: HookContext, config: dict) -> WidgetData | None:
    basis = config.get("basis", "actual")
    slices = an.spend_by_category(ctx.store, ctx.span, basis=basis)
    if not slices:
        return None
    total = sum(value for _, value in slices)
    return widget_data(
        total,
        slices=[{"label": label, "value": value,
                 "share": value / total if total else 0.0}
                for label, value in slices],
        basis=basis,
        note=per.label_for(ctx.lens, ctx.span),
    )


@hook("plan_vs_actual")
def plan_vs_actual(ctx: HookContext, config: dict) -> WidgetData | None:
    summary = led.plan_summary(ctx.store, ctx.span)
    if summary.plan.count == 0 and summary.actual.count == 0:
        return None
    return widget_data(
        summary.plan.net,
        rows=[
            Row(label="Income", value=summary.plan.income,
                detail=f"{summary.actual.income:,.0f} reconciled"),
            Row(label="Expenses", value=-summary.plan.expenses,
                detail=f"{summary.actual.expenses:,.0f} reconciled"),
            Row(label="Net", value=summary.plan.net,
                detail=f"{summary.actual.net:,.0f} reconciled"),
        ],
        plan_in=summary.plan.income, plan_out=summary.plan.expenses,
        actual_in=summary.actual.income, actual_out=summary.actual.expenses,
        settled=summary.settled, occurrences=summary.occurrences,
        settled_ratio=summary.settled_ratio,
        note=ctx.label,
    )


@hook("goal_projection")
def goal_projection(ctx: HookContext, config: dict) -> WidgetData | None:
    projections = gl.goal_projections(ctx.store, ctx.today)
    if not projections:
        return None
    return widget_data(
        sum(projection.saved for projection in projections),
        rows=[
            Row(label=p.name, value=p.saved, detail=_goal_detail(p),
                reference={"kind": "goal", "id": p.goal_id, "target": p.target,
                           "monthly": p.monthly, "months": p.months, "eta": p.eta,
                           "progress": p.progress, "remaining": p.remaining,
                           "on_track": p.on_track})
            for p in projections
        ],
        projections=len(projections),
        note=f"{sum(1 for p in projections if p.on_track)} on track",
    )


@hook("cumulative_savings")
def cumulative_savings(ctx: HookContext, config: dict) -> WidgetData | None:
    count = int(config.get("months", 12))
    points = an.month_over_month(ctx.store, ctx.span, lens=per.MONTH, periods=count)
    if len(points) < 2:
        return None
    running = 0.0
    series: list[tuple[date, float]] = []
    for point in points:
        running += point.plan_net
        series.append((point.date, running))
    if running == 0:
        return None
    return widget_data(
        running,
        series=series,
        labels=[point.label for point in points],
        note=f"over {len(points)} months",
    )


@hook("savings_record")
def savings_record(ctx: HookContext, config: dict) -> WidgetData | None:
    count = int(config.get("months", 12))
    points = an.month_over_month(ctx.store, ctx.span, lens=per.MONTH, periods=count)
    if not points:
        return None
    found = an.savings_record(points)
    return widget_data(
        found.total,
        records=[
            {"key": "total", "label": "Total saved", "value": found.total,
             "when": f"over {found.months} months"},
            {"key": "average", "label": "Average", "value": found.average,
             "when": "per month"},
            {"key": "best", "label": "Best month", "value": found.best,
             "when": found.best_label},
            {"key": "positive", "label": "Months tracked", "value": found.months_positive,
             "when": f"of {found.months}", "count": True},
        ],
        note=f"{found.months} months tracked",
        months=found.months,
    )


@hook("spend_by_tag")
def spend_by_tag(ctx: HookContext, config: dict) -> WidgetData | None:
    slices = an.spend_by_tag(ctx.store, ctx.span)
    if not slices:
        return None
    total = sum(value for _, value in slices)
    return widget_data(
        total,
        slices=[{"label": label, "value": value,
                 "share": value / total if total else 0.0}
                for label, value in slices],
        note=ctx.label,
    )


@hook("daily_spend")
def daily_spend(ctx: HookContext, config: dict) -> WidgetData | None:
    days = an.daily_spend(ctx.store, ctx.span)
    if not any(value for _, value in days):
        return None
    return widget_data(
        sum(value for _, value in days),
        series=days,
        peak=max(value for _, value in days),
        label=per.label_for(ctx.lens, ctx.span),
    )


@hook("budget_report")
def budget_report(ctx: HookContext, config: dict) -> WidgetData | None:
    rows = led.budget_vs_actual(ctx.store, ctx.span, today=ctx.today)
    if not rows:
        return widget_data(0.0, rows=[], note="no budgets yet")
    coverage = led.budget_coverage(ctx.store, ctx.span)
    return widget_data(
        sum(row.planned for row in rows),
        rows=[
            Row(label=row.category, value=row.planned,
                detail=(f"{row.actual:,.2f} actual · {row.variance:+,.2f} "
                        f"({row.progress:.0%})"),
                reference={"kind": "category", "category": row.category,
                           "planned": row.planned, "actual": row.actual,
                           "variance": row.variance, "progress": row.progress,
                           "pace": row.pace, "pace_ratio": row.pace_ratio,
                           "on_track": row.on_track, "over_pace": row.over_pace})
            for row in rows
        ],
        planned=sum(row.planned for row in rows),
        actual=sum(row.actual for row in rows),
        coverage=coverage.by_count,
        coverage_value=coverage.by_value,
        note=f"{coverage.by_count:.0%} categorised",
    )

