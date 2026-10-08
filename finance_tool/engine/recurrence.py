"""Occurrence generation from definitions (§9, §11).

The one place a schedule is turned into dates. Two functions matter:

* :func:`occurrence_dates` — the dates a definition lands on in a range.
* :func:`effective` — what one of those dates actually *is*, once the per-occurrence
  overrides are applied.

Every consumer goes through these. There is no third way to ask what happens in a
month.

A definition's schedule is an RFC 5545 rule (:mod:`finance_tool.engine.rrule`), with one
deliberate deviation from the standard: a day that does not exist in a month clamps to
that month's last day rather than skipping it (§11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from finance_tool.engine.rrule import (
    DAYS_PER_MONTH,
    RRule,
    WEEKS_PER_MONTH,
    add_months,
    clamp_day,
    days_in_month,
    months_between,
    parse_rrule,
    serialise_rrule,
)
from finance_tool.store.entities import Item

# The key a definition's rule lives under. A definition with no rule is a one-off.
RRULE_KEY = "rrule"

ONCE = "once"
DAILY = "daily"
WEEKLY = "weekly"
MONTHLY = "monthly"
YEARLY = "yearly"

# The vocabulary a definition's schedule is described in — now derived from a rule's FREQ
# rather than being the set of shapes that exist. Kept as a name because the editor and the
# row descriptions still ask a definition what kind it is.
KINDS = (ONCE, DAILY, WEEKLY, MONTHLY, YEARLY)

# Overridable per occurrence (§9): amount, note, due. Anything else in `overrides` is
# carried through untouched rather than silently ignored.
OVERRIDABLE = ("amount", "note", "due")


# ------------------------------------------------------------------------ occurrence


@dataclass(frozen=True)
class Occurrence:
    """A definition landing on a specific date (§9), with its overrides applied."""

    item_id: str
    date: date
    name: str
    type: str
    amount: float          # unsigned; direction is carried by `type` (§10.2)
    due: date
    priority: int
    tags: tuple[str, ...]
    note: str
    subscription: bool
    shared: dict[str, Any] | None
    paid: bool
    cancelled: bool
    definition: Item = field(compare=False, repr=False)

    @property
    def signed_amount(self) -> float:
        return self.amount if self.type == "income" else -self.amount

    def is_overdue(self, today: date) -> bool:
        return not self.paid and not self.cancelled and self.due < today

    def is_due_soon(self, today: date, within: int = 7) -> bool:
        return (not self.paid and not self.cancelled
                and today <= self.due <= today + timedelta(days=within))

    def with_amount(self, amount: float) -> "Occurrence":
        return Occurrence(
            item_id=self.item_id, date=self.date, name=self.name, type=self.type,
            amount=amount, due=self.due, priority=self.priority, tags=self.tags,
            note=self.note, subscription=self.subscription, shared=self.shared,
            paid=self.paid, cancelled=self.cancelled, definition=self.definition,
        )


def sort_key(occ: Occurrence) -> tuple:
    """Due date, then priority, then name, then id (§11).

    A *total* order: the id is the last tiebreaker so two same-named, same-priority,
    same-due items still never shuffle between renders.
    """
    return (occ.due, occ.priority, occ.name.casefold(), occ.item_id)


# ------------------------------------------------------------------------- the kinds


def rule_of(defn: Item) -> RRule | None:
    """The parsed rule behind a definition, or ``None`` when it does not repeat.

    Total by construction (§15): a definition whose rule cannot be parsed is a one-off, not
    an exception in the middle of a ledger render. That is safe at *read* time because a
    query must never fail; it is why the editor refuses to save a rule it cannot parse.
    """
    rec = defn.recurrence
    if not rec:
        return None
    if isinstance(rec, str):
        return parse_rrule(rec)
    if isinstance(rec, dict):
        return parse_rrule(rec.get(RRULE_KEY))
    return None


def recurrence_kind(defn: Item) -> str:
    """The kind of a definition, derived from its rule's frequency."""
    rule = rule_of(defn)
    return ONCE if rule is None else rule.freq.lower()


def _bounds(defn: Item, start: date, end: date) -> tuple[date, date] | None:
    lower = max(start, defn.start) if defn.start else start
    upper = min(end, defn.end) if defn.end else end
    if start > end or lower > upper:
        return None
    return lower, upper


def _schedule(defn: Item, lower: date, upper: date) -> list[date]:
    """Raw scheduled dates in ``[lower, upper]``, cancelled occurrences included.

    One dispatch, as before: a definition with a parseable rule hands the range to that
    rule; a definition without one is a single occurrence on its own start date.
    """
    rule = rule_of(defn)
    if rule is None:
        anchor = defn.start
        return [] if anchor is None or not (lower <= anchor <= upper) else [anchor]
    return rule.dates(defn.start, lower, upper)


def occurrence_dates(
    defn: Item,
    start: date,
    end: date,
    *,
    include_cancelled: bool = False,
) -> list[date]:
    """Every date ``defn`` lands on between ``start`` and ``end``, inclusive.

    Cancelled occurrences are excluded by default, because "this occurrence doesn't
    happen" (§9) has to mean it is absent from a total. The ledger passes
    ``include_cancelled=True`` when it wants to render the row struck through.
    """
    bounds = _bounds(defn, start, end)
    if bounds is None:
        return []
    dates = _schedule(defn, *bounds)
    if include_cancelled or not defn.cancelled:
        return dates
    cancelled = set(defn.cancelled)
    return [d for d in dates if d not in cancelled]


