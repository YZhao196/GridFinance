"""Japan trip is linked to the $400/month savings transfer; Buffer is not linked.

From the fixture:

  monthly income    salary 3200 + freelance 450 x 52/12 / 2 = 3200 + 975 = 4175.00
  monthly expenses  520 + 79 + 12.99 + 24 + 640/12 + 58.50 + 15 + 400 + 145 + 60/3
                    = 1327.8233...
  monthly surplus   2847.1766...
"""

from datetime import date

import pytest

from finance_tool.engine import goals as gl
from finance_tool.store.entities import Goal, StoreDocument
from finance_tool.store.store import Store

from fixtures import TODAY, build_store

SURPLUS = 2847.1766666666666


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def view(store):
    return store.view()


def test_monthly_income_and_expenses_use_monthly_equivalents(view):
    assert gl.monthly_income(view) == pytest.approx(4175.00)
    assert gl.monthly_expenses(view) == pytest.approx(1327.8233333333333)
    assert gl.monthly_surplus(view) == pytest.approx(SURPLUS)


def test_a_one_off_does_not_enter_the_steady_state_surplus(view):
    """The concert ticket is $180 of real money and $0 of monthly surplus."""
    with_ticket = gl.monthly_expenses(view)
    concert = view.item("d_concert")
    assert concert.recurrence is None
    # If one-offs were annualised in, dropping the ticket would move the number.
    assert gl.monthly_expenses(view) == pytest.approx(with_ticket)


def test_linked_monthly_reads_the_linked_items(view):
    assert gl.linked_monthly(view, view.goal("g_japan")) == pytest.approx(400.00)
    assert gl.linked_monthly(view, view.goal("g_buffer")) == 0.0


def test_contributions_give_linked_goals_their_own_money_and_share_the_rest(view):
    rates = gl.contributions(view)

    # Japan takes the savings transfer it is linked to.
    assert rates["g_japan"] == pytest.approx(400.00)
    # Buffer takes the whole surplus: the linked $400 is a *savings transfer*, already an
    # expense in the surplus, so subtracting it again would lose it from the plan.
    assert rates["g_buffer"] == pytest.approx(SURPLUS)
    assert sum(rates.values()) == pytest.approx(SURPLUS + 400.00)


def test_unlinked_goals_share_the_pool_in_proportion_to_what_they_need():
    store = Store(doc=StoreDocument(
        items=build_store().doc.items,
        goals=[
            Goal(id="g_a", name="A", target=1000.0, saved=0.0),
            Goal(id="g_b", name="B", target=3000.0, saved=0.0),
        ],
    ), data_dir=None)

    pool = SURPLUS          # nothing linked, so the whole surplus is available
    rates = gl.contributions(store.view())

    # A needs 1000 of the 4000 total need, so it takes a quarter.
    assert rates["g_a"] == pytest.approx(pool * 0.25)
    assert rates["g_b"] == pytest.approx(pool * 0.75)
    assert sum(rates.values()) == pytest.approx(pool)


def test_a_deficit_plan_funds_no_unlinked_goal(view, store):
    store.view().item("d_concert").amount = 0.0
    store.add("goals", Goal(id="g_x", name="X", target=1000.0, saved=0.0))
    # Push the plan into deficit with one enormous recurring expense.
    from finance_tool.store.entities import Item, EXPENSE

    store.add_item(Item(id="d_huge", name="Huge", type=EXPENSE, amount=9000.0,
                        start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}))

    rates = gl.contributions(store.view())
    assert gl.monthly_surplus(store.view()) < 0
    assert rates["g_x"] == 0.0


def test_savings_rate_over_a_period(view):
    assert gl.savings_rate(view, (date(2026, 10, 1), date(2026, 10, 31))) == \
        pytest.approx(3250.51 / 4550.00)
    assert gl.savings_rate(view, (date(2025, 1, 1), date(2025, 12, 31))) is None


# ---------------------------------------------------------------------- projections


def test_a_linked_goal_completes_on_the_date_the_money_lands(view):
    projection = gl.goal_eta(view, view.goal("g_japan"), TODAY)

    assert projection.remaining == pytest.approx(4500.00)
    assert projection.monthly == pytest.approx(400.00)
    assert projection.months == 12                  # ceil(4500 / 400)
    # The 12th $400 transfer after today (2026-10-07) lands on 2027-10-02.
    assert projection.eta == date(2027, 10, 2)
    assert projection.progress == pytest.approx(0.25)
    assert projection.complete is False
    assert projection.on_track is True
    assert projection.linked == ["d_save"]


def test_an_unlinked_goal_completes_at_its_rate_of_the_surplus(view):
    projection = gl.goal_eta(view, view.goal("g_buffer"), TODAY)

    assert projection.remaining == pytest.approx(1750.00)
    assert projection.monthly == pytest.approx(SURPLUS)
    assert projection.months == 1                   # ceil(1750 / 2847.18)
    assert projection.eta == date(2026, 11, 7)
    assert projection.progress == pytest.approx(0.125)


