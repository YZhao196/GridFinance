"""Spotify is the fixture's shared item: $12.99/month, split evenly with Sam and Alex.

Divided by members *plus the user*, that is $4.33 each — 12.99 / 3 lands exactly, which
makes every number here hand-checkable.
"""

from datetime import date

import pytest

from finance_tool.engine import shared as shr
from finance_tool.store.entities import Item, EXPENSE

from fixtures import TODAY, build_store, item_named

SPOTIFY_OCC = date(2026, 10, 12)
SHARE = 12.99 / 3   # 4.33 exactly


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def view(store):
    return store.view()


@pytest.fixture
def spotify(view):
    return item_named(view, "Spotify")


# --------------------------------------------------------------------------- splits


def test_a_shared_item_is_recognised_and_an_unshared_one_is_not(view):
    assert shr.is_shared(item_named(view, "Spotify")) is True
    assert shr.is_shared(item_named(view, "Rent")) is False
    assert shr.split_members(item_named(view, "Spotify")) == ["p_sam", "p_alex"]


def test_an_even_split_includes_the_users_own_share(spotify):
    shares = shr.member_shares(spotify, SPOTIFY_OCC)

    assert shares == {"p_sam": pytest.approx(SHARE), "p_alex": pytest.approx(SHARE)}
    # The user is the third person consuming it, so they carry a third.
    assert shr.user_share(spotify, SPOTIFY_OCC) == pytest.approx(SHARE)


def test_the_shares_add_up_to_the_occurrence(spotify):
    assert shr.splits_are_exact(spotify, SPOTIFY_OCC) is True


def test_an_explicit_split_is_taken_literally(view, store):
    item = view.item("d_net")
    item.shared = {"members": ["p_sam"], "split": {"p_sam": 20.00}}
    shares = shr.member_shares(item, date(2026, 10, 5))

    assert shares == {"p_sam": 20.00}
    assert shr.user_share(item, date(2026, 10, 5)) == pytest.approx(59.00)
    assert shr.splits_are_exact(item, date(2026, 10, 5)) is True


def test_an_explicit_split_that_does_not_add_up_is_reported(view):
    item = view.item("d_net")
    item.shared = {"members": ["p_sam"], "split": {"p_sam": 500.00}}
    assert shr.splits_are_exact(item, date(2026, 10, 5)) is False


def test_an_unshared_item_has_no_members_and_no_shares(view):
    rent = item_named(view, "Rent")
    assert shr.member_shares(rent, date(2026, 10, 1)) == {}
    assert shr.user_share(rent, date(2026, 10, 1)) == pytest.approx(520.00)
    assert shr.all_paid(rent, date(2026, 10, 1)) is True


