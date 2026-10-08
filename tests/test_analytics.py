from datetime import date

import pytest

from finance_tool.engine import analytics as an
from finance_tool.engine import periods as per
from finance_tool.store.entities import Item, StoreDocument, EXPENSE
from finance_tool.store.store import Store

from fixtures import TODAY, build_store

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
NOVEMBER = (date(2026, 11, 1), date(2026, 11, 30))
SEP_TO_OCT = (date(2026, 9, 1), date(2026, 10, 31))

# September: planned net 2854.51, actual net 2419.01 -> the plan was 435.50 optimistic.
# October:   planned net 3250.51, actual net -603.50 -> 3854.01 optimistic.
SEP_PLAN_NET = 2854.51
SEP_ACTUAL_NET = 2419.01
OCT_PLAN_NET = 3250.51
OCT_ACTUAL_NET = -603.50


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def view(store):
    return store.view()


# --------------------------------------------------------------------------- history


def test_history_pairs_plan_and_actual_for_the_same_span(view):
    points = an.history(view, *SEP_TO_OCT, lens=per.MONTH)
    assert len(points) == 2

    september, october = points
    assert september.label == "September 2026"
    assert september.plan.net == pytest.approx(SEP_PLAN_NET)
    assert september.actual.net == pytest.approx(SEP_ACTUAL_NET)
    assert october.plan.net == pytest.approx(OCT_PLAN_NET)
    assert october.actual.net == pytest.approx(OCT_ACTUAL_NET)


def test_history_deviation_is_actual_minus_plan(view):
    september, october = an.history(view, *SEP_TO_OCT)
    assert september.deviation == pytest.approx(-435.50)
    assert october.deviation == pytest.approx(-3854.01)


def test_history_has_actual_flags_a_month_with_no_transactions(view):
    points = an.history(view, date(2026, 1, 1), date(2026, 12, 31))
    by_label = {p.label: p for p in points}
    assert by_label["September 2026"].has_actual is True
    assert by_label["October 2026"].has_actual is True
    assert by_label["July 2026"].has_actual is False
    assert by_label["July 2026"].plan.count > 0


def test_history_can_walk_weeks_and_quarters(view):
    weeks = an.history(view, *OCTOBER, lens=per.WEEK)
    assert len(weeks) == 5
    # Clipped weekly bars tile October exactly.
    assert weeks[0].span == (date(2026, 10, 1), date(2026, 10, 4))
    assert sum(p.plan.expense_count for p in weeks) == 20

    quarters = an.history(view, date(2026, 1, 1), date(2026, 12, 31), lens=per.QUARTER)
    assert [p.label for p in quarters] == ["Q1 2026", "Q2 2026", "Q3 2026", "Q4 2026"]


def test_history_before_the_data_started_is_zero_filled_not_empty(view):
    """Twelve real points at zero: a month with no plan is a fact, not a missing month."""
    points = an.history(view, date(2025, 1, 1), date(2025, 12, 31))
    assert len(points) == 12
    assert all(p.plan.count == 0 for p in points)
    assert all(p.net_worth is None for p in points)


def test_history_attaches_the_latest_net_worth_reading(view):
    points = an.history(view, *SEP_TO_OCT)
    # Both months end on or after the 30 September snapshot, so both read 10400.
    assert points[0].net_worth == pytest.approx(10400.00)
    assert points[1].net_worth == pytest.approx(10400.00)

    july = an.history(view, date(2026, 7, 1), date(2026, 7, 31))[0]
    assert july.net_worth == pytest.approx(9100.00)        # the 31 July snapshot


def test_history_before_any_snapshot_has_no_net_worth(view):
    points = an.history(view, date(2026, 5, 1), date(2026, 5, 31))
    assert points[0].net_worth is None


# -------------------------------------------------------------------------- records


def test_records_identify_best_worst_and_growth(view):
    points = an.history(view, *SEP_TO_OCT)
    found = an.records(points)

    assert found.months == 2
    assert found.best.label == "October 2026"
    assert found.worst.label == "September 2026"
    assert found.months_positive == 2
    assert found.all_positive is True
    assert found.income_first == pytest.approx(4100.00)
    assert found.income_last == pytest.approx(4550.00)
    assert found.income_growth == pytest.approx(450.00)
    assert found.income_growth_ratio == pytest.approx(4550.00 / 4100.00)


