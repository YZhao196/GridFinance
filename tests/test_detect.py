from datetime import date, timedelta

import pytest

from finance_tool.engine import detect as det
from finance_tool.store.entities import Transaction

from fixtures import TODAY, build_store

NETFLIX = "NETFLIX.COM 4482"


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def view(store):
    return store.view()


def add_transactions(store, description, dates, amount=-22.99, source="csv:x.csv"):
    for index, when in enumerate(dates):
        store.add("transactions", Transaction(
            id=f"t_x{index}", date=when, amount=amount, description=description,
            account="a_everyday", source=source))


# ---------------------------------------------------------------------- merchant_key


@pytest.mark.parametrize("description,expected", [
    ("CAFE LATTE", "CAFE LATTE"),
    ("cafe latte", "CAFE LATTE"),
    ("SPOTIFY P1234", "SPOTIFY"),
    ("RENT PAYMENT 03/10", "RENT"),
    ("SALARY ACME PTY", "SALARY ACME"),
    ("INTERNET PROVIDER", "INTERNET PROVIDER"),
    ("ELECTRICITY CO", "ELECTRICITY"),
    ("MAGAZINE SUB", "MAGAZINE"),
    ("NETFLIX.COM 4482", "NETFLIX"),
    ("AMAZON.COM.AU*MK12", "AMAZON"),
    ("WITHDRAWAL ATM 123456", ""),
    ("", ""),
    ("12345", ""),
])
def test_merchant_key_strips_the_incidental_and_keeps_the_identity(description, expected):
    assert det.merchant_key(description) == expected


def test_merchant_key_is_stable_across_the_noise_a_bank_adds():
    keys = {
        det.merchant_key("NETFLIX.COM 4482"),
        det.merchant_key("NETFLIX.COM 9911"),
        det.merchant_key("NETFLIX.COM 22/08"),
        det.merchant_key("NETFLIX.COM 22/09"),
    }
    assert keys == {"NETFLIX"}


def test_merchant_key_keeps_the_merchant_not_just_the_first_token():
    assert det.merchant_key("CAR INSURANCE") == "CAR INSURANCE"


def test_an_all_noise_description_yields_nothing_rather_than_a_junk_key():
    """Grouping every unreadable line together would invent a merchant."""
    assert det.merchant_key("EFTPOS CARD PURCHASE 0011") == ""


# -------------------------------------------------------------------- regularity


def test_interval_statistics():
    dates = [date(2026, 7, 20), date(2026, 8, 20), date(2026, 9, 20)]
    median, variation = det._interval_stats(dates)
    assert median == 31
    assert variation == pytest.approx(0.0)


def test_regularity_decision():
    monthly = [date(2026, 7, 20), date(2026, 8, 20), date(2026, 9, 20)]
    assert det.is_regular(monthly) is True
    # A three-day gap is too short to be a subscription.
    assert det.is_regular([date(2026, 9, 1), date(2026, 9, 4)]) is False
    # Wildly uneven gaps are not a schedule.
    assert det.is_regular([date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 30)]) is False
    assert det.is_regular([date(2026, 9, 1)]) is False


def test_monthly_estimate_annualises_a_gap():
    assert det.monthly_estimate(31.00, 31) == pytest.approx(31.00 * 365.25 / 12 / 31)
    assert det.monthly_estimate(12.99, 0) == 0.0
    assert det.monthly_estimate(12.99, None) == 0.0


# ----------------------------------------------------------------------- grouping


def test_transactions_group_by_merchant(view):
    groups = det.group_by_merchant(view.transactions)
    # Expenses only by default, so the salary line is not a group.
    assert set(groups) == {"RENT", "INTERNET PROVIDER", "SPOTIFY",
                           "ELECTRICITY", "MAGAZINE", "CAFE LATTE"}
    assert len(groups["CAFE LATTE"]) == 3
    assert all(t.amount < 0 for t in sum(groups.values(), []))


def test_grouping_can_include_income(view):
    groups = det.group_by_merchant(view.transactions, expenses_only=False)
    assert "SALARY ACME" in groups
    assert len(groups["SALARY ACME"]) == 1


def test_undated_transactions_are_skipped_not_grouped(store):
    store.add("transactions", Transaction(id="t_nd", amount=-5.0, description="GHOST"))
    groups = det.group_by_merchant(store.view().transactions)
    assert "GHOST" not in groups


def test_recurring_payments_ranks_by_monthly_cost(view):
    groups = det.recurring_payments(view)

    assert [g.merchant for g in groups] == ["RENT", "INTERNET PROVIDER", "CAFE LATTE"]

    rent = groups[0]
    assert rent.occurrences == 2
    assert rent.interval_days == 30
    assert rent.average_amount == pytest.approx(520.00)
    assert rent.regular is True
    assert rent.tracked is True
    assert rent.matching_def_ids == ["d_rent"]
    assert rent.first_date == date(2026, 9, 1)
    assert rent.last_date == date(2026, 10, 1)
    assert rent.total == pytest.approx(1040.00)


def test_recurring_payments_can_be_filtered_to_one_merchant(view):
    found = det.recurring_payments(view, "RENT")
    assert len(found) == 1
    assert found[0].merchant == "RENT"
    assert det.recurring_payments(view, "NOT A MERCHANT") == []


def test_a_merchant_with_one_occurrence_is_not_recurring(view):
    assert all(g.merchant != "SPOTIFY" for g in det.recurring_payments(view))


def test_two_same_day_coffees_break_regularity_rather_than_being_averaged_away(view):
    coffee = next(g for g in det.recurring_payments(view) if g.merchant == "CAFE LATTE")
    assert coffee.occurrences == 3
    assert coffee.regular is False


# ------------------------------------------------------------------ matching ledger


