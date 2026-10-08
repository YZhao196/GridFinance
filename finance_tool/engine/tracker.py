"""Consumables and their cost per use, per day and per year (§14, §29).

A tracker item records what was bought, how many uses it covers, and what one use
costs. Everything else is arithmetic — which is the point: the app can say "$0.50 a day
on coffee beans" without a second data entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

from finance_tool.store.entities import TrackerItem


@dataclass(frozen=True)
class TrackerCost:
    """One consumable's economics, as of a date."""

    item_id: str
    name: str
    quantity: float
    per_use: float
    total: float
    days_elapsed: int | None
    per_day: float | None
    per_year: float | None
    purchased_on: date | None


def tracker_cost_per_use(item: TrackerItem) -> float:
    """What a single use costs. Zero for a degenerate quantity, never an error."""
    return float(item.per_use_amount or 0.0)


def tracker_total(item: TrackerItem) -> float:
    """What the whole purchase cost: every use, at the per-use price."""
    return float(item.quantity or 0.0) * float(item.per_use_amount or 0.0)


def tracker_days_elapsed(item: TrackerItem, today: date) -> int | None:
    """Days since purchase, or ``None`` when there is no purchase date to measure from."""
    if item.purchased_on is None:
        return None
    return max(0, (today - item.purchased_on).days)


def tracker_cost_per_day(item: TrackerItem, today: date) -> float | None:
    """The purchase amortised over the days since it was made.

    ``None`` rather than infinity on the day of purchase: "cost per day" on day zero is
    not a very large number, it is a question not yet answered.
    """
    days = tracker_days_elapsed(item, today)
    if days is None or days <= 0:
        return None
    return tracker_total(item) / days


def tracker_cost_per_year(item: TrackerItem, today: date) -> float | None:
    per_day = tracker_cost_per_day(item, today)
    return None if per_day is None else per_day * 365


def tracker_costs(items: Sequence[TrackerItem], today: date) -> list[TrackerCost]:
    """Every tracker item's economics, most expensive per year first."""
    found = [
        TrackerCost(
            item_id=item.id,
            name=item.name,
            quantity=float(item.quantity or 0.0),
            per_use=tracker_cost_per_use(item),
            total=tracker_total(item),
            days_elapsed=tracker_days_elapsed(item, today),
            per_day=tracker_cost_per_day(item, today),
            per_year=tracker_cost_per_year(item, today),
            purchased_on=item.purchased_on,
        )
        for item in items
    ]
    found.sort(key=lambda cost: (-(cost.per_year or 0.0), cost.name.casefold()))
    return found


def tracker_total_spend(items: Sequence[TrackerItem]) -> float:
    return sum(tracker_total(item) for item in items)


def tracker_annualised_spend(items: Sequence[TrackerItem], today: date) -> float:
    """What these consumables cost per year at their observed pace.

    Items with no purchase date are omitted rather than guessed at — an unmeasurable
    rate is not a zero rate.
    """
    return sum(
        cost for cost in (tracker_cost_per_year(item, today) for item in items)
        if cost is not None
    )
