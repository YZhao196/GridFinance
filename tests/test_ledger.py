"""Hand-computed expectations for the canonical fixture.

October 2026, from the fixture:

  income    Salary 2026-10-15 3200.00  +  Freelance 10-02 / 10-16 / 10-30  1350.00  = 4550.00
  expenses  Rent 520 + Internet 79 + Spotify 12.99 + Gym 24 + Coffee 13x4.50 58.50
            + Savings 400 + Electricity 145 + Water 60                            = 1299.49
  net                                                                             = 3250.51
"""

from datetime import date, timedelta

import pytest

from finance_tool.engine import ledger as led
from finance_tool.engine import periods as per
from finance_tool.store.entities import Item, Transaction, EXPENSE, INCOME

from fixtures import TODAY, build_store, item_named

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def view(store):
    return store.view()


# ------------------------------------------------------------------- leaves and nodes


def test_a_category_node_is_not_an_occurrence(view):
    assert led.leaf_items(view.items)
    assert not led.is_leaf(view.items, view.item("d_utilities"))
    assert led.is_leaf(view.items, view.item("d_elec"))
    assert "d_utilities" not in {i.id for i in led.leaf_items(view.items)}


def test_including_the_node_would_have_added_a_phantom_zero_occurrence(view):
    """Why ``leaves_only`` defaults on: the node has a start and would fire once."""
    with_node = led.totals_in_range(view.items, date(2026, 1, 1), date(2026, 1, 31),
                                    leaves_only=False)
    without = led.totals_in_range(view.items, date(2026, 1, 1), date(2026, 1, 31))
    assert with_node.expense_count == without.expense_count + 1
    assert with_node.expenses == without.expenses          # the node has no amount
    assert with_node.count > without.count


def test_subtree_walks_children_at_any_depth(view):
    ids = {i.id for i in led.subtree(view.items, "d_utilities")}
    assert ids == {"d_elec", "d_water"}
    assert {i.id for i in led.subtree(view.items, "d_elec")} == set()


# ------------------------------------------------------------------------- totals


def test_october_plan_totals_match_the_hand_computation(view):
    totals = led.plan_totals(view, OCTOBER)

    assert totals.income == pytest.approx(4550.00)
    assert totals.expenses == pytest.approx(1299.49)
    assert totals.net == pytest.approx(3250.51)
    assert totals.income_count == 4
    assert totals.expense_count == 20
    assert totals.count == 24


def test_september_plan_totals_match_the_hand_computation(view):
    totals = led.plan_totals(view, SEPTEMBER)

    assert totals.income == pytest.approx(4100.00)
    assert totals.expenses == pytest.approx(1245.49)
    assert totals.net == pytest.approx(2854.51)
    assert totals.income_count == 3
    assert totals.expense_count == 18


def test_the_period_delta_is_arithmetically_consistent(view):
    october = led.plan_totals(view, OCTOBER)
    september = led.plan_totals(view, SEPTEMBER)
    delta = october.delta_from(september)

    assert delta.income == pytest.approx(450.00)
    assert delta.expenses == pytest.approx(54.00)
    assert delta.net == pytest.approx(396.00)
    assert delta.net == pytest.approx(delta.income - delta.expenses)


def test_a_cancelled_occurrence_is_excluded_from_the_total(view):
    """Magazine's 2026-10-20 occurrence is cancelled, so October misses its $15."""
    totals = led.plan_totals(view, OCTOBER)
    magazine = item_named(view, "Magazine")
    assert totals.expenses == pytest.approx(1299.49)
    assert date(2026, 10, 20) in magazine.cancelled


def test_an_override_is_reflected_in_the_period_it_belongs_to(view):
    """October -> November, and why: every difference is one explainable line."""
    november = led.plan_totals(view, (date(2026, 11, 1), date(2026, 11, 30))).expenses
    october = led.plan_totals(view, OCTOBER).expenses

    rent_rise = 25.00          # the 2026-11-01 override
    concert = 180.00           # a one-off, and November is when it lands
    magazine = 15.00           # cancelled in October, back in November
    water = -60.00             # quarterly, so October pays it and November does not

    assert november - october == pytest.approx(rent_rise + concert + magazine + water)