def test_a_goal_asked_about_alone_agrees_with_the_full_projection(view):
    """One number, one behaviour — no second opinion from a shortcut."""
    alone = gl.goal_eta(view, view.goal("g_buffer"), TODAY)
    inside = next(p for p in gl.goal_projections(view, TODAY) if p.goal_id == "g_buffer")
    assert alone == inside


def test_projections_are_ordered_by_completion(view):
    projections = gl.goal_projections(view, TODAY)
    assert [p.name for p in projections] == ["Buffer", "Japan trip"]


def test_a_goal_with_no_rate_is_not_on_track_rather_than_wrong(view, store):
    store.add("goals", Goal(id="g_orphan", name="Orphan", target=1000.0, saved=0.0,
                        linked_def_ids=["d_missing"]))
    projection = gl.goal_eta(store.view(), store.view().goal("g_orphan"), TODAY)

    assert projection.monthly == 0.0
    assert projection.months is None
    assert projection.eta is None
    assert projection.on_track is False


def test_a_goal_linked_to_an_item_that_stops_short_is_honestly_unreachable(store):
    # The coffee run ends on 2026-12-31, which is nowhere near this target.
    store.add("goals", Goal(id="g_short", name="Short", target=5000.0, saved=0.0,
                            linked_def_ids=["d_coffee"]))
    goal = store.view().goal("g_short")

    projection = gl.goal_eta(store.view(), goal, TODAY)
    assert projection.monthly == pytest.approx(58.50)
    assert projection.months == 86                   # the rate says how many are needed
    assert projection.eta is None                    # the schedule says it never happens
    assert projection.on_track is False


def test_a_goal_funded_by_a_single_one_off_still_gets_a_date(store):
    """A one-off has a monthly equivalent of zero but a real completion date."""
    store.add("goals", Goal(id="g_once", name="Once", target=180.0, saved=0.0,
                            linked_def_ids=["d_concert"]))
    goal = store.view().goal("g_once")

    projection = gl.goal_eta(store.view(), goal, TODAY)
    assert projection.monthly == 0.0                 # a one-off is not a monthly cost
    assert projection.months == 1                    # but it is one contribution
    assert projection.eta == date(2026, 11, 14)
    assert projection.on_track is True


def test_a_met_goal_is_complete_and_dated_today(view, store):
    goal = view.goal("g_japan")
    goal.saved = 6000.0
    projection = gl.goal_eta(store.view(), goal, TODAY)

    assert projection.remaining == 0.0
    assert projection.complete is True
    assert projection.progress == 1.0
    assert projection.months == 0
    assert projection.eta == TODAY
    assert projection.on_track is True


def test_an_overfunded_goal_caps_its_progress(view, store):
    goal = view.goal("g_japan")
    goal.saved = 9000.0
    projection = gl.goal_eta(store.view(), goal, TODAY)
    assert projection.progress == 1.0
    assert projection.remaining == 0.0


def test_a_goal_with_a_zero_target_is_complete_not_a_division_error():
    store = Store(doc=StoreDocument(goals=[Goal(id="g_z", name="Zero", target=0.0,
                                                saved=0.0)]), data_dir=None)
    projection = gl.goal_eta(store.view(), store.view().goal("g_z"), TODAY)

    assert projection.progress == 1.0
    assert projection.complete is True
    assert projection.months == 0


def test_a_goal_with_no_target_but_savings_is_also_complete():
    store = Store(doc=StoreDocument(goals=[Goal(id="g_z", name="Zero", target=0.0,
                                                saved=50.0)]), data_dir=None)
    projection = gl.goal_eta(store.view(), store.view().goal("g_z"), TODAY)
    assert projection.progress == 1.0
    assert projection.complete is True


def test_no_goals_projects_to_nothing(view):
    store = Store(doc=StoreDocument(items=build_store().doc.items), data_dir=None)
    assert gl.goal_projections(store.view(), TODAY) == []
    assert gl.contributions(store.view()) == {}
    assert gl.savings_record(store.view(), TODAY).goals == 0


# ------------------------------------------------------------------- savings record


def test_savings_record(view):
    record = gl.savings_record(view, TODAY)

    assert record.total_saved == pytest.approx(1750.00)
    assert record.goals == 2
    assert record.complete == 0
    assert record.average_saved == pytest.approx(875.00)
    assert record.completion_ratio == 0.0
    # Both goals' rates together: the linked 400 plus the whole surplus for the other.
    assert record.monthly_rate == pytest.approx(SURPLUS + 400.00)


def test_goal_for_item_finds_the_linking_goal(view):
    assert [g.id for g in gl.goal_for_item(view, "d_save")] == ["g_japan"]
    assert gl.goal_for_item(view, "d_rent") == []