def fires_in(defn: Item, start: date, end: date) -> bool:
    return bool(occurrence_dates(defn, start, end))


def count_in(defn: Item, start: date, end: date, *, include_cancelled: bool = False) -> int:
    return len(occurrence_dates(defn, start, end, include_cancelled=include_cancelled))


# -------------------------------------------------------------------------- resolved


def _due_date(defn: Item, occ: date, override: Any) -> date:
    """A real due date from the item's ``due`` spec, or the occurrence date itself.

    ``due`` is either a day-of-month ("1"), an ISO date, or nothing — the occurrence's
    own date is the fallback, so a bill without an explicit due date is due when it
    lands rather than not due at all.
    """
    spec = override if override is not None else defn.due
    if spec is None or spec == "":
        return occ
    if isinstance(spec, date):
        return spec
    text = str(spec).strip()
    if text.isdigit():
        return clamp_day(occ.year, occ.month, int(text))
    try:
        return date.fromisoformat(text)
    except ValueError:
        return occ


def effective(defn: Item, occ: date, *, cancelled: bool | None = None) -> Occurrence:
    """One occurrence of ``defn``, with its overrides and the definition's defaults."""
    override = defn.overrides.get(occ) or {}
    return Occurrence(
        item_id=defn.id,
        date=occ,
        name=override.get("name", defn.name),
        type=defn.type,
        amount=float(override.get("amount", defn.amount) or 0.0),
        due=_due_date(defn, occ, override.get("due")),
        priority=int(override.get("priority", defn.priority) or 0),
        tags=tuple(override.get("tags", defn.tags) or ()),
        note=override.get("note", defn.note) or "",
        subscription=bool(defn.subscription),
        shared=defn.shared,
        paid=occ in defn.paid,
        cancelled=defn.is_cancelled(occ) if cancelled is None else cancelled,
        definition=defn,
    )


def occurrences(
    defn: Item,
    start: date,
    end: date,
    *,
    include_cancelled: bool = False,
) -> list[Occurrence]:
    """The composition of :func:`occurrence_dates` and :func:`effective`, ordered.

    Not a third way to ask what happens — it is the same two functions, so a caller that
    needs rows rather than dates does not have to re-derive the pairing.
    """
    return [
        effective(defn, d) for d in
        occurrence_dates(defn, start, end, include_cancelled=include_cancelled)
    ]


def all_occurrences(
    defns: Sequence[Item],
    start: date,
    end: date,
    *,
    include_cancelled: bool = False,
) -> list[Occurrence]:
    """Every occurrence across a set of definitions, in one total order (§11)."""
    found: list[Occurrence] = []
    for defn in defns:
        found.extend(occurrences(defn, start, end, include_cancelled=include_cancelled))
    found.sort(key=sort_key)
    return found


# ----------------------------------------------------------------------------- next


def next_occurrence(defn: Item, after: date, *, inclusive: bool = False) -> date | None:
    """The first occurrence strictly after ``after``, or on it when ``inclusive``.

    Honours the definition's own ``end``: an ended item has no next occurrence, which is
    what stops a cancelled subscription reappearing in a renewal timeline.
    """
    lower = after if inclusive else after + timedelta(days=1)
    if defn.end is not None and lower > defn.end:
        return None

    if recurrence_kind(defn) == ONCE:
        anchor = defn.start
        if anchor is None or anchor < lower:
            return None
        return anchor if defn.end is None or anchor <= defn.end else None

    horizon = timedelta(days=400)
    limit = timedelta(days=365 * 40)
    while True:
        upper = after + horizon
        if defn.end is not None:
            upper = min(upper, defn.end)
        dates = occurrence_dates(defn, lower, upper)
        if dates:
            return dates[0]
        if defn.end is not None and upper >= defn.end:
            return None
        if horizon >= limit:
            return None
        horizon *= 2


def next_occurrence_of(defns: Sequence[Item], after: date) -> Occurrence | None:
    """The soonest next occurrence across many definitions."""
    best: tuple[date, tuple] | None = None
    for defn in defns:
        found = next_occurrence(defn, after)
        if found is None:
            continue
        candidate = effective(defn, found)
        key = (found, sort_key(candidate))
        if best is None or key < best:
            best = key
            chosen = candidate
    return chosen if best is not None else None


# ------------------------------------------------------------------ monthly equivalence


def monthly_equiv(defn: Item, *, amount: float | None = None) -> float:
    """The definition's amount expressed per month.

    Used by subscription rollups and the cost summary. A one-off is ``0.0`` — the concert
    ticket is not a monthly cost, and averaging it in would understate every subscription
    on the canvas.

    The rate is a property of the *rule*, not of any range the caller happens to be looking
    at, so the rollup and the scheduler cannot disagree about the same plan. Clamping is
    what makes this exact: a rule on the 31st fires twelve times a year *because* it clamps.
    """
    value = float(defn.amount if amount is None else amount)
    rule = rule_of(defn)
    if rule is None or defn.start is None:
        # No start, no phase to anchor to, so nothing is scheduled — and therefore nothing
        # is spent. Charging a monthly cost for a rule that never fires is the same
        # disagreement between the rollup and the scheduler that §1 forbids.
        return 0.0
    return value * rule.rate_per_month()


def yearly_equiv(defn: Item) -> float:
    return monthly_equiv(defn) * 12
