from datetime import date, timedelta

import pytest

from finance_tool.engine import periods as per

# The fixture calendar: 2026-09-01 is a Tuesday, so October opens on a Thursday and
# 2026-10-05 and 2026-10-07 are a Monday and a Wednesday.
OCT_ANCHOR = date(2026, 10, 7)
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def test_the_calendar_anchors_the_tests_assume():
    assert date(2026, 9, 1).weekday() == 1     # Tuesday
    assert date(2026, 10, 1).weekday() == 3    # Thursday
    assert date(2026, 10, 5).weekday() == 0    # Monday
    assert date(2026, 10, 7).weekday() == 2    # Wednesday
    assert date(2025, 12, 29).weekday() == 0   # Monday


# ------------------------------------------------------------------ range_for_lens


@pytest.mark.parametrize("lens,expected", [
    (per.WEEK, (date(2026, 10, 5), date(2026, 10, 11))),
    (per.MONTH, (date(2026, 10, 1), date(2026, 10, 31))),
    (per.QUARTER, (date(2026, 10, 1), date(2026, 12, 31))),
    (per.YEAR, (date(2026, 1, 1), date(2026, 12, 31))),
])
def test_range_for_lens(lens, expected):
    assert per.range_for_lens(lens, OCT_ANCHOR) == expected


def test_a_month_range_ends_on_the_real_last_day():
    assert per.range_for_lens(per.MONTH, date(2026, 2, 10)) == (
        date(2026, 2, 1), date(2026, 2, 28))
    assert per.range_for_lens(per.MONTH, date(2024, 2, 10)) == (
        date(2024, 2, 1), date(2024, 2, 29))
    assert per.range_for_lens(per.MONTH, date(2026, 4, 10))[1] == date(2026, 4, 30)


def test_a_week_is_always_monday_to_sunday():
    for offset in range(14):
        start, end = per.range_for_lens(per.WEEK, date(2026, 10, 1) + timedelta(days=offset))
        assert start.weekday() == 0
        assert end.weekday() == 6
        assert (end - start).days == 6


def test_quarters_are_three_months_from_their_start():
    for month in range(1, 13):
        start, end = per.range_for_lens(per.QUARTER, date(2026, month, 15))
        assert (start.month - 1) % 3 == 0
        assert end.month == start.month + 2
        assert per.quarter_of(date(2026, month, 15)) == (start.month - 1) // 3 + 1


def test_an_unknown_lens_is_a_month_rather_than_an_error():
    assert per.normalise_lens("decade") == per.MONTH
    assert per.normalise_lens(None) == per.MONTH
    assert per.range_for_lens("decade", OCT_ANCHOR) == OCTOBER


# --------------------------------------------------------------------- step_lens


def test_step_lens_walks_backwards_and_forwards():
    assert per.step_lens(per.MONTH, OCT_ANCHOR, -1) == (date(2026, 9, 1), date(2026, 9, 30))
    assert per.step_lens(per.MONTH, OCT_ANCHOR, 1) == (date(2026, 11, 1), date(2026, 11, 30))
    assert per.step_lens(per.MONTH, OCT_ANCHOR, 0) == OCTOBER
    assert per.step_lens(per.QUARTER, OCT_ANCHOR, 1) == (date(2027, 1, 1), date(2027, 3, 31))
    assert per.step_lens(per.QUARTER, OCT_ANCHOR, -3) == (date(2026, 1, 1), date(2026, 3, 31))
    assert per.step_lens(per.YEAR, OCT_ANCHOR, -1) == (date(2025, 1, 1), date(2025, 12, 31))


def test_stepping_a_month_from_the_31st_clamps_and_lands_in_the_right_month():
    assert per.step_lens(per.MONTH, date(2026, 1, 31), 1) == (
        date(2026, 2, 1), date(2026, 2, 28))


def test_stepping_a_week_adds_seven_days():
    assert per.step_lens(per.WEEK, OCT_ANCHOR, 2) == (date(2026, 10, 19), date(2026, 10, 25))
    assert per.step_lens(per.WEEK, OCT_ANCHOR, -1) == (date(2026, 9, 28), date(2026, 10, 4))


def test_previous_and_next_range_are_inverses():
    for lens in per.LENSES:
        span = per.range_for_lens(lens, OCT_ANCHOR)
        assert per.next_range(lens, per.previous_range(lens, span)) == span
        assert per.previous_range(lens, per.previous_range(lens, span)) == \
            per.step_lens(lens, OCT_ANCHOR, -2)