def test_open_ended_items_keep_firing_however_far_forward_you_look(view):
    """January 2030 is a normal month, not a void.

    Eight expenses, not October's twenty: the coffee run ended in December 2026 and the
    concert ticket was a one-off, which is exactly what a schedule with an ``end`` and a
    ``recurrence = null`` are for.
    """
    far = led.plan_totals(view, (date(2030, 1, 1), date(2030, 1, 31)))
    assert far.income == pytest.approx(4100.00)    # salary + two fortnightly invoices
    assert far.income_count == 3
    assert far.expense_count == 8
    assert far.expenses == pytest.approx(1255.99)
    assert [o.date for o in led.ledger_rows(view, (date(2030, 1, 1), date(2030, 1, 31)),
                                            kind=INCOME)] == [
        date(2030, 1, 4), date(2030, 1, 15), date(2030, 1, 18)]


def test_an_override_does_not_leak_into_other_occurrences(view):
    """One month's rent rise must not become every month's rent rise (§9)."""
    october = led.plan_totals(view, OCTOBER).expenses
    november = led.plan_totals(view, (date(2026, 11, 1), date(2026, 11, 30))).expenses
    december = led.plan_totals(view, (date(2026, 12, 1), date(2026, 12, 31))).expenses

    # Into November: rent +25, the concert lands +180, the magazine stops being
    # cancelled +15, and the quarterly water bill does not fall −60.
    assert november - october == pytest.approx(160.00)
    # Into December: rent is back to 520 and the concert is behind us.
    assert december - november == pytest.approx(-25.00 - 180.00)


def test_a_range_before_anything_started_totals_to_zero_rather_than_raising(view):
    totals = led.plan_totals(view, (date(2025, 1, 1), date(2025, 12, 31)))
    assert totals == led.EMPTY
    assert totals.net == 0.0
    assert led.EMPTY.savings_rate is None


def test_savings_rate_is_net_over_income_and_guarded(view):
    assert led.plan_totals(view, OCTOBER).savings_rate == pytest.approx(3250.51 / 4550.00)
    assert led.Totals(income=0, expenses=100).savings_rate is None


def test_totals_plus_and_delta():
    a = led.Totals(income=10, expenses=4, count=2, income_count=1, expense_count=1)
    b = led.Totals(income=1, expenses=2, count=3, income_count=2, expense_count=1)
    assert a.plus(b) == led.Totals(income=11, expenses=6, count=5, income_count=3, expense_count=2)
    assert a.delta_from(b) == led.Totals(income=9, expenses=2, count=-1,
                                         income_count=-1, expense_count=0)


def test_a_scoped_total_reads_one_subtree(view):
    utilities = led.subtree(view.items, "d_utilities")
    totals = led.plan_totals(view, OCTOBER, items=utilities)
    assert totals.expenses == pytest.approx(205.00)   # electricity 145 + water 60
    assert totals.income == 0.0


# ------------------------------------------------------------------- reconciled side


def test_october_actual_totals(view):
    actual = led.actual_totals(view, OCTOBER)
    assert actual.income == 0.0
    assert actual.expenses == pytest.approx(603.50)
    assert actual.count == 3


def test_september_actual_totals(view):
    actual = led.actual_totals(view, SEPTEMBER)
    assert actual.income == pytest.approx(3200.00)
    assert actual.expenses == pytest.approx(780.99)
    assert actual.count == 8


def test_a_transaction_without_a_date_is_skipped_not_crashed(view, store):
    store.add("transactions", Transaction(id="t_nodate", amount=-99.0))
    actual = led.actual_totals(store.view(), OCTOBER)
    assert actual.expenses == pytest.approx(603.50)


# ---------------------------------------------------------------------- ledger rows


def test_outgoing_rows_are_ordered_by_the_total_order(view):
    rows = led.ledger_rows(view, OCTOBER, kind=EXPENSE)
    assert len(rows) == 20
    keys = [(r.due, r.priority, r.name.casefold(), r.item_id) for r in rows]
    assert keys == sorted(keys)
    assert rows[0].name == "Rent"           # due 1 October
    assert rows[-1].name == "Gym"           # due 31 October


def test_incoming_rows(view):
    rows = led.ledger_rows(view, OCTOBER, kind=INCOME)
    assert [r.date for r in rows] == [
        date(2026, 10, 2), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 30)]


def test_cancelled_rows_are_available_when_asked_for(view):
    default = led.ledger_rows(view, OCTOBER, kind=EXPENSE)
    with_cancelled = led.ledger_rows(view, OCTOBER, kind=EXPENSE, include_cancelled=True)
    assert len(with_cancelled) == len(default) + 1
    assert any(r.cancelled for r in with_cancelled)