def test_a_single_point_has_no_growth_to_report(view):
    points = an.history(view, *OCTOBER)
    found = an.records(points)
    assert found.months == 1
    assert found.income_growth is None
    assert found.income_growth_ratio is None
    assert found.best is found.worst


def test_records_of_nothing_are_empty(view):
    assert an.records([]) == an.Records()
    assert an.records([]).income_growth is None


def test_records_do_not_rank_for_colour(view):
    """§29: colour follows the sign, never the rank.

    The engine hands back both the best and the worst period and their signs; nothing
    here returns a rank the UI could paint a bad month with.
    """
    points = an.history(view, date(2026, 1, 1), date(2026, 12, 31))
    found = an.records(points)
    assert found.best is not None and found.worst is not None
    assert not hasattr(found.best, "rank")


def test_savings_record_over_a_year(view):
    points = an.history(view, date(2026, 1, 1), date(2026, 12, 31))
    record = an.savings_record(points)

    assert record.months == 12
    assert record.months_positive == 12
    assert record.positive_ratio == 1.0
    assert record.best > 0
    assert record.best_label
    assert record.total == pytest.approx(record.average * 12)


def test_savings_record_of_nothing_is_zero():
    record = an.savings_record([])
    assert record == an.SavingsRecord()
    assert record.positive_ratio is None


# -------------------------------------------------------------------------- metrics


def test_metrics_average_over_a_trailing_twelve_months(view):
    found = an.metrics(view, OCTOBER, months=12)

    assert found.months == 12
    # 2026-01 .. 2026-08 at 3200 each, then September 4100 and October 4550.
    assert found.avg_income == pytest.approx(34250.00 / 12)
    # Monthly expenses per the fixture's own schedule, summed over the window.
    # January is lighter than the rest: the internet plan does not start until February,
    # so only ten months of expenses fall inside the window.
    monthly_expenses = [1164.00, 1183.00, 1835.99, 1255.99, 1195.99,
                        1195.99, 1255.99, 1195.99, 1245.49, 1299.49]
    assert found.avg_expenses == pytest.approx(sum(monthly_expenses) / 12)
    assert found.avg_net == pytest.approx(found.avg_income - found.avg_expenses)
    assert found.avg_savings_rate == pytest.approx(found.avg_net / found.avg_income)


def test_the_trailing_window_starts_before_anything_existed(view):
    """Two of the twelve points predate every item, and that is the honest average."""
    found = an.metrics(view, OCTOBER, months=12)
    assert found.points[0].date == date(2025, 11, 1)
    assert found.points[0].plan.count == 0
    assert found.points[1].plan.count == 0
    assert found.points[2].plan.count > 0


def test_metrics_series_has_a_point_per_month(view):
    found = an.metrics(view, OCTOBER, months=6)
    income = found.series("income")
    assert len(income) == 6
    assert [d for d, _ in income][-1] == date(2026, 10, 1)
    assert income[-1][1] == pytest.approx(4550.00)
    assert found.actual_series("net")[-1][1] == pytest.approx(OCT_ACTUAL_NET)


def test_metrics_of_nothing_is_empty(view):
    found = an.metrics(view, (date(2025, 1, 1), date(2025, 1, 31)))
    # Even an empty window still spans months, all of them zero.
    assert found.months == 12
    assert found.avg_income == 0.0
    assert found.avg_savings_rate is None


# ------------------------------------------------------------------- period summary


def test_period_summary_pairs_the_period_with_the_one_before_it(view):
    summary = an.period_summary(view, OCTOBER)

    assert summary.label == "October 2026"
    assert summary.lens == "month"
    assert summary.plan.net == pytest.approx(OCT_PLAN_NET)
    assert summary.prev_plan.net == pytest.approx(SEP_PLAN_NET)
    assert summary.plan_delta.net == pytest.approx(OCT_PLAN_NET - SEP_PLAN_NET)
    assert summary.actual_net == pytest.approx(OCT_ACTUAL_NET)
    assert summary.prev_actual.net == pytest.approx(SEP_ACTUAL_NET)
    assert summary.actual_delta.net == pytest.approx(OCT_ACTUAL_NET - SEP_ACTUAL_NET)
    assert summary.savings_rate == pytest.approx(OCT_PLAN_NET / 4550.00)
    assert summary.has_actual is True