def test_lens_containing_gives_the_three_period_neighbourhood():
    span = per.range_for_lens(per.MONTH, OCT_ANCHOR)
    previous, current, following = per.lens_containing(per.MONTH, span)
    assert current == span
    assert previous[0] == date(2026, 9, 1)
    assert following[0] == date(2026, 11, 1)


# ---------------------------------------------------------------- range arithmetic


def test_contains_and_days_in():
    assert per.contains(OCTOBER, date(2026, 10, 1)) is True
    assert per.contains(OCTOBER, date(2026, 10, 31)) is True
    assert per.contains(OCTOBER, date(2026, 11, 1)) is False
    assert per.days_in(OCTOBER) == 31
    assert per.days_in((date(2026, 2, 1), date(2026, 2, 28))) == 28


def test_clamp_range_returns_the_overlap_or_nothing():
    assert per.clamp_range(OCTOBER, date(2026, 10, 15), date(2026, 11, 30)) == (
        date(2026, 10, 15), date(2026, 10, 31))
    assert per.clamp_range(OCTOBER, date(2026, 11, 1), date(2026, 11, 30)) is None
    assert per.clamp_range(OCTOBER, date(2026, 1, 1), date(2027, 1, 1)) == OCTOBER
    assert per.clamp_range(OCTOBER, date(2026, 10, 7), date(2026, 10, 7)) == (
        date(2026, 10, 7), date(2026, 10, 7))


def test_day_count_elapsed_paces_a_period_and_never_goes_negative():
    """The budget-pace widget needs "how far through am I", clamped to the period."""
    assert per.day_count_elapsed(OCTOBER, date(2026, 10, 7)) == 7
    assert per.day_count_elapsed(OCTOBER, date(2026, 10, 1)) == 1
    assert per.day_count_elapsed(OCTOBER, date(2026, 9, 30)) == 0
    assert per.day_count_elapsed(OCTOBER, date(2026, 10, 31)) == 31
    assert per.day_count_elapsed(OCTOBER, date(2026, 11, 30)) == 31


# ------------------------------------------------------------------ month and week


def test_month_ranges_clips_at_both_ends():
    assert per.month_ranges(date(2026, 9, 15), date(2026, 11, 10)) == [
        (date(2026, 9, 15), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 10)),
    ]


def test_month_starts_gives_the_spine_of_the_history_views():
    assert per.month_starts(date(2026, 9, 15), date(2026, 11, 10)) == [
        date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]


def test_month_starts_covers_a_single_day():
    assert per.month_starts(date(2026, 10, 7), date(2026, 10, 7)) == [date(2026, 10, 1)]


def test_inverted_ranges_are_empty_not_errors():
    assert per.month_ranges(date(2026, 11, 1), date(2026, 10, 1)) == []
    assert per.month_starts(date(2026, 11, 1), date(2026, 10, 1)) == []
    assert per.week_ranges(date(2026, 11, 1), date(2026, 10, 1)) == []
    assert per.sub_ranges(per.MONTH, date(2026, 11, 1), date(2026, 10, 1)) == []
    assert list(per.each_day(date(2026, 11, 1), date(2026, 10, 1))) == []


def test_week_ranges_tile_the_period_exactly():
    """Summing the weekly bars of a month must give that month's total, no more."""
    weeks = per.week_ranges(*OCTOBER)
    assert len(weeks) == 5
    assert weeks[0] == (date(2026, 10, 1), date(2026, 10, 4))    # a four-day opening bar
    assert weeks[1] == (date(2026, 10, 5), date(2026, 10, 11))
    assert weeks[-1] == (date(2026, 10, 26), date(2026, 10, 31))
    assert sum(per.days_in(w) for w in weeks) == 31

    covered = [d for w in weeks for d in per.each_day(*w)]
    assert covered == list(per.each_day(*OCTOBER))


def test_week_ranges_of_a_whole_year_has_no_gaps_or_overlaps():
    weeks = per.week_ranges(date(2026, 1, 1), date(2026, 12, 31))
    assert sum(per.days_in(w) for w in weeks) == 365
    flat = [d for w in weeks for d in per.each_day(*w)]
    assert flat == sorted(flat)
    assert len(set(flat)) == 365


def test_sub_ranges_tiles_by_quarter_and_year():
    quarters = per.sub_ranges(per.QUARTER, date(2026, 1, 1), date(2026, 12, 31))
    assert quarters == [
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 12, 31)),
    ]

    years = per.sub_ranges(per.YEAR, date(2025, 6, 1), date(2027, 3, 1))
    assert years == [
        (date(2025, 6, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 3, 1)),
    ]


