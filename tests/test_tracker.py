from datetime import date, timedelta

import pytest

from finance_tool.engine import tracker as trk
from finance_tool.store.entities import TrackerItem

from fixtures import TODAY, build_store

# Coffee beans: 30 uses at $0.60 = $18.00, bought 36 days before TODAY = 2026-09-01.
BEANS = TrackerItem(id="k_beans", name="Coffee beans", quantity=30.0,
                    per_use_amount=0.60, purchased_on=date(2026, 9, 1))
BOUGHT_TODAY = TrackerItem(id="k_new", name="Fresh", quantity=10.0,
                           per_use_amount=2.0, purchased_on=TODAY)


def test_cost_per_use_is_the_declared_price():
    assert trk.tracker_cost_per_use(BEANS) == pytest.approx(0.60)


def test_total_is_every_use_at_the_per_use_price():
    assert trk.tracker_total(BEANS) == pytest.approx(18.00)


def test_days_elapsed():
    assert trk.tracker_days_elapsed(BEANS, TODAY) == 36
    assert trk.tracker_days_elapsed(BEANS, date(2026, 9, 1)) == 0


def test_a_future_purchase_date_does_not_produce_negative_days():
    future = TrackerItem(id="k_f", name="Future", quantity=1, per_use_amount=1.0,
                         purchased_on=TODAY + timedelta(days=5))
    assert trk.tracker_days_elapsed(future, TODAY) == 0


def test_cost_per_day_amortises_over_the_days_since_purchase():
    assert trk.tracker_cost_per_day(BEANS, TODAY) == pytest.approx(0.50)


def test_cost_per_year_is_the_daily_rate_over_a_year():
    assert trk.tracker_cost_per_year(BEANS, TODAY) == pytest.approx(182.50)


def test_on_the_day_of_purchase_cost_per_day_is_unknown_not_infinite():
    assert trk.tracker_cost_per_day(BOUGHT_TODAY, TODAY) is None
    assert trk.tracker_cost_per_year(BOUGHT_TODAY, TODAY) is None


def test_without_a_purchase_date_there_is_nothing_to_amortise():
    undated = TrackerItem(id="k_u", name="Undated", quantity=5, per_use_amount=3.0)
    assert trk.tracker_days_elapsed(undated, TODAY) is None
    assert trk.tracker_cost_per_day(undated, TODAY) is None
    assert trk.tracker_cost_per_use(undated) == pytest.approx(3.0)
    assert trk.tracker_total(undated) == pytest.approx(15.0)


def test_degenerate_values_are_zero_not_errors():
    empty = TrackerItem(id="k_z", name="Zero", quantity=0.0, per_use_amount=0.0)
    assert trk.tracker_total(empty) == 0.0
    assert trk.tracker_cost_per_use(empty) == 0.0


def test_tracker_costs_are_ordered_by_annual_cost():
    store = build_store()
    costs = trk.tracker_costs(store.doc.tracker, TODAY)
    assert [c.name for c in costs] == ["Coffee beans"]
    assert costs[0].per_year == pytest.approx(182.50)
    assert costs[0].total == pytest.approx(18.00)
    assert costs[0].per_use == pytest.approx(0.60)


def test_tracker_costs_orders_multiple_items_by_annual_cost():
    cheap = TrackerItem(id="k_c", name="Cheap", quantity=10, per_use_amount=0.5,
                        purchased_on=date(2025, 10, 7))
    dear = TrackerItem(id="k_d", name="Dear", quantity=10, per_use_amount=5.0,
                       purchased_on=date(2026, 9, 7))
    costs = trk.tracker_costs([cheap, dear], TODAY)
    assert [c.name for c in costs] == ["Dear", "Cheap"]


def test_total_and_annualised_spend():
    store = build_store()
    items = store.doc.tracker + [TrackerItem(id="k_u", name="Undated", quantity=1,
                                             per_use_amount=9.0)]
    assert trk.tracker_total_spend(items) == pytest.approx(18.00 + 9.00)
    # The undated item is omitted rather than treated as zero: an unmeasurable rate is
    # not the same as a zero one.
    assert trk.tracker_annualised_spend(items, TODAY) == pytest.approx(182.50)


def test_cost_falls_as_the_item_ages():
    later = TODAY + timedelta(days=36)
    assert trk.tracker_cost_per_year(BEANS, later) == pytest.approx(182.50 / 2)