def test_the_summary_carries_a_five_month_sparkline(view):
    """§29 asks for a 5-month sparkline beside the hero figure, whatever the lens."""
    summary = an.period_summary(view, OCTOBER)
    assert len(summary.series) == 5
    assert summary.series[0][0] == date(2026, 6, 1)
    assert summary.series[-1] == (date(2026, 10, 1), pytest.approx(OCT_PLAN_NET))


def test_a_year_lens_summary_still_carries_a_five_month_sparkline(view):
    summary = an.period_summary(view, (date(2026, 1, 1), date(2026, 12, 31)), lens=per.YEAR)
    assert len(summary.series) == 5
    assert summary.window.months == 12


def test_a_summary_before_the_start_of_history_is_zero_not_missing(view):
    summary = an.period_summary(view, (date(2025, 3, 1), date(2025, 3, 31)))
    assert summary.plan.count == 0
    assert summary.actual.count == 0
    assert summary.has_actual is False
    assert summary.savings_rate is None


# ------------------------------------------------------------------------ forecast


def test_forecast_opens_on_the_liquid_balance_not_net_worth(view):
    """"Liquid" excludes nothing here because both AUD accounts are spendable."""
    assert an.liquid_balance(view) == pytest.approx(10400.00)
    assert an.net_worth(view) == pytest.approx(10400.00)


def test_a_foreign_currency_account_is_never_summed(view):
    """$500 USD is not $500 AUD, so it is flagged and left out (§22)."""
    store = build_store()
    store.view().account("a_usd").balance = 0.0   # zeroing it must change nothing
    assert an.net_worth(store.view()) == pytest.approx(10400.00)
    assert an.net_worth_series(store.view(), today=TODAY).foreign == ["US Checking"]


def test_forecast_rolls_the_plan_forward_one_month(view):
    found = an.forecast(view, NOVEMBER)

    assert found.opening == pytest.approx(10400.00)
    assert found.months == 1
    # November: income 4100, expenses 1459.49 (rent rises, the concert lands, no water).
    assert found.points[0].plan_net == pytest.approx(2640.51)
    assert found.final == pytest.approx(13040.51)
    assert found.first_negative is None
    assert found.is_flat is False


def test_forecast_over_two_months_compounds(view):
    found = an.forecast(view, (date(2026, 11, 1), date(2026, 12, 31)))
    assert found.points[0].balance == pytest.approx(13040.51)
    assert found.points[1].balance == pytest.approx(15886.02)
    assert found.points[1].label == "December 2026"


def test_a_six_month_forecast_is_six_points_and_still_rising(view):
    found = an.forecast(view, (date(2026, 11, 1), date(2027, 4, 30)))
    assert found.months == 6
    balances = [p.balance for p in found.points]
    assert balances == sorted(balances)
    assert balances[0] == pytest.approx(13040.51)
    assert balances[-1] == pytest.approx(27192.06)
    assert found.first_negative is None


def test_forecast_reports_the_month_the_balance_goes_under(store):
    store.add_item(Item(id="d_huge", name="Huge", type=EXPENSE, amount=20000.0,
                        start=date(2026, 11, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}))
    found = an.forecast(store.view(), NOVEMBER)

    assert found.final < 0
    assert found.first_negative == date(2026, 11, 30)


def test_a_point_forecast_has_no_band(view):
    found = an.forecast(view, NOVEMBER)
    assert found.is_banded is False
    assert found.points[0].spread == 0.0
    assert found.points[0].low == found.points[0].high == found.points[0].balance


def test_forecast_band_measures_the_plans_own_error(view):
    """Two months of actuals, both short of plan, set the width of the band."""
    band = an.forecast_band(view, (date(2026, 11, 1), date(2026, 12, 31)))

    # Sample standard deviation of [-435.50, -3854.01] around their mean.
    assert band.sigma == pytest.approx(2417.25, abs=0.5)
    assert band.is_banded is True


