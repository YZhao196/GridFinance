"""RFC 5545 recurrence rules, and the one place we deliberately differ (§11).

The deviation is the whole reason this module exists in the shape it does: RFC 5545 gives
``BYMONTHDAY=31`` no February occurrence, and this app promises the 28th. Almost every test
below is here because that promise has to hold against a real calendar, not against a
reading of the spec.
"""

from datetime import date

import pytest

from finance_tool.engine import recurrence as rec
from finance_tool.engine import rrule
from finance_tool.engine.rrule import parse_rrule, serialise_rrule


def dates(text: str, start: date, lower: date, upper: date) -> list[date]:
    return parse_rrule(text).dates(start, lower, upper)


# ------------------------------------------------------------------------ clamping


def test_a_monthly_rule_on_the_31st_fires_twelve_times_a_year():
    """The deviation, stated as a test: RFC 5545 would give seven.

    This is §37's "gets it wrong once, loses trust permanently" — a March rent that
    vanishes every February is the failure this app refuses to have.
    """
    start = date(2026, 1, 31)
    found = dates("FREQ=MONTHLY;BYMONTHDAY=31", start, start, date(2026, 12, 31))

    assert len(found) == 12
    assert [d.month for d in found] == list(range(1, 13))
    assert found[1] == date(2026, 2, 28)          # February, clamped
    assert found[3] == date(2026, 4, 30)          # April, clamped
    assert found[2] == date(2026, 3, 31)          # March keeps its 31st


def test_the_31st_lands_on_the_29th_in_a_leap_february():
    start = date(2024, 1, 31)
    found = dates("FREQ=MONTHLY;BYMONTHDAY=31", start, start, date(2024, 2, 29))

    assert found == [date(2024, 1, 31), date(2024, 2, 29)]


def test_a_yearly_rule_on_february_29th_is_annual_not_quadrennial():
    """Clamping is what makes the rate exact: this fires every year, so it costs 1/12."""
    start = date(2026, 2, 28)
    found = dates("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", start, start, date(2032, 12, 31))

    assert len(found) == 7
    assert found[2] == date(2028, 2, 29)          # the leap year gets the real 29th
    assert found[0] == date(2026, 2, 28)
    assert parse_rrule("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29").rate_per_month() == \
        pytest.approx(1 / 12)


def test_a_negative_month_day_counts_back_from_the_end():
    start = date(2026, 1, 1)
    found = dates("FREQ=MONTHLY;BYMONTHDAY=-1", start, start, date(2026, 4, 30))
    assert found == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31),
                     date(2026, 4, 30)]


# ------------------------------------------------------------------------ weekdays


def test_an_ordinal_weekday_selects_the_nth_one_of_the_month():
    start = date(2026, 1, 1)
    found = dates("FREQ=MONTHLY;BYDAY=2TU", start, start, date(2026, 4, 30))

    assert found == [date(2026, 1, 13), date(2026, 2, 10), date(2026, 3, 10),
                     date(2026, 4, 14)]
    assert all(d.weekday() == 1 for d in found)


def test_the_last_weekday_of_the_month_counts_backwards():
    start = date(2026, 1, 1)
    found = dates("FREQ=MONTHLY;BYDAY=-1FR", start, start, date(2026, 3, 31))

    assert found == [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 27)]
    assert all(d.weekday() == 4 for d in found)


def test_an_ordinal_past_the_end_of_the_month_clamps_like_a_month_day():
    """The second half of the deviation, and the one we chose rather than inherited.

    RFC 5545 says a 5th Friday in a four-Friday month is no occurrence. That would make a
    bill disappear six or seven months a year for no reason the user could see, so it clamps
    to that month's last Friday instead — the same promise the 31st gets.
    """
    start = date(2026, 1, 1)
    found = dates("FREQ=MONTHLY;BYDAY=5FR", start, start, date(2026, 12, 31))

    assert len(found) == 12
    assert all(d.weekday() == 4 for d in found)
    january = [d for d in found if d.month == 1]
    assert january == [date(2026, 1, 30)]         # January 2026 has four Fridays


