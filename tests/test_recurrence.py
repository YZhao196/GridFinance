from datetime import date, timedelta

import pytest

from finance_tool.engine import recurrence as rec
from finance_tool.store.entities import Item, EXPENSE, INCOME

from fixtures import TODAY, build_store, item_named


def item(**kwargs) -> Item:
    kwargs.setdefault("id", "d_x")
    kwargs.setdefault("name", "X")
    kwargs.setdefault("type", EXPENSE)
    return Item(**kwargs)


# ------------------------------------------------------------------ calendar helpers


def test_add_months_clamps_to_the_target_month():
    assert rec.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert rec.add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert rec.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap
    assert rec.add_months(date(2026, 1, 31), 12) == date(2027, 1, 31)
    assert rec.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert rec.add_months(date(2026, 5, 15), -1) == date(2026, 4, 15)


def test_months_between_counts_calendar_months():
    assert rec.months_between(date(2026, 1, 1), date(2026, 1, 31)) == 0
    assert rec.months_between(date(2026, 1, 1), date(2026, 4, 1)) == 3
    assert rec.months_between(date(2026, 1, 1), date(2025, 12, 1)) == -1


def test_clamp_day_is_the_named_month_end_rule():
    assert rec.clamp_day(2026, 2, 31) == date(2026, 2, 28)
    assert rec.clamp_day(2024, 2, 31) == date(2024, 2, 29)
    assert rec.clamp_day(2026, 4, 31) == date(2026, 4, 30)
    assert rec.clamp_day(2026, 1, 0) == date(2026, 1, 1)
    assert rec.clamp_day(2026, 1, -5) == date(2026, 1, 1)


# ---------------------------------------------------------------------------- once


def test_a_once_item_fires_on_its_start_date():
    defn = item(start=date(2026, 11, 14), recurrence=None)
    assert rec.occurrence_dates(defn, date(2026, 11, 1), date(2026, 11, 30)) == [date(2026, 11, 14)]
    assert rec.occurrence_dates(defn, date(2026, 12, 1), date(2026, 12, 31)) == []


def test_explicit_once_kind_behaves_like_no_recurrence():
    defn = item(start=date(2026, 11, 14), recurrence=None)
    assert rec.recurrence_kind(defn) == rec.ONCE
    assert rec.occurrence_dates(defn, date(2026, 11, 1), date(2026, 11, 30)) == [date(2026, 11, 14)]


def test_an_unrecognised_kind_is_treated_as_a_one_off_not_an_error():
    """§15.4: total, never raises."""
    defn = item(start=date(2026, 11, 14), recurrence={"rrule": "FORTNIGHTLY-ISH"})
    assert rec.recurrence_kind(defn) == rec.ONCE
    assert rec.occurrence_dates(defn, date(2026, 11, 1), date(2026, 11, 30)) == [date(2026, 11, 14)]


def test_a_once_item_with_no_start_fires_never():
    assert rec.occurrence_dates(item(recurrence=None), date(2026, 1, 1), date(2026, 12, 31)) == []


# -------------------------------------------------------------------------- monthly


def test_monthly_fires_on_the_named_day():
    defn = item(start=date(2026, 1, 15), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=15"})
    assert rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 12, 31)) == [
        date(2026, 9, 15), date(2026, 10, 15), date(2026, 11, 15), date(2026, 12, 15)]


def test_monthly_never_fires_before_its_start():
    defn = item(start=date(2026, 10, 15), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=15"})
    assert rec.occurrence_dates(defn, date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 10, 15), date(2026, 11, 15), date(2026, 12, 15)]