def test_the_band_widens_with_the_square_root_of_the_distance(view):
    band = an.forecast_band(view, (date(2026, 11, 1), date(2027, 2, 28)))
    spreads = [p.spread for p in band.points]

    assert spreads[0] == pytest.approx(band.sigma)
    assert spreads[1] == pytest.approx(band.sigma * 2 ** 0.5)
    assert spreads[2] == pytest.approx(band.sigma * 3 ** 0.5)
    assert spreads == sorted(spreads)


def test_the_band_stays_centred_on_the_plan(view):
    band = an.forecast_band(view, (date(2026, 11, 1), date(2026, 12, 31)))
    point = band.points[0]
    assert point.balance == pytest.approx((point.low + point.high) / 2)
    assert band.final_low < band.final < band.final_high


def test_with_no_actuals_the_band_falls_back_to_a_wide_guess():
    """A month with no reconciled history has nothing to measure, so it says so."""
    store = Store(doc=StoreDocument(items=build_store().doc.items), data_dir=None)
    band = an.forecast_band(store.view(), NOVEMBER)

    assert band.sigma == pytest.approx(0.15 * 1327.8233333333333)
    assert band.points[0].spread > 0


def test_forecasting_a_period_with_no_plan_leaves_the_balance_alone(view):
    found = an.forecast(view, (date(2025, 1, 1), date(2025, 1, 31)))
    assert found.months == 1
    assert found.points[0].plan_net == 0.0
    assert found.final == found.opening
    assert found.is_flat is False
    assert found.first_negative is None


# ----------------------------------------------------------------------- net worth


def test_net_worth_and_liquid_balance(view):
    assert an.net_worth(view) == pytest.approx(10400.00)
    assert an.liquid_balance(view) == pytest.approx(10400.00)


def test_a_non_liquid_account_counts_towards_net_worth_but_not_liquid_balance(view, store):
    store.view().account("a_savings").kind = "term-deposit"
    assert an.net_worth(store.view()) == pytest.approx(10400.00)
    assert an.liquid_balance(store.view()) == pytest.approx(2400.00)


def test_a_debt_is_an_account_with_a_negative_balance(view, store):
    from finance_tool.store.entities import Account

    store.add("accounts", Account(id="a_card", name="Credit card", kind="credit",
                                  balance=-650.00, currency="AUD"))
    assert an.net_worth(store.view()) == pytest.approx(10400.00 - 650.00)
    assert an.liquid_balance(store.view()) == pytest.approx(10400.00)


def test_net_worth_series_uses_snapshots_plus_the_live_reading(view):
    series = an.net_worth_series(view, today=TODAY)

    assert series.points == [
        (date(2026, 7, 31), 9100.00),
        (date(2026, 8, 31), 9950.00),
        (date(2026, 9, 30), 10400.00),
        (TODAY, 10400.00),
    ]
    assert series.current == pytest.approx(10400.00)
    assert series.change == pytest.approx(1300.00)
    assert series.has_series is True
    assert series.span == (date(2026, 7, 31), TODAY)


def test_a_series_with_one_reading_says_so(view, store):
    store.doc.snapshots.clear()
    series = an.net_worth_series(store.view(), today=TODAY)

    assert len(series.points) == 1
    assert series.has_series is False
    assert series.change is None
    assert series.current == pytest.approx(10400.00)


def test_net_worth_series_of_an_empty_store_is_a_single_zero():
    store = Store(doc=StoreDocument(), data_dir=None)
    series = an.net_worth_series(store.view(), today=TODAY)
    assert series.points == [(TODAY, 0.0)]
    assert series.has_series is False


def test_net_worth_series_can_be_windowed(view):
    series = an.net_worth_series(view, date(2026, 8, 1), date(2026, 9, 30), today=TODAY)
    assert [d for d, _ in series.points] == [date(2026, 8, 31), date(2026, 9, 30)]


# ------------------------------------------------------------------ cashflow links


def test_cashflow_links_from_the_plan(view):
    flow = an.cashflow_links(view, OCTOBER)

    assert flow.surplus == pytest.approx(3250.51)
    assert flow.sources[0] == ("Salary", pytest.approx(3200.00))
    assert ("Rent", pytest.approx(520.00)) in flow.targets
    assert flow.total_in == pytest.approx(4550.00)
    # Two income sources, six spending categories, plus the Savings target.
    assert len(flow.links) == 2 * 7
    assert flow.is_empty is False


