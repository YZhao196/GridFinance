"""Lenses and date ranges (§14).

A *lens* is the window the whole app is looking through — week, month, quarter or year.
Everything that needs a range asks here, so the hero figure, the ledger and the charts
can never disagree about what "this month" means.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Iterator, Sequence

WEEK = "week"
MONTH = "month"
QUARTER = "quarter"
YEAR = "year"

LENSES: tuple[str, ...] = (WEEK, MONTH, QUARTER, YEAR)

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

DateRange = tuple[date, date]


# ------------------------------------------------------------------ primitives


def add_months(anchor: date, months: int) -> date:
    """Shift by calendar months, clamping the day to the target month's length."""
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def month_start(anchor: date) -> date:
    return date(anchor.year, anchor.month, 1)


def month_end(anchor: date) -> date:
    return date(anchor.year, anchor.month, calendar.monthrange(anchor.year, anchor.month)[1])


def quarter_of(anchor: date) -> int:
    return (anchor.month - 1) // 3 + 1


def quarter_start(anchor: date) -> date:
    return date(anchor.year, (quarter_of(anchor) - 1) * 3 + 1, 1)


def year_start(anchor: date) -> date:
    return date(anchor.year, 1, 1)


def year_end(anchor: date) -> date:
    return date(anchor.year, 12, 31)


def iso_week_range(anchor: date) -> DateRange:
    """The Monday–Sunday ISO week containing ``anchor``."""
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)


def iso_week_number(anchor: date) -> int:
    return anchor.isocalendar().week


def normalise_lens(lens: str | None) -> str:
    """Total by construction (§15): an unknown lens is a month, never an exception."""
    return lens if lens in LENSES else MONTH


# ------------------------------------------------------------------ range_for_lens


def range_for_lens(lens: str, anchor: date) -> DateRange:
    """The period of ``lens`` containing ``anchor``, inclusive at both ends."""
    kind = normalise_lens(lens)

    if kind == WEEK:
        return iso_week_range(anchor)
    if kind == MONTH:
        return month_start(anchor), month_end(anchor)
    if kind == QUARTER:
        start = quarter_start(anchor)
        return start, month_end(add_months(start, 2))
    return year_start(anchor), year_end(anchor)


def step_lens(lens: str, anchor: date, steps: int) -> DateRange:
    """The range ``steps`` periods away from the one containing ``anchor``."""
    kind = normalise_lens(lens)
    if steps == 0:
        return range_for_lens(kind, anchor)

    if kind == WEEK:
        moved = anchor + timedelta(days=7 * steps)
    elif kind == MONTH:
        moved = add_months(anchor, steps)
    elif kind == QUARTER:
        moved = add_months(anchor, 3 * steps)
    else:
        moved = _replace_year(anchor, anchor.year + steps)
    return range_for_lens(kind, moved)


def previous_range(lens: str, span: DateRange) -> DateRange:
    return step_lens(lens, span[0], -1)


def next_range(lens: str, span: DateRange) -> DateRange:
    return step_lens(lens, span[0], 1)


def contains(span: DateRange, when: date) -> bool:
    return span[0] <= when <= span[1]


def clamp_range(span: DateRange, start: date, end: date) -> DateRange | None:
    """The overlap of two ranges, or ``None`` when they do not meet."""
    lower, upper = max(span[0], start), min(span[1], end)
    return None if lower > upper else (lower, upper)


def days_in(span: DateRange) -> int:
    return (span[1] - span[0]).days + 1


def day_count_elapsed(span: DateRange, today: date) -> int:
    """Days of ``span`` that have already happened, for pace calculations.

    Clamped to the span, so a future period reports 0 rather than a negative proportion.
    """
    if today < span[0]:
        return 0
    if today >= span[1]:
        return days_in(span)
    return (today - span[0]).days + 1


def _replace_year(anchor: date, year: int) -> date:
    return date(year, anchor.month, min(anchor.day, calendar.monthrange(year, anchor.month)[1]))


# ------------------------------------------------------------------------ multiples


def month_ranges(start: date, end: date) -> list[DateRange]:
    """Every calendar month overlapping ``[start, end]``, each clipped to it."""
    if start > end:
        return []
    spans: list[DateRange] = []
    cursor = month_start(start)
    while cursor <= end:
        spans.append((max(cursor, start), min(month_end(cursor), end)))
        cursor = add_months(cursor, 1)
    return spans