def test_monthly_stops_at_its_end():
    defn = item(start=date(2026, 1, 15), end=date(2026, 11, 30),
                recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=15"})
    assert rec.occurrence_dates(defn, date(2026, 1, 1), date(2027, 12, 31))[-1] == date(2026, 11, 15)
    assert len(rec.occurrence_dates(defn, date(2026, 1, 1), date(2027, 12, 31))) == 11


def test_monthly_on_the_31st_clamps_and_never_skips():
    """§37's first named risk: the rent is missing because February has no 31st."""
    defn = item(start=date(2026, 1, 31), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=31"})
    dates = rec.occurrence_dates(defn, date(2026, 1, 1), date(2026, 12, 31))

    assert len(dates) == 12, "every month must fire exactly once — never skipped"
    assert dates[1] == date(2026, 2, 28)
    assert dates[3] == date(2026, 4, 30)
    assert dates[10] == date(2026, 11, 30)
    assert dates[-1] == date(2026, 12, 31)
    # Never rolls into the following month.
    assert [d.month for d in dates] == list(range(1, 13))


def test_monthly_on_the_31st_clamps_to_the_29th_in_a_leap_february():
    defn = item(start=date(2024, 1, 31), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=31"})
    assert rec.occurrence_dates(defn, date(2024, 2, 1), date(2024, 2, 29)) == [date(2024, 2, 29)]


def test_monthly_with_a_day_the_month_never_has_still_fires_every_month():
    defn = item(start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=31"})
    assert len(rec.occurrence_dates(defn, date(2026, 1, 1), date(2026, 6, 30))) == 6


def test_monthly_falls_back_to_the_starts_own_day():
    defn = item(start=date(2026, 3, 9), recurrence={"rrule": "FREQ=MONTHLY"})
    assert rec.occurrence_dates(defn, date(2026, 3, 1), date(2026, 5, 31)) == [
        date(2026, 3, 9), date(2026, 4, 9), date(2026, 5, 9)]


# --------------------------------------------------------------------------- weekly


def test_weekly_fires_on_every_listed_weekday():
    """weekdays are Monday=0, matching ``date.weekday()``."""
    defn = item(start=date(2026, 9, 7), end=date(2026, 12, 31),
                recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR"})
    dates = rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 9, 30))

    assert dates[0] == date(2026, 9, 7)          # a Monday
    assert dates[-1] == date(2026, 9, 30)        # a Wednesday
    assert len(dates) == 11
    assert all(d.weekday() in (0, 2, 4) for d in dates)
    assert all(d >= defn.start for d in dates)


def test_a_weekly_dedupes_repeated_weekdays():
    defn = item(start=date(2026, 9, 7), recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,MO,MO"})
    assert rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 9, 30)) == [
        date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]


def test_a_weekly_with_no_weekdays_takes_the_start_days_weekday():
    """Changed when the closed set became RFC 5545.

    A bare ``FREQ=WEEKLY`` used to fire never, because "weekly" with no weekdays listed
    was treated as an incomplete instruction. RFC 5545 says the weekday comes from
    DTSTART, which is the more useful reading: the user still has to pick a start date,
    so the schedule is never ambiguous, and it cannot silently do nothing.
    """
    defn = item(start=date(2026, 9, 7), recurrence={"rrule": "FREQ=WEEKLY"})
    dates = rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 9, 30))

    assert date(2026, 9, 7).weekday() == 0        # the start date is a Monday
    assert dates == [date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21),
                     date(2026, 9, 28)]
    assert all(d.weekday() == 0 for d in dates)


def test_weekly_byday_is_read_as_weekday_codes():
    defn = item(start=date(2026, 9, 7), recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,WE"})
    assert rec.occurrence_dates(defn, date(2026, 9, 7), date(2026, 9, 13)) == [
        date(2026, 9, 7), date(2026, 9, 9)]


# ------------------------------------------------------------------------- interval


def test_interval_in_days():
    defn = item(start=date(2026, 9, 1), recurrence={"rrule": "FREQ=DAILY;INTERVAL=10"})
    assert rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 10, 10)) == [
        date(2026, 9, 1), date(2026, 9, 11), date(2026, 9, 21), date(2026, 10, 1)]


def test_interval_in_weeks_keeps_its_phase():
    defn = item(start=date(2026, 9, 4), recurrence={"rrule": "FREQ=WEEKLY;INTERVAL=2"})
    assert rec.occurrence_dates(defn, date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 2), date(2026, 10, 16), date(2026, 10, 30)]