def test_ledger_groups_nest_children_under_their_node(view):
    groups = led.ledger_groups(view, OCTOBER, kind=EXPENSE)

    nested = next(g for g in groups if g.item is not None)
    assert nested.item.name == "Utilities"
    assert {o.name for o in nested.occurrences} == {"Electricity", "Water"}
    assert nested.totals.expenses == pytest.approx(205.00)
    assert nested.name == "Utilities"

    loose = [g for g in groups if g.item is None]
    assert len(loose) == 1
    assert sum(g.totals.expense_count for g in loose) == 18

    # The node leads, and the loose items follow in their own due order.
    assert groups[0].item is not None


def test_group_totals_sum_to_the_period_total(view):
    groups = led.ledger_groups(view, OCTOBER, kind=EXPENSE)
    assert sum(g.totals.expenses for g in groups) == pytest.approx(1299.49)


# ------------------------------------------------------------------- plan vs actual


def test_plan_summary_puts_the_plan_beside_the_actual(view):
    summary = led.plan_summary(view, OCTOBER)

    assert summary.plan.income == pytest.approx(4550.00)
    assert summary.plan.expenses == pytest.approx(1299.49)
    assert summary.actual.income == 0.0
    assert summary.actual.expenses == pytest.approx(603.50)
    assert summary.occurrences == 25          # 21 expense rows (incl. cancelled) + 4 income
    assert summary.settled == 1               # rent was marked paid
    assert summary.settled_ratio == pytest.approx(1 / 25)


def test_settled_ratio_is_none_rather_than_an_error_with_no_occurrences(view):
    summary = led.plan_summary(view, (date(2025, 1, 1), date(2025, 12, 31)))
    assert summary.occurrences == 0
    assert summary.settled_ratio is None


# ------------------------------------------------------------------------- budgets


def test_budget_vs_actual_by_category(view):
    rows = led.budget_vs_actual(view, OCTOBER)
    by_name = {row.category: row for row in rows}

    assert by_name["Rent"].planned == pytest.approx(520.00)
    assert by_name["Rent"].actual == pytest.approx(520.00)
    assert by_name["Utilities"].planned == pytest.approx(284.00)   # 79 + 145 + 60
    assert by_name["Utilities"].actual == pytest.approx(79.00)
    assert by_name["Subscriptions"].planned == pytest.approx(12.99)
    assert by_name["Subscriptions"].actual == 0.0
    assert by_name["Food"].planned == pytest.approx(58.50)
    assert by_name["Savings"].planned == pytest.approx(400.00)
    assert by_name["Health"].planned == pytest.approx(24.00)


def test_uncategorised_spend_gets_its_own_row(view):
    """§23: never quietly dropped from a budget-vs-actual total."""
    rows = led.budget_vs_actual(view, OCTOBER)
    uncategorised = next(r for r in rows if r.category == led.UNCATEGORISED)

    assert uncategorised.planned == 0.0
    assert uncategorised.actual == pytest.approx(4.50)
    assert uncategorised.on_track is False

    planned = sum(r.planned for r in rows)
    actual = sum(r.actual for r in rows)
    assert planned == pytest.approx(1299.49)
    assert actual == pytest.approx(603.50)


def test_budget_rows_are_ordered_by_planned_then_actual(view):
    rows = led.budget_vs_actual(view, OCTOBER)
    keys = [(-r.planned, -r.actual, r.category.casefold()) for r in rows]
    assert keys == sorted(keys)
    assert rows[0].category == "Rent"
    assert rows[-1].category == led.UNCATEGORISED


def test_budget_variance_and_progress(view):
    rows = {r.category: r for r in led.budget_vs_actual(view, OCTOBER)}
    rent = rows["Rent"]
    assert rent.variance == pytest.approx(0.0)
    assert rent.on_track is True
    assert rent.progress == pytest.approx(1.0)

    food = rows["Food"]
    assert food.variance == pytest.approx(58.50)
    assert food.progress == 0.0


def test_progress_is_guarded_against_a_zero_planned_amount(view):
    rows = {r.category: r for r in led.budget_vs_actual(view, OCTOBER)}
    assert rows[led.UNCATEGORISED].progress == 1.0
    assert led.BudgetRow(category="x", planned=0.0, actual=0.0).progress == 0.0