def test_a_share_follows_an_amount_override(view):
    item = Item(id="d_s", name="Shared", type=EXPENSE, amount=30.0,
                start=date(2026, 10, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
                shared={"members": ["p_sam", "p_alex"], "split": "even"},
                overrides={date(2026, 11, 1): {"amount": 60.0}})

    assert shr.member_shares(item, date(2026, 10, 1))["p_sam"] == pytest.approx(10.00)
    assert shr.member_shares(item, date(2026, 11, 1))["p_sam"] == pytest.approx(20.00)


# ------------------------------------------------------------------------ paid state


def test_nobody_has_paid_until_they_have(spotify):
    assert shr.all_paid(spotify, SPOTIFY_OCC) is False
    assert shr.unpaid_members(spotify, SPOTIFY_OCC) == ["p_sam", "p_alex"]
    assert shr.settled_members(spotify, SPOTIFY_OCC) == []
    assert shr.occurrence_outstanding(spotify, SPOTIFY_OCC) == pytest.approx(2 * SHARE)


def test_paid_state_is_per_person_per_occurrence(store):
    other = date(2026, 11, 12)
    store.mark_share_paid("d_spotify", SPOTIFY_OCC, "p_sam")

    spotify = store.view().item("d_spotify")
    assert shr.unpaid_members(spotify, SPOTIFY_OCC) == ["p_alex"]
    assert shr.occurrence_outstanding(spotify, SPOTIFY_OCC) == pytest.approx(SHARE)
    assert shr.all_paid(spotify, SPOTIFY_OCC) is False

    # A different month is untouched — the whole point of §16.
    assert shr.unpaid_members(spotify, other) == ["p_sam", "p_alex"]


def test_all_paid_once_everyone_has(store):
    store.mark_share_paid("d_spotify", SPOTIFY_OCC, "p_sam")
    store.mark_share_paid("d_spotify", SPOTIFY_OCC, "p_alex")

    spotify = store.view().item("d_spotify")
    assert shr.all_paid(spotify, SPOTIFY_OCC) is True
    assert shr.occurrence_outstanding(spotify, SPOTIFY_OCC) == 0.0


# ----------------------------------------------------------------------- share rows


def test_share_rows_cover_every_shared_occurrence_in_the_range(view):
    rows = shr.share_rows(view, date(2026, 10, 1), date(2026, 10, 31))
    assert [r.occurrence.date for r in rows] == [date(2026, 10, 12)]
    assert rows[0].settled is False
    assert rows[0].outstanding == pytest.approx(2 * SHARE)


def test_share_rows_can_hide_settled_occurrences(view, store):
    store.mark_share_paid("d_spotify", date(2026, 10, 12), "p_sam")
    store.mark_share_paid("d_spotify", date(2026, 10, 12), "p_alex")

    unpaid = shr.share_rows(store.view(), date(2026, 10, 1), date(2026, 10, 31),
                            include_paid=False)
    assert unpaid == []
    all_rows = shr.share_rows(store.view(), date(2026, 10, 1), date(2026, 10, 31))
    assert len(all_rows) == 1


# ------------------------------------------------------------------------- rosters


def test_the_default_span_runs_from_the_first_shared_item_to_today(view):
    span = shr.default_span(view, TODAY)
    assert span == (date(2026, 3, 12), TODAY)


def test_the_roster_counts_every_occurrence_since_the_item_started(view):
    """Spotify has billed on the 12th since March; October's bill has not landed yet."""
    span = shr.default_span(view, TODAY)
    roster = {b.person.name: b for b in shr.people_roster(view, span)}

    assert set(roster) == {"Sam", "Alex"}
    for balance in roster.values():
        assert balance.occurrences == 7          # March through September
        assert balance.owed == pytest.approx(7 * SHARE)
        assert balance.settled == 0.0
        assert balance.outstanding == pytest.approx(7 * SHARE)
        assert balance.oldest == date(2026, 3, 12)
        assert balance.settled_ratio == 0.0


def test_the_roster_is_ordered_by_what_is_outstanding(view, store):
    span = shr.default_span(view, TODAY)
    store.mark_share_paid("d_spotify", date(2026, 3, 12), "p_alex")

    roster = shr.people_roster(store.view(), span)
    assert [b.person.name for b in roster] == ["Sam", "Alex"]
    assert roster[0].outstanding > roster[1].outstanding


def test_who_owes_and_the_total(view):
    span = shr.default_span(view, TODAY)
    assert shr.who_owes(view, span) == {
        "p_sam": pytest.approx(7 * SHARE),
        "p_alex": pytest.approx(7 * SHARE),
    }
    assert shr.owed_total(view, span) == pytest.approx(14 * SHARE)


def test_settling_everything_empties_who_owes(store):
    span = shr.default_span(store.view(), TODAY)
    for occ in range(3, 10):
        store.mark_share_paid("d_spotify", date(2026, occ, 12), "p_sam")
        store.mark_share_paid("d_spotify", date(2026, occ, 12), "p_alex")

    assert shr.who_owes(store.view(), span) == {}
    assert shr.all_settled(store.view(), span) is True
    assert shr.settle_up(store.view(), span) == {}


def test_partial_settlement_shows_up_as_a_smaller_outstanding(view, store):
    span = shr.default_span(view, TODAY)
    store.mark_share_paid("d_spotify", date(2026, 3, 12), "p_sam")

    assert shr.who_owes(store.view(), span)["p_sam"] == pytest.approx(6 * SHARE)
    assert shr.who_owes(store.view(), span)["p_alex"] == pytest.approx(7 * SHARE)


def test_owed_summary_leads_with_the_largest_debts(view):
    span = shr.default_span(view, TODAY)
    summary = shr.owed_summary(view, span, top=1)

    assert summary.count == 2
    assert len(summary.people) == 1
    assert summary.total == pytest.approx(14 * SHARE)
    assert summary.is_empty is False


def test_a_store_with_no_shares_owes_nothing():
    from finance_tool.store.entities import StoreDocument
    from finance_tool.store.store import Store

    empty = Store(doc=StoreDocument(), data_dir=None).view()
    span = (date(2026, 1, 1), TODAY)
    assert shr.people_roster(empty, span) == []
    assert shr.who_owes(empty, span) == {}
    assert shr.owed_summary(empty, span).is_empty is True
    assert shr.all_settled(empty, span) is True


def test_share_of_total_and_even_share(spotify):
    assert shr.share_of_total(spotify, SPOTIFY_OCC, "p_sam") == pytest.approx(1 / 3)
    assert shr.share_of_total(spotify, SPOTIFY_OCC, "p_nobody") == 0.0
    assert shr.even_share(30.0, ["a", "b"]) == pytest.approx(10.0)
    assert shr.even_share(30.0, []) == 0.0


def test_share_of_total_is_guarded_against_a_zero_amount():
    item = Item(id="d_z", name="Zero", type=EXPENSE, amount=0.0,
                start=date(2026, 10, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
                shared={"members": ["p_sam"], "split": "even"})
    assert shr.share_of_total(item, date(2026, 10, 1), "p_sam") == 0.0
    assert shr.member_shares(item, date(2026, 10, 1)) == {"p_sam": 0.0}