def test_interval_in_months_is_anchored_not_iterative():
    """Repeatedly adding a month to a clamped date would drift to the 28th forever."""
    defn = item(start=date(2026, 1, 31), recurrence={"rrule": "FREQ=MONTHLY;INTERVAL=1"})
    dates = rec.occurrence_dates(defn, date(2026, 1, 1), date(2026, 12, 31))
    assert dates[1] == date(2026, 2, 28)
    assert dates[2] == date(2026, 3, 31)
    assert dates[-1] == date(2026, 12, 31)


def test_interval_in_months_with_a_step():
    defn = item(start=date(2026, 1, 25), recurrence={"rrule": "FREQ=MONTHLY;INTERVAL=3"})
    assert rec.occurrence_dates(defn, date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 1, 25), date(2026, 4, 25), date(2026, 7, 25), date(2026, 10, 25)]


def test_interval_in_months_reaches_past_the_range_start():
    defn = item(start=date(2026, 1, 25), recurrence={"rrule": "FREQ=MONTHLY;INTERVAL=3"})
    assert rec.occurrence_dates(defn, date(2026, 6, 1), date(2026, 12, 31)) == [
        date(2026, 7, 25), date(2026, 10, 25)]


def test_an_interval_of_zero_or_less_is_clamped_to_one():
    defn = item(start=date(2026, 9, 1), recurrence={"rrule": "FREQ=DAILY;INTERVAL=0"})
    assert rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 9, 3)) == [
        date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


def test_an_interval_before_its_anchor_starts_at_the_anchor():
    defn = item(start=date(2026, 9, 10), recurrence={"rrule": "FREQ=DAILY;INTERVAL=5"})
    assert rec.occurrence_dates(defn, date(2026, 9, 1), date(2026, 9, 20)) == [
        date(2026, 9, 10), date(2026, 9, 15), date(2026, 9, 20)]


# --------------------------------------------------------------------------- yearly


def test_yearly_fires_once_a_year():
    defn = item(start=date(2026, 3, 20), recurrence={"rrule": "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"})
    assert rec.occurrence_dates(defn, date(2026, 1, 1), date(2028, 12, 31)) == [
        date(2026, 3, 20), date(2027, 3, 20), date(2028, 3, 20)]


def test_yearly_on_the_29th_of_february_clamps_in_common_years():
    defn = item(start=date(2024, 2, 29), recurrence={"rrule": "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29"})
    assert rec.occurrence_dates(defn, date(2024, 1, 1), date(2028, 12, 31)) == [
        date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28),
        date(2027, 2, 28), date(2028, 2, 29)]


# ------------------------------------------------------------------ bounds and totals