def test_a_bare_weekday_matches_every_one_of_them_in_the_month():
    start = date(2026, 1, 1)
    found = dates("FREQ=MONTHLY;BYDAY=MO", start, start, date(2026, 2, 28))
    assert len(found) == 8                        # four Mondays in each month
    assert all(d.weekday() == 0 for d in found)


def test_weekly_keeps_its_phase_from_the_start_date():
    """INTERVAL counts periods from DTSTART, so every second week stays the same week."""
    start = date(2026, 1, 1)                      # a Thursday
    rule = parse_rrule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH")
    found = rule.dates(start, start, date(2026, 2, 1))

    assert date(2026, 1, 1) in found              # the start day itself
    assert date(2026, 1, 12) in found             # a Monday, two weeks on
    assert date(2026, 1, 5) not in found          # the Monday of the week between


# --------------------------------------------------------------------- set positions


def test_bysetpos_picks_from_within_the_period():
    start = date(2026, 1, 1)
    found = dates("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1", start, start,
                  date(2026, 3, 31))
    assert found == [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31)]
    assert all(d.weekday() < 5 for d in found)


# --------------------------------------------------------------------------- bounds


def test_count_truncates_from_the_first_occurrence_not_from_the_range():
    rule = parse_rrule("FREQ=WEEKLY;BYDAY=MO;COUNT=5")
    found = rule.dates(date(2026, 1, 5), date(2026, 1, 1), date(2030, 1, 1))

    assert found == [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19),
                     date(2026, 1, 26), date(2026, 2, 2)]


def test_until_is_an_absolute_last_date():
    rule = parse_rrule("FREQ=DAILY;UNTIL=20260110")
    found = rule.dates(date(2026, 1, 1), date(2026, 1, 1), date(2030, 1, 1))
    assert found[-1] == date(2026, 1, 10)
    assert len(found) == 10


def test_a_rule_never_reports_a_date_outside_the_range_asked_for():
    """A week straddling the end of the range has days on both sides of it."""
    rule = parse_rrule("FREQ=WEEKLY;BYDAY=TH")
    found = rule.dates(date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 31))

    assert found == [date(2026, 1, 1), date(2026, 1, 8), date(2026, 1, 15),
                     date(2026, 1, 22), date(2026, 1, 29)]
    assert all(d <= date(2026, 1, 31) for d in found)


# -------------------------------------------------------------------- the invariants


RULES = (
    "FREQ=DAILY;INTERVAL=1", "FREQ=DAILY;INTERVAL=3", "FREQ=DAILY;BYDAY=MO,WE",
    "FREQ=WEEKLY;INTERVAL=1", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH",
    "FREQ=WEEKLY;BYDAY=MO,WE,FR", "FREQ=WEEKLY;INTERVAL=5;BYDAY=SU",
    "FREQ=MONTHLY;BYMONTHDAY=31", "FREQ=MONTHLY;BYMONTHDAY=15",
    "FREQ=MONTHLY;INTERVAL=3", "FREQ=MONTHLY;BYDAY=2TU", "FREQ=MONTHLY;BYDAY=-1FR",
    "FREQ=MONTHLY;BYMONTHDAY=-1", "FREQ=YEARLY", "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20",
    "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29", "FREQ=YEARLY;BYMONTH=1,7",
)
STARTS = (date(2026, 1, 1), date(2026, 1, 15), date(2026, 1, 31), date(2025, 12, 29),
          date(2026, 2, 28))