def test_budget_pace_is_prorated_to_today(view):
    """Mid-month, a category is judged against where it should be by now."""
    rows = {r.category: r for r in led.budget_vs_actual(view, OCTOBER, today=TODAY)}
    utilities = rows["Utilities"]

    assert utilities.pace == pytest.approx(284.00 * 7 / 31)
    assert utilities.pace_ratio == pytest.approx(79.00 / (284.00 * 7 / 31))
    assert utilities.over_pace is True


def test_pace_is_absent_for_a_period_that_has_ended(view):
    rows = led.budget_vs_actual(view, SEPTEMBER, today=TODAY)
    assert rows
    assert all(row.pace is None for row in rows)
    assert all(row.pace_ratio is None for row in rows)
    assert all(row.over_pace is False for row in rows)


def test_a_budget_view_with_no_actuals_is_still_a_plan(view):
    rows = led.budget_vs_actual(view, (date(2026, 11, 1), date(2026, 11, 30)))
    assert rows
    assert all(row.actual == 0.0 for row in rows)
    assert all(row.on_track for row in rows)
    assert {r.category for r in rows} >= {"Rent", "Fun", "Utilities"}


def test_budget_vs_actual_of_an_empty_store_is_empty_not_an_error():
    from finance_tool.store.entities import StoreDocument
    from finance_tool.store.store import Store

    empty = Store(doc=StoreDocument(), data_dir=None).view()
    assert led.budget_vs_actual(empty, OCTOBER) == []
    assert led.plan_totals(empty, OCTOBER) == led.EMPTY


# ------------------------------------------------------------------------ coverage


def test_coverage_is_measured_by_count_and_by_value(view):
    coverage = led.budget_coverage(view, OCTOBER)

    assert coverage.total_count == 3
    assert coverage.categorised_count == 2
    assert coverage.by_count == pytest.approx(2 / 3)
    assert coverage.total_value == pytest.approx(603.50)
    assert coverage.categorised_value == pytest.approx(599.00)
    assert coverage.by_value == pytest.approx(599.00 / 603.50)
    # The count bar fails even though the value bar is almost perfect — which is
    # exactly why §24 asks for both.
    assert coverage.by_value >= 0.80
    assert coverage.meets(0.80) is False


def test_coverage_over_the_whole_store(view):
    coverage = led.budget_coverage(view)
    assert coverage.total_count == 10          # expenses only
    assert coverage.categorised_count == 7
    assert coverage.by_count == pytest.approx(0.70)
    assert coverage.by_value == pytest.approx(1370.99 / 1384.49)


def test_coverage_of_nothing_is_complete_not_undefined(view):
    coverage = led.budget_coverage(view, (date(2025, 1, 1), date(2025, 12, 31)))
    assert coverage.by_count == 1.0
    assert coverage.by_value == 1.0
    assert coverage.meets() is True


def test_coverage_can_include_income(view):
    with_income = led.budget_coverage(view, expenses_only=False)
    assert with_income.total_count == 11
    assert with_income.categorised_count == 8


def test_uncategorised_transactions_are_listed_and_ordered(view):
    found = led.uncategorised_transactions(view, OCTOBER)
    assert [t.id for t in found] == ["t_11"]
    everything = led.uncategorised_transactions(view)
    assert [t.id for t in everything] == ["t_07", "t_08", "t_11"]


# --------------------------------------------------------------------------- bills


def test_bills_in_range_returns_unpaid_expense_occurrences_in_order(view):
    bills = led.bills_in_range(view, date(2026, 10, 1), date(2026, 10, 31))
    assert bills
    assert all(not b.paid for b in bills)
    assert all(b.type == EXPENSE for b in bills)
    assert [b.due for b in bills] == sorted(b.due for b in bills)


def test_bills_in_range_can_include_settled_ones(view):
    default = led.bills_in_range(view, date(2026, 10, 1), date(2026, 10, 31))
    with_paid = led.bills_in_range(view, date(2026, 10, 1), date(2026, 10, 31),
                                   include_paid=True)
    assert len(with_paid) == len(default) + 1      # rent, marked paid
    assert any(b.paid for b in with_paid)


def test_overdue_bills_finds_the_internet_bill_and_orders_by_due(view):
    overdue = led.overdue_bills(view, TODAY, lookback_days=20)
    assert len(overdue) == 13
    assert all(b.due < TODAY for b in overdue)
    assert all(not b.paid for b in overdue)
    assert [b.due for b in overdue] == sorted(b.due for b in overdue)
    assert any(b.name == "Internet" and b.date == date(2026, 10, 5) for b in overdue)