def test_definitions_match_by_containment_either_way(view):
    # The transaction says INTERNET PROVIDER; the ledger says Internet.
    assert det.matching_definitions(view, "INTERNET PROVIDER") == ["d_net"]
    assert det.matching_definitions(view, "RENT") == ["d_rent"]
    assert det.matching_definitions(view, "CAR INSURANCE") == ["d_insurance"]


def test_an_unrelated_merchant_matches_nothing(view):
    assert det.matching_definitions(view, "NETFLIX") == []
    assert det.matching_definitions(view, "") == []


def test_income_items_are_not_offered_as_expense_definitions(view, store):
    # There is an item named Salary, but it is income, so it cannot match a charge.
    assert det.matching_definitions(view, "SALARY ACME") == []


# -------------------------------------------------------------- detect_subscriptions


def test_the_fixtures_recurring_charges_are_all_already_tracked(view):
    """Every regular merchant in the fixture has a ledger item, so nothing is proposed."""
    assert det.detect_subscriptions(view, today=TODAY) == []
    assert det.untracked_spend(view, today=TODAY).is_empty is True


def test_an_untracked_recurring_charge_is_detected(store):
    add_transactions(store, NETFLIX, [date(2026, 7, 20), date(2026, 8, 20), date(2026, 9, 20)])

    found = det.detect_subscriptions(store.view(), today=TODAY)

    assert len(found) == 1
    charge = found[0]
    assert charge.merchant == "NETFLIX"
    assert charge.occurrences == 3
    assert charge.average_amount == pytest.approx(22.99)
    assert charge.interval_days == 31
    assert charge.monthly_estimate == pytest.approx(22.99 * 365.25 / 12 / 31)
    assert charge.yearly_estimate == pytest.approx(charge.monthly_estimate * 12)
    assert charge.first_date == date(2026, 7, 20)
    assert charge.last_date == date(2026, 9, 20)
    assert charge.suggested_name == "Netflix"
    assert len(charge.transaction_ids) == 3


def test_a_charge_that_matches_the_ledger_is_not_proposed(store):
    add_transactions(store, "SPOTIFY P4482",
                     [date(2026, 6, 12), date(2026, 7, 12), date(2026, 8, 12)])
    # Spotify is already an item, so the suggestion is suppressed.
    assert det.detect_subscriptions(store.view(), today=TODAY) == []


def test_an_irregular_charge_is_not_proposed_as_a_subscription(store):
    add_transactions(store, NETFLIX,
                     [date(2026, 7, 3), date(2026, 7, 9), date(2026, 9, 28)])
    assert det.detect_subscriptions(store.view(), today=TODAY) == []


def test_a_single_charge_is_not_a_subscription(store):
    add_transactions(store, NETFLIX, [date(2026, 9, 20)])
    assert det.detect_subscriptions(store.view(), today=TODAY) == []


def test_untracked_spend_totals_the_suggestions(store):
    add_transactions(store, NETFLIX, [date(2026, 7, 20), date(2026, 8, 20), date(2026, 9, 20)])
    add_transactions(store, "DISNEY PLUS 111",
                     [date(2026, 7, 5), date(2026, 8, 5)], amount=-13.99)

    found = det.untracked_spend(store.view(), today=TODAY)

    assert found.count == 2
    assert found.monthly_total == pytest.approx(
        sum(c.monthly_estimate for c in found.charges))
    assert found.yearly_total == pytest.approx(found.monthly_total * 12)
    assert found.is_empty is False


def test_detection_never_proposes_a_change_to_the_store(store):
    """§17: detection proposes; it never writes."""
    add_transactions(store, NETFLIX, [date(2026, 7, 20), date(2026, 8, 20), date(2026, 9, 20)])
    before_items = len(store.doc.items)
    before_version = store.version

    det.detect_subscriptions(store.view(), today=TODAY)

    assert len(store.doc.items) == before_items
    assert store.version == before_version


# ------------------------------------------------------------------ renewal timeline


def test_upcoming_renewals_are_expenses_within_the_window(view):
    found = det.upcoming_renewals(view, TODAY, days=60)

    assert len(found) == 40
    assert all(o.type == "expense" for o in found)
    assert [o.due for o in found] == sorted(o.due for o in found)
    assert found[0].date == date(2026, 10, 7)      # the coffee run, first thing due
    assert found[-1].date <= TODAY + timedelta(days=60)


def test_a_one_off_is_not_a_renewal(view):
    found = det.upcoming_renewals(view, TODAY, days=400)
    assert not any(o.name == "Concert ticket" for o in found)


def test_a_cancelled_occurrence_is_not_a_renewal(view):
    found = det.upcoming_renewals(view, date(2026, 10, 1), days=40)
    assert not any(o.name == "Magazine" and o.date == date(2026, 10, 20) for o in found)


def test_renewals_can_be_limited_to_subscriptions(view):
    all_renewals = det.upcoming_renewals(view, TODAY, days=60)
    subscriptions = det.upcoming_renewals(view, TODAY, days=60, subscriptions_only=True)

    assert len(subscriptions) < len(all_renewals)
    assert all(o.subscription for o in subscriptions)
    assert not any(o.name == "Rent" for o in subscriptions)
    assert any(o.name == "Spotify" for o in subscriptions)


def test_renewal_count_and_days_until_next(view):
    assert det.renewal_count(view, TODAY, days=60) == 40
    assert det.renewal_count(view, TODAY, days=0) == 1     # the coffee due today
    assert det.days_until_next(view.item("d_spotify"), TODAY) == 5


def test_days_until_next_is_none_for_an_ended_item(view, store):
    store.view().item("d_coffee").end = TODAY
    assert det.days_until_next(store.view().item("d_coffee"), TODAY) is None