def test_every_source_funds_the_whole_composition(view):
    """The stated convention: each source has the same makeup as the pool."""
    flow = an.cashflow_links(view, OCTOBER)
    salary = sum(link.amount for link in flow.links_from("Salary"))
    assert salary == pytest.approx(3200.00)

    rent_from_salary = next(l for l in flow.links_from("Salary") if l.target == "Rent")
    assert rent_from_salary.amount == pytest.approx(3200.00 * 520.00 / 4550.00)


def test_every_target_is_funded_by_the_whole_pool(view):
    flow = an.cashflow_links(view, OCTOBER)
    for target, amount in flow.targets:
        assert sum(link.amount for link in flow.links_to(target)) == pytest.approx(amount)


def test_the_surplus_appears_as_an_unallocated_target_so_the_flow_balances(view):
    """A Sankey that does not balance is not drawable, so the surplus is a target too."""
    flow = an.cashflow_links(view, OCTOBER)

    assert flow.targets[0] == (an.UNALLOCATED, pytest.approx(3250.51))
    assert flow.total_in == pytest.approx(flow.total_out) == pytest.approx(4550.00)
    assert sum(link.amount for link in flow.links) == pytest.approx(4550.00)
    # The plan's own Savings line is a separate target and keeps its own amount.
    assert ("Savings", pytest.approx(400.00)) in flow.targets


def test_a_deficit_appears_as_a_source_so_the_flow_still_balances(view):
    flow = an.cashflow_links(view, OCTOBER, basis="actual")
    assert flow.sources == [("Deficit", pytest.approx(603.50))]
    assert flow.total_in == pytest.approx(flow.total_out) == pytest.approx(603.50)
    assert flow.surplus == pytest.approx(-603.50)


def test_cashflow_links_from_the_actual(view):
    flow = an.cashflow_links(view, OCTOBER, basis="actual")

    assert flow.total_out == pytest.approx(603.50)
    assert [t[0] for t in flow.targets] == ["Rent", "Utilities", "Uncategorised"]
    # No income was reconciled this month, so the spending is funded by a named deficit
    # rather than by links that would not add up.
    assert [s[0] for s in flow.sources] == [an.DEFICIT]
    assert len(flow.links) == 3
    assert flow.surplus == pytest.approx(-603.50)


def test_cashflow_of_a_quiet_period_is_empty(view):
    flow = an.cashflow_links(view, (date(2025, 1, 1), date(2025, 1, 31)))
    assert flow.is_empty is True
    assert flow.links == []


def test_spend_by_category_and_tag(view):
    assert an.spend_by_category(view, OCTOBER) == [
        ("Rent", pytest.approx(520.00)),
        ("Utilities", pytest.approx(79.00)),
        ("Uncategorised", pytest.approx(4.50)),
    ]
    assert an.spend_by_category(view, OCTOBER, limit=1) == [("Rent", pytest.approx(520.00))]
    # No fixture transaction carries a tag, so there is nothing to break down.
    assert an.spend_by_tag(view, OCTOBER) == []


def test_spend_by_tag_splits_a_multi_tagged_transaction():
    store = build_store()
    store.view().transaction("t_11").tags = ["food", "work"]
    assert an.spend_by_tag(store.view(), OCTOBER) == [
        ("food", pytest.approx(2.25)),
        ("work", pytest.approx(2.25)),
    ]


def test_daily_spend_is_zero_filled_across_the_span(view):
    days = an.daily_spend(view, OCTOBER)

    assert len(days) == 31
    assert days[0] == (date(2026, 10, 1), pytest.approx(520.00))
    assert days[4] == (date(2026, 10, 5), pytest.approx(79.00))
    assert days[5] == (date(2026, 10, 6), pytest.approx(4.50))
    assert days[6] == (date(2026, 10, 7), 0.0)
    assert sum(amount for _, amount in days) == pytest.approx(603.50)


def test_month_over_month_returns_the_trailing_window(view):
    points = an.month_over_month(view, OCTOBER, periods=6)
    assert len(points) == 6
    assert points[0].label == "May 2026"
    assert points[-1].label == "October 2026"