@pytest.mark.parametrize("text", RULES)
@pytest.mark.parametrize("start", STARTS)
def test_asking_for_a_slice_agrees_with_asking_for_everything(text, start):
    """The cheap way to skip to a range is what broke: a period index not divided by
    INTERVAL silently jumped over occurrences instead of starting at them."""
    rule = parse_rrule(text)
    everything = rule.dates(start, start, date(2030, 12, 31))

    for lower in (date(2026, 3, 1), date(2027, 6, 15), date(2028, 11, 30)):
        for upper in (date(2027, 1, 1), date(2029, 6, 30), date(2030, 12, 31)):
            expected = [d for d in everything if lower <= d <= upper]
            assert rule.dates(start, lower, upper) == expected


@pytest.mark.parametrize("text", RULES)
def test_dates_are_always_ascending_and_deduplicated(text):
    found = dates(text, date(2026, 1, 1), date(2026, 1, 1), date(2035, 12, 31))
    assert found == sorted(set(found))


RULE_SHAPES = (
    "FREQ=DAILY;INTERVAL=1", "FREQ=DAILY;INTERVAL=7", "FREQ=WEEKLY",
    "FREQ=WEEKLY;INTERVAL=2", "FREQ=WEEKLY;BYDAY=MO,TH", "FREQ=WEEKLY;BYDAY=MO,WE,FR",
    "FREQ=MONTHLY", "FREQ=MONTHLY;BYMONTHDAY=31", "FREQ=MONTHLY;BYMONTHDAY=1,15",
    "FREQ=MONTHLY;INTERVAL=3", "FREQ=MONTHLY;BYDAY=2TU", "FREQ=MONTHLY;BYDAY=-1FR",
    "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR", "FREQ=YEARLY", "FREQ=YEARLY;BYMONTH=6",
    "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20",
)


@pytest.mark.parametrize("text", RULE_SHAPES)
def test_the_monthly_rate_agrees_with_what_actually_fires(text):
    """The rollup and the scheduler must not disagree about the same plan.

    The rate is a property of the rule, so it must match a decade of real dates without
    being tuned to any particular window — a tolerance, not an equality, because a ten-year
    span is not a whole number of months or weeks.
    """
    rule = parse_rrule(text)
    start = date(2026, 1, 1)
    counted = len(rule.dates(start, start, date(2036, 1, 1))) / 120.0
    rate = rule.rate_per_month()

    assert rate == pytest.approx(counted, rel=0.03, abs=0.02)


# ------------------------------------------------------------------- parsing safely


def test_text_that_cannot_be_a_rule_returns_none_rather_than_raising():
    for junk in ("", None, "   ", "INTERVAL=2", "not a rule at all", "FORTNIGHTLY=1",
                 "RRULE:", ";;;"):
        assert parse_rrule(junk) is None


def test_a_pasted_rrule_line_is_accepted():
    assert parse_rrule("RRULE:FREQ=MONTHLY;BYMONTHDAY=5").bymonthday == (5,)


def test_parts_this_app_has_no_use_for_are_recorded_not_dropped():
    rule = parse_rrule("FREQ=DAILY;BYHOUR=9;BYMINUTE=30;BYSECOND=0")
    assert set(rule.ignored) >= {"BYHOUR", "BYMINUTE", "BYSECOND"}
    assert rule.freq == "DAILY"                   # still usable, just incomplete


def test_an_unrecognised_key_is_reported_too():
    assert "WOBBLE" in parse_rrule("FREQ=DAILY;WOBBLE=3").ignored


def test_a_malformed_selection_drops_that_part_and_says_so():
    rule = parse_rrule("FREQ=MONTHLY;BYDAY=NOTADAY")
    assert rule is not None and rule.freq == "MONTHLY"
    assert "BYDAY" in rule.ignored


def test_clamping_applies_to_a_day_filter_as_well_as_a_selection():
    """A consequence worth stating: "the 30th" means February 28 everywhere in this app.

    Under RFC 5545, ``BYMONTHDAY`` acts as a *filter* for a daily rule, so February would
    simply be excluded and nothing would fire. Once clamping is the rule for a day that
    does not exist, applying it inconsistently depending on which frequency the day is
    attached to would be the surprising thing.
    """
    found = dates("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30", date(2026, 1, 1),
                  date(2026, 1, 1), date(2026, 12, 31))
    assert found == [date(2026, 2, 28)]


