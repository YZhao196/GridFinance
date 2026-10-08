"""RFC 5545 recurrence rules, with one deliberate deviation (§11).

A rule is text — ``FREQ=MONTHLY;BYMONTHDAY=31`` — parsed to a frozen :class:`RRule` and
evaluated by :meth:`RRule.dates`. This is the only place recurrence text means anything;
:mod:`finance_tool.engine.recurrence` is its only importer, so §9's "there is no third way to
ask what happens in a month" still holds.

**The deviation.** RFC 5545 says a day-of-month that does not exist in a given month yields
no occurrence, so ``BYMONTHDAY=31`` would fire seven times a year. Here it clamps to that
month's last day and fires twelve, because the design doc is emphatic that a monthly bill on
the 31st must fire in February (§11, §37). The same clamping applies to a ``BYDAY`` ordinal
past the end of a month: ``5FR`` in a four-Friday month is that month's last Friday.

Time-of-day and week-of-year parts (``BYHOUR``, ``BYMINUTE``, ``BYSECOND``, ``BYWEEKNO``,
``BYYEARDAY``) are parsed and recorded in :attr:`RRule.ignored` rather than refused, because
this is a date-only ledger. A caller that *can* refuse — the editor — is expected to; a
caller that is only rendering is not. Nothing here raises, ever (§15.4).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

# The project's convention for expressing a cadence per month. Calendar-agnostic on
# purpose: a recurring cost has no single "right" month count, and 52/12 is what every
# subscription rollup uses.
WEEKS_PER_MONTH = 52 / 12
DAYS_PER_MONTH = 365.25 / 12

DAILY = "DAILY"
WEEKLY = "WEEKLY"
MONTHLY = "MONTHLY"
YEARLY = "YEARLY"

FREQS = (DAILY, WEEKLY, MONTHLY, YEARLY)

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_WEEKDAY_INDEX = {code: index for index, code in enumerate(WEEKDAY_CODES)}

# Parts we understand and deliberately drop: this ledger has dates but no times, and
# BYWEEKNO/BYYEARDAY are ISO-week exotica no personal bill uses.
IGNORABLE = ("BYHOUR", "BYMINUTE", "BYSECOND", "BYWEEKNO", "BYYEARDAY")

# A 400-year Gregorian cycle is exactly 146097 days — a whole number of weeks (20871) and
# of months — so counting over it gives an anchor-independent rate for the handful of rules
# that have no closed form.
_CYCLE_START = date(2000, 1, 1)
_CYCLE_END = date(2399, 12, 31)
_CYCLE_MONTHS = 400 * 12

_MAX_PERIODS = 200_000

_BYDAY_RE = re.compile(r"^([+-]?\d{1,2})?([A-Z]{2})$")


# ------------------------------------------------------------------ calendar helpers


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def add_months(anchor: date, months: int) -> date:
    """``anchor`` shifted by ``months``, clamped to the target month's length.

    Anchored rather than iterative, so a monthly interval from the 31st lands on the 31st
    again in March instead of drifting to the 28th after passing through February.
    """
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(anchor.day, days_in_month(year, month)))


def months_between(first: date, second: date) -> int:
    return (second.year - first.year) * 12 + (second.month - first.month)


def clamp_day(year: int, month: int, day: int) -> date:
    """**The month-end clamping rule (§11).** Never skips, never rolls over."""
    return date(year, month, min(max(day, 1), days_in_month(year, month)))


# --------------------------------------------------------------------------- parsing


def _positive(text: str) -> int | None:
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _ints(text: str, *, low: int, high: int, allow_negative: bool) -> tuple[int, ...] | None:
    out: list[int] = []
    floor = -high if allow_negative else low
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            return None
        if value == 0 or value > high or value < floor:
            return None
        out.append(value)
    return tuple(out) or None


def _parse_byday(text: str) -> tuple[tuple[int, int], ...] | None:
    found: list[tuple[int, int]] = []
    for chunk in str(text).split(","):
        chunk = chunk.strip().upper()
        if not chunk:
            continue
        match = _BYDAY_RE.match(chunk)
        if not match:
            return None
        ordinal_text, code = match.groups()
        if code not in _WEEKDAY_INDEX:
            return None
        ordinal = int(ordinal_text) if ordinal_text else 0
        if abs(ordinal) > 53:
            return None
        found.append((ordinal, _WEEKDAY_INDEX[code]))
    return tuple(found) or None


def _parse_until(text: str) -> date | None:
    """``20261231``, ``2026-12-31`` or ``20261231T000000Z`` — the date part only."""
    cleaned = str(text).strip().upper().rstrip("Z")
    if len(cleaned) > 8 and "T" in cleaned:
        cleaned = cleaned.split("T", 1)[0]
    if "-" in cleaned:
        try:
            return date.fromisoformat(cleaned)
        except ValueError:
            return None
    if len(cleaned) == 8 and cleaned.isdigit():
        try:
            return date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:8]))
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class RRule:
    """A parsed recurrence rule.

    ``byday`` holds ``(ordinal, weekday)`` with the ordinal ``0`` meaning "every such
    weekday in the period" and a negative ordinal counting back from the end. Frozen so it
    can be cached on and compared.
    """

    freq: str = ""
    interval: int = 1
    bymonth: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    byday: tuple[tuple[int, int], ...] = ()
    bysetpos: tuple[int, ...] = ()
    count: int | None = None
    until: date | None = None
    wkst: int = 0
    ignored: tuple[str, ...] = ()

    # -- periods ----------------------------------------------------------

    def _week_start(self, anchor: date) -> date:
        return anchor - timedelta(days=(anchor.weekday() - self.wkst) % 7)

    def _period_start(self, dtstart: date, index: int) -> date:
        if self.freq == DAILY:
            return dtstart + timedelta(days=index * self.interval)
        if self.freq == WEEKLY:
            return self._week_start(dtstart) + timedelta(weeks=index * self.interval)
        if self.freq == MONTHLY:
            return add_months(date(dtstart.year, dtstart.month, 1), index * self.interval)
        return date(dtstart.year + index * self.interval, 1, 1)

    def _index_for(self, dtstart: date, moment: date) -> int:
        """The period index whose period contains ``moment``, floored at zero.

        Every branch divides by ``interval`` — the index counts *periods*, and a period is
        ``interval`` units long. Forgetting that on the weekly branch meant a range opening
        months into a series skipped over occurrences instead of starting at them.
        """
        if moment <= dtstart:
            return 0
        if self.freq == DAILY:
            units = (moment - dtstart).days
        elif self.freq == WEEKLY:
            units = (moment - self._week_start(dtstart)).days // 7
        elif self.freq == MONTHLY:
            units = months_between(date(dtstart.year, dtstart.month, 1), moment)
        else:
            units = months_between(date(dtstart.year, 1, 1), moment) // 12
        return max(0, units // self.interval)

    # -- expansion --------------------------------------------------------

    def _month_candidates(self, year: int, month: int, dtstart: date) -> list[date]:
        """The days this rule selects in one month, with clamping applied."""
        length = days_in_month(year, month)

        days: set[int] | None = None
        if self.bymonthday:
            days = set()
            for value in self.bymonthday:
                if value > 0:
                    days.add(min(value, length))          # clamp: 31 -> Feb 28
                else:
                    index = length + value + 1
                    if 1 <= index <= length:
                        days.add(index)

        weekdays: set[int] | None = None
        if self.byday:
            weekdays = set()
            for ordinal, weekday in self.byday:
                matches = [day for day in range(1, length + 1)
                           if date(year, month, day).weekday() == weekday]
                if not matches:
                    continue
                if ordinal == 0:
                    weekdays.update(matches)
                elif ordinal > 0:
                    # Clamped, like BYMONTHDAY: the 5th Friday of a four-Friday month is
                    # that month's last Friday rather than nothing at all.
                    weekdays.add(matches[min(ordinal, len(matches)) - 1])
                else:
                    weekdays.add(matches[max(len(matches) + ordinal, 0)])

        if days is None and weekdays is None:
            days = {min(dtstart.day, length)}
        if days is not None and weekdays is not None:
            chosen = days & weekdays
        else:
            chosen = days if days is not None else weekdays
        return [date(year, month, day) for day in sorted(chosen or ())]

    def _passes_day_filters(self, day: date) -> bool:
        if self.bymonth and day.month not in self.bymonth:
            return False
        length = days_in_month(day.year, day.month)
        if self.bymonthday:
            allowed = {(min(value, length) if value > 0 else length + value + 1)
                       for value in self.bymonthday}
            if day.day not in allowed:
                return False
        if self.byday and day.weekday() not in {weekday for _o, weekday in self.byday}:
            return False
        return True

    def _expand(self, period_start: date, dtstart: date) -> list[date]:
        if self.freq == DAILY:
            return [period_start] if self._passes_day_filters(period_start) else []

        if self.freq == WEEKLY:
            allowed = ({weekday for _o, weekday in self.byday} if self.byday
                       else {dtstart.weekday()})
            week = [period_start + timedelta(days=offset) for offset in range(7)]
            return [day for day in week
                    if day.weekday() in allowed and self._passes_day_filters(day)]

        if self.freq == MONTHLY:
            if self.bymonth and period_start.month not in self.bymonth:
                return []
            return self._month_candidates(period_start.year, period_start.month, dtstart)

        # YEARLY: BYMONTH selects the months; with neither BYMONTH nor BYMONTHDAY nor
        # BYDAY, RFC 5545 takes the month from DTSTART.
        months = self.bymonth or (dtstart.month,)
        found: list[date] = []
        for month in sorted(months):
            found.extend(self._month_candidates(period_start.year, month, dtstart))
        return sorted(found)

    def _apply_setpos(self, candidates: list[date]) -> list[date]:
        if not self.bysetpos:
            return candidates
        ordered = sorted(candidates)
        chosen = []
        for position in self.bysetpos:
            if position == 0:
                continue
            index = position - 1 if position > 0 else len(ordered) + position
            if 0 <= index < len(ordered):
                chosen.append(ordered[index])
        return sorted(set(chosen))

    # -- the public evaluation -------------------------------------------

    def dates(self, dtstart: date | None, lower: date, upper: date) -> list[date]:
        """Every date this rule lands on in ``[lower, upper]``, ascending.

        Total: a missing start, an unknown frequency, a contradictory selection or a
        reversed range all return ``[]`` rather than raising (§15.4).
        """
        if dtstart is None or lower > upper or self.freq not in FREQS:
            return []

        # COUNT is counted from the series' first occurrence, so it has to walk from
        # DTSTART. Without one, walking can begin wherever the caller is looking.
        first = 0 if self.count is not None else self._index_for(dtstart, lower)

        found: list[date] = []
        emitted = 0
        for index in range(first, first + _MAX_PERIODS):
            period_start = self._period_start(dtstart, index)
            if period_start > upper:
                break
            for candidate in self._apply_setpos(self._expand(period_start, dtstart)):
                if candidate < dtstart:
                    continue
                if self.until is not None and candidate > self.until:
                    return found
                if self.count is not None and emitted >= self.count:
                    return found
                emitted += 1
                # A period whose *start* is inside the range can still run past its end —
                # a week straddling the boundary has days on both sides of it. Bounds are
                # checked on the candidate, never on the period.
                if lower <= candidate <= upper:
                    found.append(candidate)
        return found

    def count_between(self, dtstart: date | None, lower: date, upper: date) -> int:
        """How many dates this rule lands on — the same walk without building a list."""
        return len(self.dates(dtstart, lower, upper))

    # -- rate -------------------------------------------------------------

    def rate_per_month(self) -> float:
        """Occurrences per month, ignoring COUNT/UNTIL and the item's own end.

        Steady state, not a window: the figure is a property of the *rule*, so it does not
        depend on where you look. A 12-month window would give a weekly rule 52 or 53
        depending on where it opened, which is why this is derived from the rule instead.
        """
        if self.freq == DAILY:
            # Daily is the one frequency where the cycle fallback is expensive — it means
            # walking 146,097 days — so every shape it can meet gets a closed form. Only a
            # combination of two selections, or an unaligned interval, still needs the walk.
            if self.bysetpos or (self.bymonthday and self.byday):
                return self._cycle_rate()
            month_factor = (len(self.bymonth) / 12.0) if self.bymonth else 1.0
            if self.bymonthday:
                if self.interval != 1:
                    return self._cycle_rate()
                return month_factor * len(self.bymonthday)
            per_day = len({weekday for _o, weekday in self.byday}) / 7.0 \
                if self.byday else 1.0
            return per_day * month_factor * DAYS_PER_MONTH / self.interval

        if self.freq == WEEKLY:
            plain = not (self.bymonth or self.bymonthday or self.bysetpos)
            if not plain:
                return self._cycle_rate()
            selections = len({weekday for _o, weekday in self.byday}) or 1
            return selections * WEEKS_PER_MONTH / self.interval

        if self.freq == MONTHLY:
            if self.bysetpos or (self.bymonthday and self.byday):
                return self._cycle_rate()
            if self.bymonth:
                if self.interval != 1:
                    return self._cycle_rate()
                return len(self.bymonth) / 12.0
            if self.bymonthday:
                return len(self.bymonthday) / self.interval
            if self.byday:
                if all(ordinal == 0 for ordinal, _w in self.byday):
                    weekdays = len({weekday for _o, weekday in self.byday})
                    return weekdays * WEEKS_PER_MONTH / self.interval
                return len(self.byday) / self.interval
            return 1.0 / self.interval

        if self.freq == YEARLY:
            months = len(self.bymonth) or 1
            if self.bymonthday:
                per_month = len(self.bymonthday)
            elif self.byday:
                if all(ordinal == 0 for ordinal, _w in self.byday):
                    per_month = len({weekday for _o, weekday in self.byday}) * WEEKS_PER_MONTH
                else:
                    per_month = len(self.byday)
            else:
                per_month = 1
            if self.bysetpos:
                return self._cycle_rate()
            return months * per_month / (12 * self.interval)

        return 0.0

    def _cycle_rate(self) -> float:
        return _cycle_count(self) / _CYCLE_MONTHS

    # -- english ----------------------------------------------------------

    def describe(self) -> str:
        return _describe(self)


@lru_cache(maxsize=256)
def _cycle_count(rule: RRule) -> int:
    """Occurrences in one whole 400-year cycle — exact, and independent of any anchor."""
    return rule.count_between(_CYCLE_START, _CYCLE_START, _CYCLE_END)


def parse_rrule(text: str | None) -> RRule | None:
    """Parse RRULE text. Returns ``None`` when there is no usable ``FREQ``.

    Never raises. Parts that are understood but unusable here — and keys that are not
    recognized at all — are collected into :attr:`RRule.ignored` so that a caller which can
    refuse (the editor) sees exactly what would be dropped.
    """
    if not text:
        return None
    raw = str(text).strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:].strip()
    if not raw:
        return None

    parts: dict[str, str] = {}
    ignored: list[str] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        key = key.strip().upper()
        if not sep or not key:
            ignored.append(chunk.upper())
            continue
        parts[key] = value.strip()

    freq = parts.pop("FREQ", "").strip().upper()
    if freq not in FREQS:
        return None

    interval = _positive(parts.pop("INTERVAL", "1"))
    if interval is None:
        # A malformed INTERVAL is not a reason to drop the rule; fall back to 1 and say so.
        interval = 1
        ignored.append("INTERVAL")

    wkst_text = parts.pop("WKST", "").strip().upper()
    wkst = _WEEKDAY_INDEX.get(wkst_text)
    if wkst is None:
        if wkst_text:
            ignored.append("WKST")
        wkst = 0

    bymonth = _pop_ints(parts, "BYMONTH", 1, 12, False, ignored)
    bymonthday = _pop_ints(parts, "BYMONTHDAY", 1, 31, True, ignored)
    bysetpos = _pop_ints(parts, "BYSETPOS", 1, 366, True, ignored)
    byday = _pop_byday(parts, ignored)
    count = _pop_count(parts, ignored)
    until = _pop_until(parts, ignored)

    # Whatever remains was not recognized at all; IGNORABLE parts were understood and
    # dropped. Both are reported, so the editor can refuse rather than lose them silently.
    for key in parts:
        if key not in ignored:
            ignored.append(key)
    ordered: list[str] = []
    for key in IGNORABLE:
        if key in ignored and key not in ordered:
            ordered.append(key)
    for key in ignored:
        if key not in ordered:
            ordered.append(key)

    return RRule(freq=freq, interval=interval, bymonth=bymonth, bymonthday=bymonthday,
                 byday=byday, bysetpos=bysetpos, count=count, until=until, wkst=wkst,
                 ignored=tuple(ordered))


def _pop_ints(parts, key, low, high, allow_negative, ignored):
    if key not in parts:
        return ()
    value = _ints(parts.pop(key), low=low, high=high, allow_negative=allow_negative)
    if value is None:
        ignored.append(key)
        return ()
    return value


def _pop_byday(parts, ignored):
    if "BYDAY" not in parts:
        return ()
    value = _parse_byday(parts.pop("BYDAY"))
    if value is None:
        ignored.append("BYDAY")
        return ()
    return value


def _pop_count(parts, ignored):
    if "COUNT" not in parts:
        return None
    value = _positive(parts.pop("COUNT"))
    if value is None:
        ignored.append("COUNT")
        return None
    return value


def _pop_until(parts, ignored):
    if "UNTIL" not in parts:
        return None
    value = _parse_until(parts.pop("UNTIL"))
    if value is None:
        ignored.append("UNTIL")
        return None
    return value


def serialise_rrule(rule: RRule | None) -> str | None:
    """The canonical RRULE text for a rule, or ``None`` if it has no frequency."""
    if rule is None or rule.freq not in FREQS:
        return None
    parts = [f"FREQ={rule.freq}"]
    if rule.interval != 1:
        parts.append(f"INTERVAL={rule.interval}")
    if rule.bymonth:
        parts.append("BYMONTH=" + ",".join(str(v) for v in rule.bymonth))
    if rule.bymonthday:
        parts.append("BYMONTHDAY=" + ",".join(str(v) for v in rule.bymonthday))
    if rule.byday:
        parts.append("BYDAY=" + ",".join(
            f"{'' if ordinal == 0 else ordinal}{WEEKDAY_CODES[weekday]}"
            for ordinal, weekday in rule.byday))
    if rule.bysetpos:
        parts.append("BYSETPOS=" + ",".join(str(v) for v in rule.bysetpos))
    if rule.count is not None:
        parts.append(f"COUNT={rule.count}")
    if rule.until is not None:
        parts.append("UNTIL=" + rule.until.strftime("%Y%m%d"))
    if rule.wkst != 0:
        parts.append(f"WKST={WEEKDAY_CODES[rule.wkst]}")
    return ";".join(parts)


# ------------------------------------------------------------------------ english


_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
_ORDINAL_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                  "Sunday")


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _describe(rule: RRule) -> str:
    """Plain English for a rule, with the one deviation stated rather than hidden."""
    if rule.freq not in FREQS:
        return "does not repeat"

    every = "" if rule.interval == 1 else f"every {rule.interval} "
    # A day past the 28th cannot exist in every month, so say where it lands instead of
    # letting the user discover it in February. This is the one deviation from RFC 5545.
    clamp = (", landing on the last day of a shorter month"
             if any(value > 28 for value in rule.bymonthday) else "")

    if rule.freq == DAILY:
        return f"{every}daily" if rule.interval > 1 else "daily"

    if rule.freq == WEEKLY:
        plain = [_WEEKDAY_NAMES[w] for o, w in rule.byday if o == 0]
        if plain:
            if rule.interval == 1:
                return "every " + _join(plain)
            return f"{every}weeks on " + _join(plain)
        return "weekly" if rule.interval == 1 else f"every {rule.interval} weeks"

    if rule.freq == MONTHLY:
        if rule.bymonthday:
            days = [_ordinal_day(v) for v in rule.bymonthday]
            base = "monthly on " + _join(days)
            return base + clamp if rule.interval == 1 else f"{every}months on " + _join(days)
        ordinals = [(o, w) for o, w in rule.byday if o != 0]
        if ordinals:
            if len(ordinals) == 1 and ordinals[0][0] == -1:
                phrase = f"the last {_WEEKDAY_NAMES[ordinals[0][1]]} of the month"
            else:
                phrase = ", ".join(
                    f"{_ORDINALS.get(abs(o), f'{abs(o)}th')} {_WEEKDAY_NAMES[w]}"
                    for o, w in ordinals)
            return ("monthly on " + phrase) if rule.interval == 1 \
                else f"{every}months on {phrase}"
        plain = [_WEEKDAY_NAMES[w] for o, w in rule.byday if o == 0]
        if plain:
            return "every " + _join(plain) + " each month"
        if rule.bymonth:
            months = _join([_MONTHS[m - 1] for m in rule.bymonth])
            return f"{every}months, in {months}"
        return "monthly" if rule.interval == 1 else f"every {rule.interval} months"

    months = [_MONTHS[m - 1] for m in rule.bymonth] or []
    if rule.bymonthday:
        days = [_ordinal_day(v) for v in rule.bymonthday]
        where = (" in " + _join(months)) if months else ""
        return f"yearly on {_join(days)}{where}"
    if rule.byday:
        phrases = []
        for ordinal, weekday in rule.byday:
            if ordinal == -1:
                phrases.append(f"the last {_WEEKDAY_NAMES[weekday]}")
            elif ordinal == 0:
                phrases.append(_WEEKDAY_NAMES[weekday])
            else:
                phrases.append(f"the {_ORDINAL_WORDS.get(ordinal, ordinal)} "
                               f"{_WEEKDAY_NAMES[weekday]}")
        where = (" in " + _join(months)) if months else ""
        return f"yearly on {_join(phrases)}{where}"
    if months:
        return "yearly in " + _join(months)
    return "yearly" if rule.interval == 1 else f"every {rule.interval} years"


def _ordinal_day(value: int) -> str:
    if value == -1:
        return "the last day"
    if value < 0:
        return f"the {abs(value)}nd-last day"
    return f"day {value}"