def test_an_inverted_range_is_empty_rather_than_an_error():
    defn = item(start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.occurrence_dates(defn, date(2026, 12, 31), date(2026, 1, 1)) == []


def test_a_definition_that_ended_before_the_range_is_empty():
    defn = item(start=date(2026, 1, 1), end=date(2026, 3, 31),
                recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.occurrence_dates(defn, date(2026, 6, 1), date(2026, 12, 31)) == []


def test_the_range_is_inclusive_at_both_ends():
    defn = item(start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.occurrence_dates(defn, date(2026, 10, 1), date(2026, 10, 1)) == [date(2026, 10, 1)]


# ----------------------------------------------------------------------- cancellations


def test_a_cancelled_occurrence_is_absent_by_default():
    store = build_store()
    magazine = item_named(store, "Magazine")

    dates = rec.occurrence_dates(magazine, date(2026, 9, 1), date(2026, 10, 31))
    assert dates == [date(2026, 9, 20)]


def test_a_cancelled_occurrence_is_available_for_the_ledger_to_strike_through():
    store = build_store()
    magazine = item_named(store, "Magazine")

    dates = rec.occurrence_dates(magazine, date(2026, 9, 1), date(2026, 10, 31),
                                 include_cancelled=True)
    assert dates == [date(2026, 9, 20), date(2026, 10, 20)]

    marked = rec.effective(magazine, date(2026, 10, 20))
    assert marked.cancelled is True
    assert rec.effective(magazine, date(2026, 9, 20)).cancelled is False


def test_fires_in_and_count_in():
    store = build_store()
    gym = item_named(store, "Gym")
    assert rec.fires_in(gym, date(2026, 10, 1), date(2026, 10, 31)) is True
    assert rec.count_in(gym, date(2026, 1, 1), date(2026, 12, 31)) == 12
    assert rec.count_in(gym, date(2026, 1, 1), date(2026, 12, 31),
                        include_cancelled=True) == 12


# ---------------------------------------------------------------------------- effective


def test_effective_applies_an_amount_override():
    store = build_store()
    rent = item_named(store, "Rent")

    assert rec.effective(rent, date(2026, 10, 1)).amount == 520.0
    assert rec.effective(rent, date(2026, 11, 1)).amount == 545.0
    # The definition itself is untouched — §9's whole point.
    assert rent.amount == 520.0


def test_effective_carries_paid_state_per_occurrence():
    store = build_store()
    rent = item_named(store, "Rent")

    assert rec.effective(rent, date(2026, 9, 1)).paid is True
    assert rec.effective(rent, date(2026, 10, 1)).paid is True
    assert rec.effective(rent, date(2026, 11, 1)).paid is False


def test_effective_derives_a_due_date_from_a_day_of_month():
    store = build_store()
    net = item_named(store, "Internet")
    assert rec.effective(net, date(2026, 10, 5)).due == date(2026, 10, 5)


def test_effective_derives_a_due_date_later_in_the_month():
    store = build_store()
    elec = item_named(store, "Electricity")
    assert rec.effective(elec, date(2026, 10, 18)).due == date(2026, 10, 18)


def test_a_due_day_shorter_than_the_month_clamps():
    defn = item(start=date(2026, 2, 1), due="31", recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.effective(defn, date(2026, 2, 1)).due == date(2026, 2, 28)


def test_a_due_spec_may_be_an_iso_date():
    defn = item(start=date(2026, 2, 1), due="2026-03-15", recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.effective(defn, date(2026, 2, 1)).due == date(2026, 3, 15)


def test_a_nonsense_due_spec_falls_back_to_the_occurrence_date():
    defn = item(start=date(2026, 2, 1), due="whenever", recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.effective(defn, date(2026, 2, 1)).due == date(2026, 2, 1)


def test_no_due_spec_means_due_when_it_lands():
    defn = item(start=date(2026, 2, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.effective(defn, date(2026, 2, 1)).due == date(2026, 2, 1)


def test_effective_applies_a_note_override_without_touching_the_definition():
    store = build_store()
    store.set_override("d_net", date(2026, 11, 5), "note", "plan change pending")
    net = store.view().item("d_net")

    assert rec.effective(net, date(2026, 11, 5)).note == "plan change pending"
    assert rec.effective(net, date(2026, 12, 5)).note == ""
    assert net.note == ""


def test_signed_amount_follows_the_type_not_the_stored_sign():
    store = build_store()
    salary = rec.effective(item_named(store, "Salary"), date(2026, 10, 15))
    rent = rec.effective(item_named(store, "Rent"), date(2026, 10, 1))
    assert salary.signed_amount == 3200.0
    assert rent.signed_amount == -520.0


def test_overdue_and_due_soon():
    store = build_store()
    net = item_named(store, "Internet")

    overdue = rec.effective(net, date(2026, 10, 5))
    assert overdue.is_overdue(TODAY) is True
    assert overdue.is_overdue(date(2026, 10, 4)) is False

    elec = item_named(store, "Electricity")
    upcoming = rec.effective(elec, date(2026, 10, 18))
    assert upcoming.is_due_soon(date(2026, 10, 14)) is True
    assert upcoming.is_due_soon(date(2026, 10, 1)) is False
    assert upcoming.is_overdue(date(2026, 10, 14)) is False


def test_a_cancelled_occurrence_is_never_overdue():
    store = build_store()
    magazine = item_named(store, "Magazine")
    cancelled = rec.effective(magazine, date(2026, 10, 20))
    assert cancelled.is_overdue(date(2026, 11, 1)) is False


# ---------------------------------------------------------------------------- ordering


def test_occurrences_are_ordered_by_due_then_priority_then_name():
    """§11: a total order, so lists never shuffle between renders."""
    store = build_store()
    defns = [item_named(store, name) for name in ("Rent", "Internet", "Spotify", "Gym")]

    found = rec.all_occurrences(defns, date(2026, 10, 1), date(2026, 10, 31))

    keys = [rec.sort_key(o) for o in found]
    assert keys == sorted(keys)
    # All four are monthly with the same priority, so the order is purely by due date.
    assert [o.name for o in found] == ["Rent", "Internet", "Spotify", "Gym"]
    assert [o.date.day for o in found] == [1, 5, 12, 31]


def test_the_order_is_total_even_for_identical_names_and_dates():
    a = item(id="d_a", name="Twin", start=date(2026, 10, 1),
             recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    b = item(id="d_b", name="Twin", start=date(2026, 10, 1),
             recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})

    first = rec.all_occurrences([a, b], date(2026, 10, 1), date(2026, 10, 31))
    second = rec.all_occurrences([b, a], date(2026, 10, 1), date(2026, 10, 31))

    assert [o.item_id for o in first] == [o.item_id for o in second] == ["d_a", "d_b"]


# ------------------------------------------------------------------------------ next


def test_next_occurrence_of_a_monthly_item():
    store = build_store()
    net = item_named(store, "Internet")
    assert rec.next_occurrence(net, date(2026, 10, 5)) == date(2026, 11, 5)
    assert rec.next_occurrence(net, date(2026, 10, 5), inclusive=True) == date(2026, 10, 5)
    assert rec.next_occurrence(net, date(2026, 10, 6)) == date(2026, 11, 5)


def test_next_occurrence_skips_a_cancelled_occurrence():
    store = build_store()
    magazine = item_named(store, "Magazine")
    assert rec.next_occurrence(magazine, date(2026, 10, 1)) == date(2026, 11, 20)


def test_next_occurrence_honours_the_end_date():
    store = build_store()
    coffee = item_named(store, "Coffee")
    assert rec.next_occurrence(coffee, date(2026, 12, 25)) == date(2026, 12, 28)
    assert rec.next_occurrence(coffee, date(2026, 12, 31)) is None
    assert rec.next_occurrence(coffee, date(2027, 1, 1)) is None


def test_next_occurrence_of_a_once_item():
    store = build_store()
    concert = item_named(store, "Concert ticket")
    assert rec.next_occurrence(concert, date(2026, 10, 7)) == date(2026, 11, 14)
    assert rec.next_occurrence(concert, date(2026, 11, 14)) is None
    assert rec.next_occurrence(concert, date(2026, 11, 14), inclusive=True) == date(2026, 11, 14)
    assert rec.next_occurrence(concert, date(2026, 12, 1)) is None


def test_next_occurrence_of_a_yearly_item():
    store = build_store()
    insurance = item_named(store, "Car insurance")
    assert rec.next_occurrence(insurance, date(2026, 6, 1)) == date(2027, 3, 20)
    assert rec.next_occurrence(insurance, date(2026, 1, 1)) == date(2026, 3, 20)


def test_next_occurrence_of_a_weekly_item():
    store = build_store()
    coffee = item_named(store, "Coffee")
    assert rec.next_occurrence(coffee, date(2026, 10, 7)) == date(2026, 10, 9)  # Friday


def test_next_occurrence_of_an_interval_item():
    store = build_store()
    freelance = item_named(store, "Freelance")
    assert rec.next_occurrence(freelance, date(2026, 10, 2)) == date(2026, 10, 16)


def test_next_occurrence_of_a_monthly_item_on_the_31st_never_returns_none():
    defn = item(start=date(2026, 1, 31), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=31"})
    for after in (date(2026, 2, 28), date(2026, 4, 30), date(2026, 6, 30)):
        assert rec.next_occurrence(defn, after) is not None


def test_next_occurrence_of_a_long_interval_terminates():
    defn = item(start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;INTERVAL=18"})
    assert rec.next_occurrence(defn, date(2026, 2, 1)) == date(2027, 7, 1)


def test_next_occurrence_of_an_ended_item_is_none():
    defn = item(start=date(2026, 1, 1), end=date(2026, 1, 31),
                recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.next_occurrence(defn, date(2026, 2, 1)) is None


def test_next_occurrence_of_many_picks_the_soonest():
    store = build_store()
    defns = [item_named(store, n) for n in ("Rent", "Internet", "Gym", "Spotify")]
    found = rec.next_occurrence_of(defns, TODAY)
    assert found is not None
    assert found.date == min(
        rec.next_occurrence(d, TODAY) for d in defns
        if rec.next_occurrence(d, TODAY) is not None)


def test_next_occurrence_of_nothing_is_none():
    assert rec.next_occurrence_of([], TODAY) is None


# --------------------------------------------------------------- monthly equivalence


def test_monthly_equivalence_for_every_kind():
    assert rec.monthly_equiv(item(start=date(2026, 1, 1), amount=520.0,
                                  recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})) == 520.0
    assert rec.monthly_equiv(item(start=date(2026, 1, 1), amount=640.0,
                                  recurrence={"rrule": "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"})) == \
        pytest.approx(640 / 12)
    assert rec.monthly_equiv(item(start=date(2026, 1, 1), amount=450.0,
                                  recurrence={"rrule": "FREQ=WEEKLY;INTERVAL=2"})) == \
        pytest.approx(975.0)
    assert rec.monthly_equiv(item(start=date(2026, 1, 1), amount=60.0,
                                  recurrence={"rrule": "FREQ=MONTHLY;INTERVAL=3"})) == \
        pytest.approx(20.0)
    assert rec.monthly_equiv(item(start=date(2026, 1, 1), amount=180.0,
                                  recurrence=None)) == 0.0


def test_a_weekly_item_multiplies_by_its_weekdays():
    defn = item(start=date(2026, 1, 1), amount=4.50,
                recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR"})
    assert rec.monthly_equiv(defn) == pytest.approx(58.5)
    assert rec.yearly_equiv(defn) == pytest.approx(702.0)


def test_daily_interval_monthly_equivalence():
    defn = item(start=date(2026, 1, 1), amount=1.0,
                recurrence={"rrule": "FREQ=DAILY;INTERVAL=1"})
    assert rec.monthly_equiv(defn) == pytest.approx(365.25 / 12)


def test_monthly_equiv_accepts_an_override_amount():
    defn = item(start=date(2026, 1, 1), amount=520.0, recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"})
    assert rec.monthly_equiv(defn, amount=545.0) == 545.0


# ------------------------------------------------------------------ fixture-wide checks


def test_the_fixtures_own_schedules_match_the_hand_computed_calendar():
    store = build_store()
    year = (date(2026, 1, 1), date(2026, 12, 31))

    gym = rec.occurrence_dates(item_named(store, "Gym"), *year)
    assert len(gym) == 12 and gym[1] == date(2026, 2, 28)

    water = rec.occurrence_dates(item_named(store, "Water"), *year)
    assert water == [date(2026, 1, 25), date(2026, 4, 25), date(2026, 7, 25), date(2026, 10, 25)]

    insurance = rec.occurrence_dates(item_named(store, "Car insurance"), *year)
    assert insurance == [date(2026, 3, 20)]

    freelance = rec.occurrence_dates(item_named(store, "Freelance"),
                                     date(2026, 10, 1), date(2026, 10, 31))
    assert freelance == [date(2026, 10, 2), date(2026, 10, 16), date(2026, 10, 30)]

    coffee = rec.occurrence_dates(item_named(store, "Coffee"),
                                  date(2026, 9, 1), date(2026, 9, 30))
    assert len(coffee) == 11
    assert coffee[0] == date(2026, 9, 7)


def test_every_fixture_item_generates_without_raising_over_a_wide_range():
    """§15.4: total across the whole fixture, whatever the schedule."""
    store = build_store()
    wide = (date(2020, 1, 1), date(2030, 12, 31))
    for defn in store.doc.items:
        dates = rec.occurrence_dates(defn, *wide, include_cancelled=True)
        assert dates == sorted(dates)
        assert all(isinstance(d, date) for d in dates)
