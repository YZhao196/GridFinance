from datetime import date

import pytest

from finance_tool.engine import search as se

from fixtures import build_store


@pytest.fixture
def store():
    store = build_store()
    store.add_canvas("Bills")
    store.add_canvas("Runway")
    return store


@pytest.fixture
def view(store):
    return store.view()


# ------------------------------------------------------------------------ scoring


@pytest.mark.parametrize("text,query,expected", [
    ("Rent", "rent", se.SCORE_EXACT),
    ("Rental bond", "rent", se.SCORE_PREFIX),
    ("My rent payment", "rent", se.SCORE_WORD_PREFIX),
    ("The rentenmark", "renten", se.SCORE_WORD_PREFIX),
])
def test_match_score_ranks_strictest_first(text, query, expected):
    assert se.match_score(text, query) == expected


def test_a_scattered_match_is_the_last_resort():
    # "rent" is not in "Prepayment" as a run, but its letters are in order.
    assert se.match_score("Prepayment", "rent") == se.SCORE_FUZZY
    assert se.match_score("Prepayment", "zzz") is None


def test_match_score_finds_a_substring_that_is_not_a_word_start():
    assert se.match_score("Car insurance", "insur") == se.SCORE_WORD_PREFIX
    assert se.match_score("Reinsurance", "insur") == se.SCORE_SUBSTRING


def test_match_score_falls_back_to_fuzzy():
    assert se.match_score("Salary", "sly") == se.SCORE_FUZZY
    assert se.match_score("Salary", "yz") is None


def test_match_score_of_nothing_is_none():
    assert se.match_score("", "rent") is None
    assert se.match_score(None, "rent") is None
    assert se.match_score("Rent", "") is None


def test_a_secondary_field_match_is_capped_below_a_primary_one():
    score = se._score("RENT PAYMENT", ["Rent"], "rent")
    # The description prefixes the query; the category happens to be exact. The
    # description wins, because that is the row's own name.
    assert score == se.SCORE_PREFIX


def test_a_buried_tag_cannot_outrank_a_title():
    assert se._score("Internet", ["home"], "internet") == se.SCORE_EXACT
    assert se._score("Something else", ["internet"], "internet") == se.SCORE_SUBSTRING


# -------------------------------------------------------------------- search itself


def test_an_empty_query_returns_nothing(view):
    assert se.search(view, "") == []
    assert se.search(view, "   ") == []


def test_a_query_matching_nothing_returns_nothing(view):
    assert se.search(view, "zzzzqqq") == []


def test_an_exact_item_name_ranks_first(view):
    results = se.search(view, "rent")

    assert results[0].kind == se.ITEM
    assert results[0].title == "Rent"
    assert results[0].score == se.SCORE_EXACT
    # A transaction whose description prefixes the query follows, at a lower score.
    assert any(r.title == "RENT PAYMENT" for r in results)
    assert all(r.score <= results[0].score for r in results)


def test_results_are_ordered_by_score_then_recency(view):
    results = se.search(view, "rent")
    keys = [(-r.score, -r.recency, r.title.casefold()) for r in results]
    assert keys == sorted(keys)


def test_search_finds_an_item_by_its_category(view):
    results = se.search(view, "Utilities")
    kinds = {r.kind for r in results}
    assert se.ITEM in kinds
    assert "Electricity" in {r.title for r in results}


def test_search_finds_an_item_by_its_note(view, store):
    store.view().item("d_rent").note = "includes water usage"
    results = se.search(store.view(), "water usage")
    assert "Rent" in {r.title for r in results}


def test_search_finds_an_item_by_its_tag(view):
    results = se.search(view, "health")
    assert "Gym" in {r.title for r in results}


def test_search_finds_transactions(view):
    results = se.search(view, "latte")
    assert len(results) == 3
    assert all(r.kind == se.TRANSACTION for r in results)
    assert all(r.title == "CAFE LATTE" for r in results)


def test_a_transaction_result_carries_its_date_and_category(view):
    result = next(r for r in se.search(view, "latte") if r.id == "t_11")
    assert result.subtitle == "2026-10-06 · Uncategorised"
    assert result.target["account"] == "a_everyday"


def test_search_finds_people_accounts_goals_and_consumables(view):
    people = se.search(view, "sam")
    assert se.PERSON in {r.kind for r in people}
    assert people[0].kind == se.PERSON          # an exact name beats a fuzzy one

    assert {r.kind for r in se.search(view, "everyday")} == {se.ACCOUNT}
    assert {r.kind for r in se.search(view, "japan")} == {se.GOAL}
    assert {r.kind for r in se.search(view, "beans")} == {se.CONSUMABLE}


def test_search_finds_rules_by_what_they_match(view):
    results = se.search(view, "rent")
    rule = next(r for r in results if r.kind == se.RULE)
    assert rule.title == "RENT"
    assert rule.subtitle == "→ Rent"


def test_search_finds_canvases(view):
    results = se.search(view, "runway")
    assert [r.kind for r in results] == [se.CANVAS]
    assert results[0].target == {"kind": se.CANVAS, "id": results[0].id}


def test_a_word_appearing_in_several_places_returns_several_kinds(view):
    results = se.search(view, "savings")
    kinds = set(se.kinds_present(results))
    assert se.ACCOUNT in kinds
    assert se.ITEM in kinds
    # The account's exact name beats the item's prefix.
    assert results[0].kind == se.ACCOUNT


def test_every_result_carries_a_navigation_target(view):
    for result in se.search(view, "a"):
        assert result.navigation
        assert result.navigation["kind"] == result.kind
        assert result.navigation["id"] == result.id


def test_limit_truncates_without_reordering(view):
    everything = se.search(view, "a", limit=0)
    limited = se.search(view, "a", limit=3)
    assert len(limited) == 3
    assert limited == everything[:3]


def test_search_is_case_insensitive(view):
    assert se.search(view, "SPOTIFY") == se.search(view, "spotify")


def test_kinds_present_lists_each_kind_once_in_order(view):
    kinds = se.kinds_present(se.search(view, "rent"))
    assert len(kinds) == len(set(kinds))
    assert kinds[0] == se.ITEM


def test_search_over_an_empty_store_is_empty():
    from finance_tool.store.entities import StoreDocument
    from finance_tool.store.store import Store

    empty = Store(doc=StoreDocument(), data_dir=None).view()
    assert se.search(empty, "anything") == []