def test_sub_ranges_of_a_week_gives_one_entry_per_week():
    weeks = per.sub_ranges(per.WEEK, *OCTOBER)
    assert weeks == per.week_ranges(*OCTOBER)
    assert len(weeks) == 5
    assert weeks[0] == (date(2026, 10, 1), date(2026, 10, 4))


def test_sub_ranges_of_a_year_by_month_gives_twelve_bars():
    months = per.sub_ranges(per.MONTH, date(2026, 1, 1), date(2026, 12, 31))
    assert len(months) == 12
    assert months[0] == (date(2026, 1, 1), date(2026, 1, 31))
    assert months[-1] == (date(2026, 12, 1), date(2026, 12, 31))


def test_each_day_is_inclusive():
    days = list(per.each_day(date(2026, 10, 5), date(2026, 10, 8)))
    assert days == [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert list(per.each_day(date(2026, 10, 5), date(2026, 10, 5))) == [date(2026, 10, 5)]


# ------------------------------------------------------------------- month_grid


def test_month_grid_is_a_rectangle_padded_outside_the_month():
    grid = per.month_grid(2026, 10)
    assert len(grid) == 5
    assert all(len(week) == 7 for week in grid)
    assert grid[0] == [None, None, None, date(2026, 10, 1), date(2026, 10, 2),
                       date(2026, 10, 3), date(2026, 10, 4)]
    assert grid[-1][-1] is None  # 1 November, belonging to the next month


def test_month_grid_holds_every_day_of_the_month_exactly_once():
    grid = per.month_grid(2026, 10)
    present = [d for week in grid for d in week if d is not None]
    assert present == list(per.each_day(*OCTOBER))


def test_month_grid_of_a_month_that_begins_on_a_monday_needs_no_padding():
    # June 2026 opens on a Monday.
    assert date(2026, 6, 1).weekday() == 0
    assert per.month_grid(2026, 6)[0][0] == date(2026, 6, 1)


# ------------------------------------------------------------------------ labels


def test_labels_read_like_a_person_wrote_them():
    assert per.label_for(per.MONTH, OCTOBER) == "October 2026"
    assert per.label_for(per.QUARTER, (date(2026, 10, 1), date(2026, 12, 31))) == "Q4 2026"
    assert per.label_for(per.YEAR, (date(2026, 1, 1), date(2026, 12, 31))) == "2026"
    assert per.label_for(per.WEEK, (date(2026, 10, 5), date(2026, 10, 11))) == "5–11 Oct 2026"


def test_a_week_label_spanning_two_months_names_both():
    assert per.label_for(per.WEEK, (date(2026, 9, 28), date(2026, 10, 4))) == \
        "28 Sep – 4 Oct 2026"


def test_a_week_label_spanning_two_years_names_both():
    span = per.iso_week_range(date(2026, 1, 1))
    assert span == (date(2025, 12, 29), date(2026, 1, 4))
    assert per.label_for(per.WEEK, span) == "29 Dec 2025 – 4 Jan 2026"


def test_short_lens_labels():
    assert per.short_label_for_lens(per.WEEK) == "Week"
    assert per.short_label_for_lens("nonsense") == "Month"


# ----------------------------------------------------------------------- calendars


def test_iso_week_range_and_number():
    assert per.iso_week_range(date(2026, 10, 7)) == (date(2026, 10, 5), date(2026, 10, 11))
    assert per.iso_week_number(date(2026, 1, 1)) == 1


def test_add_months_and_month_bounds():
    assert per.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert per.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert per.month_start(date(2026, 10, 7)) == date(2026, 10, 1)
    assert per.month_end(date(2026, 10, 7)) == date(2026, 10, 31)
    assert per.quarter_start(date(2026, 5, 1)) == date(2026, 4, 1)
    assert per.year_start(date(2026, 5, 1)) == date(2026, 1, 1)
    assert per.year_end(date(2026, 5, 1)) == date(2026, 12, 31)


def test_every_lens_produces_a_consistent_span_across_a_decade():
    """A lens must never produce an inverted or empty-looking range."""
    cursor = date(2020, 1, 1)
    while cursor < date(2030, 1, 1):
        for lens in per.LENSES:
            start, end = per.range_for_lens(lens, cursor)
            assert start <= end
            assert per.contains((start, end), cursor)
            assert per.label_for(lens, (start, end))
        cursor += timedelta(days=17)