def month_starts(start: date, end: date) -> list[date]:
    """The first of every month in ``[start, end]``. The spine of the history views."""
    if start > end:
        return []
    found: list[date] = []
    cursor = month_start(start)
    while cursor <= end:
        found.append(cursor)
        cursor = add_months(cursor, 1)
    return found


def week_ranges(start: date, end: date) -> list[DateRange]:
    """Every ISO week overlapping ``[start, end]``, each clipped to it.

    Clipped rather than whole, so summing the weekly bars of a period gives exactly that
    period's total. A month that begins on a Thursday shows a four-day opening bar — a
    truthful one — instead of borrowing September's tail into October's ledger.
    """
    if start > end:
        return []
    spans: list[DateRange] = []
    cursor = iso_week_range(start)[0]
    while cursor <= end:
        spans.append((max(cursor, start), min(cursor + timedelta(days=6), end)))
        cursor += timedelta(days=7)
    return spans


def sub_ranges(lens: str, start: date, end: date) -> list[DateRange]:
    """The periods of ``lens`` tiling ``[start, end]``, clipped at both ends.

    This is what makes a history view work at any resolution: asking for a year of weeks
    gives fifty-odd bars, and asking for a year of months gives twelve.
    """
    kind = normalise_lens(lens)
    if kind == WEEK:
        return week_ranges(start, end)
    if kind == MONTH:
        return month_ranges(start, end)
    if kind == QUARTER:
        return _quarter_ranges(start, end)
    return _year_ranges(start, end)


def _quarter_ranges(start: date, end: date) -> list[DateRange]:
    if start > end:
        return []
    spans: list[DateRange] = []
    cursor = quarter_start(start)
    while cursor <= end:
        spans.append((max(cursor, start), min(month_end(add_months(cursor, 2)), end)))
        cursor = add_months(cursor, 3)
    return spans


def _year_ranges(start: date, end: date) -> list[DateRange]:
    if start > end:
        return []
    spans: list[DateRange] = []
    for year in range(start.year, end.year + 1):
        lower = max(date(year, 1, 1), start)
        upper = min(date(year, 12, 31), end)
        if lower <= upper:
            spans.append((lower, upper))
    return spans


def each_day(start: date, end: date) -> Iterator[date]:
    if start > end:
        return iter(())
    return (start + timedelta(days=offset) for offset in range((end - start).days + 1))


def month_grid(year: int, month: int, *, first_weekday: int = 0) -> list[list[date | None]]:
    """A calendar grid for the spending calendar widget (§29).

    Monday-first by default, padded with ``None`` so every week is seven wide and every
    month is a rectangle — which is what makes the spend shading readable.
    """
    weeks = calendar.Calendar(firstweekday=first_weekday).monthdatescalendar(year, month)
    grid: list[list[date | None]] = []
    for week in weeks:
        row: list[date | None] = []
        for day in week:
            row.append(day if day.month == month else None)
        grid.append(row)
    return grid


# ----------------------------------------------------------------------------- labels


def label_for(lens: str, span: DateRange) -> str:
    """A human label for a period: "October 2026", "Q4 2026", "6–12 Oct 2026"."""
    kind = normalise_lens(lens)
    start = span[0]

    if kind == MONTH:
        return f"{MONTH_NAMES[start.month - 1]} {start.year}"
    if kind == QUARTER:
        return f"Q{quarter_of(start)} {start.year}"
    if kind == YEAR:
        return str(start.year)
    return _span_label(start, span[1])


def _span_label(start: date, end: date) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {MONTH_NAMES[start.month - 1][:3]} {start.year}"
    if start.year == end.year:
        return (f"{start.day} {MONTH_NAMES[start.month - 1][:3]} – "
                f"{end.day} {MONTH_NAMES[end.month - 1][:3]} {start.year}")
    return (f"{start.day} {MONTH_NAMES[start.month - 1][:3]} {start.year} – "
            f"{end.day} {MONTH_NAMES[end.month - 1][:3]} {end.year}")


def short_label_for_lens(lens: str) -> str:
    """The lens's own name, for a lens switcher's label."""
    return {WEEK: "Week", MONTH: "Month", QUARTER: "Quarter", YEAR: "Year"}[normalise_lens(lens)]


def lens_containing(lens: str, span: DateRange) -> Sequence[DateRange]:
    """The surrounding periods, for a lens switcher: previous, current, next."""
    return (previous_range(lens, span), span, next_range(lens, span))