def test_degenerate_input_returns_nothing_instead_of_raising():
    """§15.4: a query may never fail, whatever it is asked."""
    rule = parse_rrule("FREQ=MONTHLY;BYDAY=2TU")
    assert rule.dates(None, date(2026, 1, 1), date(2026, 12, 31)) == []
    assert rule.dates(date(2026, 1, 1), date(2026, 6, 1), date(2026, 1, 1)) == []
    assert rule.count_between(None, date(2026, 1, 1), date(2027, 1, 1)) == 0
    # A selection position that cannot exist in a one-element period.
    assert dates("FREQ=MONTHLY;BYDAY=MO;BYMONTHDAY=1;BYSETPOS=5", date(2026, 1, 1),
                 date(2026, 1, 1), date(2026, 12, 31)) == []
    # A rule whose next occurrence is centuries away terminates rather than searching for
    # it — the walk stops as soon as a period starts past the end of the range.
    assert dates("FREQ=YEARLY;INTERVAL=300", date(2026, 1, 1), date(2026, 1, 2),
                 date(2027, 1, 1)) == []


def test_the_rate_of_a_rule_with_no_frequency_is_zero():
    assert rrule.RRule().rate_per_month() == 0.0


# ------------------------------------------------------------------- round tripping


@pytest.mark.parametrize("text", RULE_SHAPES)
def test_a_rule_survives_being_written_and_read_back(text):
    first = parse_rrule(text)
    again = parse_rrule(serialise_rrule(first))
    assert (again.freq, again.interval, again.bymonth, again.bymonthday, again.byday,
            again.bysetpos) == (first.freq, first.interval, first.bymonth,
                                first.bymonthday, first.byday, first.bysetpos)


def test_a_rule_is_described_in_english():
    cases = {
        "FREQ=MONTHLY;BYDAY=2TU": "monthly on 2nd Tuesday",
        "FREQ=MONTHLY;BYDAY=-1FR": "monthly on the last Friday of the month",
        "FREQ=WEEKLY;BYDAY=MO,WE": "every Monday and Wednesday",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO": "every 2 weeks on Monday",
        "FREQ=DAILY": "daily",
        "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20": "yearly on day 20 in March",
    }
    for text, expected in cases.items():
        assert parse_rrule(text).describe() == expected


def test_the_description_says_where_a_short_month_will_land():
    """"day 31" alone hides the interesting part of the promise."""
    assert "last day" in parse_rrule("FREQ=MONTHLY;BYMONTHDAY=31").describe()
    assert "last day" not in parse_rrule("FREQ=MONTHLY;BYMONTHDAY=15").describe()


# ------------------------------------------------------------- through the facade


def test_the_engine_reads_a_rule_and_reports_its_kind():
    from finance_tool.store.entities import Item, EXPENSE

    defn = Item(id="d", name="x", type=EXPENSE, amount=1.0, start=date(2026, 1, 1),
                recurrence={"rrule": "FREQ=MONTHLY;BYDAY=2TU"})

    assert rec.recurrence_kind(defn) == "monthly"
    assert rec.rule_of(defn).byday == ((2, 1),)
    assert rec.count_in(defn, date(2026, 1, 1), date(2026, 3, 31)) == 3


def test_an_unreadable_rule_makes_an_item_a_one_off_rather_than_an_error():
    from finance_tool.store.entities import Item, EXPENSE

    defn = Item(id="d", name="x", type=EXPENSE, amount=1.0, start=date(2026, 3, 4),
                recurrence={"rrule": "TOTAL NONSENSE"})

    assert rec.recurrence_kind(defn) == "once"
    assert rec.occurrence_dates(defn, date(2026, 1, 1), date(2026, 12, 31)) == \
        [date(2026, 3, 4)]
    assert rec.monthly_equiv(defn) == 0.0