def test_overdue_bills_ignores_settled_rent(view):
    overdue = led.overdue_bills(view, TODAY, lookback_days=45)
    assert not any(b.name == "Rent" for b in overdue)


def test_due_soon_bills_uses_the_due_date_window(view):
    soon = led.due_soon_bills(view, TODAY, days=7)
    assert [b.date for b in soon] == [
        date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 12),
        date(2026, 10, 12), date(2026, 10, 14)]
    assert sum(b.amount for b in soon) == pytest.approx(30.99)


def test_next_due_is_the_soonest_unpaid_expense(view):
    found = led.next_due(view, TODAY)
    assert found is not None
    assert found.date == date(2026, 10, 7)
    assert found.name == "Coffee"


def test_due_summary_splits_overdue_due_soon_and_upcoming(view):
    bills = led.bills_in_range(view, TODAY - timedelta(days=20), TODAY + timedelta(days=10))
    summary = led.due_summary(bills, TODAY, soon_days=7)

    assert summary.overdue_count + summary.due_soon_count + summary.upcoming_count == len(bills)
    assert summary.overdue_count == 13
    assert summary.due_soon_count == 5
    assert summary.due_soon_amount == pytest.approx(30.99)
    assert summary.total_amount == pytest.approx(
        summary.overdue_amount + summary.due_soon_amount + summary.upcoming_amount)


def test_due_summary_of_nothing_is_all_zero():
    assert led.due_summary([], TODAY) == led.DueSummary()


# ------------------------------------------------------------------ subscriptions


def test_subscription_totals_counts_active_subscriptions_only(view):
    totals = led.subscription_totals(view, when=TODAY)

    assert totals.count == 4                                       # internet, gym, magazine, spotify
    assert totals.monthly == pytest.approx(130.99)                 # 79 + 24 + 15 + 12.99
    assert totals.yearly == pytest.approx(130.99 * 12)
    assert [i.name for i in totals.items] == ["Internet", "Gym", "Magazine", "Spotify"]


def test_a_subscription_cancelled_before_the_reference_date_stops_counting(view, store):
    item = store.view().item("d_gym")
    item.end = date(2026, 3, 31)
    totals = led.subscription_totals(store.view(), when=TODAY)
    assert totals.count == 3
    assert totals.monthly == pytest.approx(130.99 - 24.00)


def test_a_subscription_that_has_not_started_yet_does_not_count(view, store):
    item = store.view().item("d_gym")
    item.start = date(2027, 1, 1)
    totals = led.subscription_totals(store.view(), when=TODAY)
    assert "Gym" not in [i.name for i in totals.items]


def test_active_on_boundaries():
    item = Item(id="d_x", start=date(2026, 1, 1), end=date(2026, 6, 30))
    assert led.active_on(item, date(2026, 1, 1)) is True
    assert led.active_on(item, date(2026, 6, 30)) is True
    assert led.active_on(item, date(2026, 7, 1)) is False
    assert led.active_on(item, date(2025, 12, 31)) is False
    assert led.active_on(Item(id="d_y", start=date(2026, 1, 1)), date(2030, 1, 1)) is True


def test_monthly_plan_by_category(view):
    plan = led.monthly_plan_by_category(view.items)
    assert plan["Utilities"] == pytest.approx(79 + 145 + 60 / 3)
    assert plan["Rent"] == pytest.approx(520.00)
    assert plan["Food"] == pytest.approx(58.50)
    assert plan["Subscriptions"] == pytest.approx(12.99 + 15.00)


# --------------------------------------------------------------- income projection


def test_predicted_income_for_the_next_month(view):
    """November: salary on the 15th, and two fortnightly invoices either side of it."""
    projection = led.predicted_income(view, (date(2026, 11, 1), date(2026, 11, 30)))

    assert projection.totals.income == pytest.approx(4100.00)
    assert projection.totals.count == 3
    assert projection.is_empty is False
    assert [s.date for s in projection.sources] == [
        date(2026, 11, 13), date(2026, 11, 15), date(2026, 11, 27)]
    assert [s.name for s in projection.sources] == ["Freelance", "Salary", "Freelance"]


def test_predicted_income_of_a_quiet_month_is_empty_not_missing(view):
    projection = led.predicted_income(view, (date(2026, 10, 20), date(2026, 10, 21)))
    assert projection.totals.count == 0
    assert projection.is_empty is True
    assert projection.sources == []
