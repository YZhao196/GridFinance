from datetime import date

import pytest

from finance_tool.engine import categorise as cat
from finance_tool.store.entities import Rule, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store

from fixtures import build_store

CAFE = 4.50


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def view(store):
    return store.view()


# --------------------------------------------------------------------- the seeds


def test_seeded_defaults_are_ordinary_rules_at_negative_priority():
    """No hidden layer: a seed is a Rule, editable and deletable like any other."""
    seeds = cat.seed_rules()

    assert seeds
    assert all(isinstance(rule, Rule) for rule in seeds)
    assert all(cat.is_seed(rule) for rule in seeds)
    # Negative priority is the whole mechanism that lets a user rule win.
    assert all(rule.priority < 0 for rule in seeds)
    assert all(rule.category for rule in seeds)


def test_seed_ids_are_stable_and_unique():
    ids = [rule.id for rule in cat.seed_rules()]
    assert len(ids) == len(set(ids))
    assert cat.seed_rules()[0].id == cat.seed_rules()[0].id


def test_every_seed_keyword_is_lowercase_and_non_empty():
    for rule in cat.seed_rules():
        assert rule.match
        assert rule.match == rule.match.casefold()


# ------------------------------------------------------------------- rule matching


def test_a_long_keyword_matches_as_a_substring():
    rule = Rule(id="r", match="electricity", category="Utilities")
    assert cat.rule_matches(rule, "ELECTRICITY CO 9921") is True
    assert cat.rule_matches(rule, "electricityireland") is True
    assert cat.rule_matches(rule, "GAS BILL") is False


def test_a_four_letter_keyword_must_stand_on_its_own():
    rule = Rule(id="r", match="rent", category="Rent")
    assert cat.rule_matches(rule, "RENT PAYMENT 8821") is True
    assert cat.rule_matches(rule, "Prepayment to Bob") is False


def test_a_short_keyword_has_to_stand_on_its_own():
    """'bp' is a fuel brand, but as a bare substring it is also inside 'subprime'."""
    rule = Rule(id="r", match="bp", category="Transport")
    assert cat.rule_matches(rule, "BP CONNECT 1234") is True
    assert cat.rule_matches(rule, "SUBSCRIPTION FEE") is False


def test_a_rule_matches_the_merchant_key_too():
    rule = Rule(id="r", match="NETFLIX", category="Subscriptions")
    assert cat.rule_matches(rule, "NETFLIX.COM 4482", "NETFLIX") is True
    assert cat.rule_matches(rule, "SOMETHING ELSE", "NETFLIX") is True
    assert cat.rule_matches(rule, "SOMETHING ELSE", "OTHER") is False


def test_an_empty_match_matches_nothing():
    assert cat.rule_matches(Rule(id="r", match=""), "anything") is False


# ---------------------------------------------------------------------- decision


def test_categorise_returns_the_first_matching_rule():
    rules = [
        Rule(id="r_net", match="INTERNET", category="Utilities", priority=10),
        Rule(id="r_any", match="PROVIDER", category="Other", priority=5),
    ]
    decision = cat.categorise("INTERNET PROVIDER 4471", rules)

    assert decision.category == "Utilities"
    assert decision.rule_id == "r_net"
    assert decision.matched == "INTERNET"
    assert decision.categorised is True


def test_higher_priority_wins_regardless_of_rule_order():
    rules = [
        Rule(id="r_low", match="CAFE", category="Wrong", priority=1),
        Rule(id="r_high", match="CAFE", category="Right", priority=99),
    ]
    assert cat.categorise("CAFE LATTE", rules).category == "Right"


def test_a_user_rule_beats_a_seeded_one():
    user = Rule(id="r_user", match="SPOTIFY", category="Music", priority=0)
    rules = cat.seed_rules() + [user]
    assert cat.categorise("SPOTIFY P4482", rules).category == "Music"


def test_a_seeded_rule_is_deletable_and_then_matches_nothing():
    without_spotify = [rule for rule in cat.seed_rules() if rule.match != "spotify"]
    assert cat.categorise("SPOTIFY P4482", without_spotify).category is None
    assert cat.categorise("SPOTIFY P4482", cat.seed_rules()).category == "Subscriptions"


def test_an_unmatched_description_is_left_alone_not_guessed():
    decision = cat.categorise("UNKNOWN MERCHANT 9911", cat.seed_rules())
    assert decision == cat.UNCLEAR
    assert decision.categorised is False
    assert decision.rule_id is None


def test_a_seed_can_attach_tags():
    decision = cat.categorise("NETFLIX.COM 4482", cat.seed_rules())
    assert decision.category == "Subscriptions"
    assert decision.tags == ["streaming"]


def test_categorise_many_and_uncategorised(view):
    pairs = cat.categorise_many(list(view.transactions), cat.seed_rules())
    assert len(pairs) == len(view.transactions)
    # The fixture's three cafes are deliberately uncategorised, ready for a rule.
    assert [t.id for t in cat.uncategorised(list(view.transactions))] == ["t_07", "t_08", "t_11"]


def test_order_rules_is_stable():
    rules = [Rule(id="b", match="x", priority=5), Rule(id="a", match="y", priority=5)]
    assert [r.id for r in cat.order_rules(rules)] == ["a", "b"]


# ------------------------------------------------------------------- suggestions


def test_suggestions_group_uncategorised_spend_by_merchant(view):
    found = cat.suggest_rules(view)

    assert len(found) == 1
    suggestion = found[0]
    assert suggestion.merchant == "CAFE LATTE"
    assert suggestion.occurrences == 3
    assert suggestion.total == pytest.approx(3 * CAFE)
    assert suggestion.average == pytest.approx(CAFE)
    assert suggestion.category == "Cafes"          # from the seeded keyword table
    assert suggestion.sample == "CAFE LATTE"
    assert len(suggestion.transaction_ids) == 3
    assert suggestion.label == "CAFE LATTE ×3 → Cafes"


def test_a_single_uncategorised_row_is_not_worth_a_rule(view, store):
    store.view().transaction("t_11").category = "Cafes"
    store.remove("transactions", "t_07")
    store.remove("transactions", "t_08")
    assert cat.suggest_rules(store.view()) == []


def test_a_merchant_no_seed_recognises_still_gets_a_proposal(store):
    for index, when in enumerate(["2026-09-01", "2026-09-08"]):
        store.add("transactions", Transaction(
            id=f"t_z{index}", date=date.fromisoformat(when),
            amount=-31.00, description="ZORBULON INDUSTRIES 77", account="a_everyday"))

    found = cat.suggest_rules(store.view())
    zorbulon = next(s for s in found if s.merchant.startswith("ZORBULON"))
    assert zorbulon.category is None
    assert zorbulon.label == "ZORBULON INDUSTRIES ×2 → ?"


def test_suggestions_are_ordered_by_total(view, store):
    store.add("transactions", Transaction(
        id="t_big", date=date(2026, 9, 3), amount=-900.00,
        description="MYSTERY VENDOR 42", account="a_everyday"))
    store.add("transactions", Transaction(
        id="t_big2", date=date(2026, 9, 4), amount=-900.00,
        description="MYSTERY VENDOR 43", account="a_everyday"))

    found = cat.suggest_rules(store.view())
    assert found[0].merchant == "MYSTERY VENDOR"
    assert found[0].total == pytest.approx(1800.00)


def test_the_widget_one_liner(view):
    assert cat.suggestions_summary(view) == "3 uncategorised · review 1 suggested rule"


def test_the_one_liner_when_nothing_is_left(view, store):
    for txn_id in ("t_07", "t_08", "t_11"):
        store.view().transaction(txn_id).category = "Cafes"
    assert cat.suggestions_summary(store.view()) == "Everything categorised"


def test_a_suggested_rule_is_a_plain_rule():
    suggestion = cat.RuleSuggestion(merchant="CALTEX FUEL", occurrences=14, total=900.0,
                                    category="Transport", tags=["car"])
    rule = suggestion.to_rule()
    assert isinstance(rule, Rule)
    assert rule.match == "CALTEX FUEL"
    assert rule.category == "Transport"
    assert rule.tags == ["car"]


# --------------------------------------------------------------------- accepting


def test_accepting_a_suggestion_writes_a_rule_and_recategorises(view, store):
    suggestion = cat.suggest_rules(store.view())[0]
    rule = cat.accept_suggestion(store, suggestion)

    assert rule.id and cat.is_seed(rule) is False
    assert rule.match == "CAFE LATTE"
    assert store.view().rule(rule.id) is not None
    # And the rows it was proposed for are now categorised.
    assert store.view().transaction("t_07").category == "Cafes"
    assert store.view().transaction("t_11").category == "Cafes"
    assert cat.suggestions_summary(store.view()) == "Everything categorised"


def test_accepting_a_suggestion_with_a_chosen_category(view, store):
    suggestion = cat.suggest_rules(store.view())[0]
    rule = cat.accept_suggestion(store, suggestion, category="Coffee")
    assert rule.category == "Coffee"
    assert store.view().transaction("t_07").category == "Coffee"


def test_accepting_does_not_disturb_a_row_that_was_already_categorised(view, store):
    before = store.view().transaction("t_01").category
    cat.accept_suggestion(store, cat.suggest_rules(store.view())[0])
    assert store.view().transaction("t_01").category == before == "Rent"


def test_recategorise_all_reports_what_it_changed_and_is_idempotent(view, store):
    # Eight of the eleven rows change. The three that do not are the ones carrying a
    # category with no rule to attribute it to — hand-set, and therefore protected.
    assert cat.recategorise_all(store, rules=cat.seed_rules()) == 8
    # And the second pass is a genuine no-op, which is the property that matters.
    assert cat.recategorise_all(store, rules=cat.seed_rules()) == 0


def test_recategorise_all_protects_a_category_a_person_set(store):
    """The bug this guards: "re-run categorisation" used to reset every hand-set category
    to whatever a rule said, or to nothing at all."""
    store.view().transaction("t_11").category = "My own category"
    store.view().transaction("t_11").rule_id = None

    cat.recategorise_all(store, rules=cat.seed_rules())

    assert store.view().transaction("t_11").category == "My own category"


def test_recategorise_all_can_be_told_to_take_the_manual_ones_too(store):
    store.view().transaction("t_11").category = "My own category"
    store.view().transaction("t_11").rule_id = None

    cat.recategorise_all(store, rules=cat.seed_rules(), protect_manual=False)

    assert store.view().transaction("t_11").category == "Cafes"


def test_recategorise_all_can_fill_gaps_only(view, store):
    store.view().transaction("t_11").category = "Something the user chose"
    changed = cat.recategorise_all(store, rules=cat.seed_rules(), overwrite=False)

    assert store.view().transaction("t_11").category == "Something the user chose"
    assert changed == 2


def test_recategorise_all_still_overwrites_what_a_rule_set(view, store):
    """A rule the user has just edited must take effect on the rows it already claimed."""
    store.view().rule("r_rent").category = "Housing"

    cat.recategorise_all(store, rules=store.doc.rules)

    assert store.view().transaction("t_01").category == "Housing"
    assert store.view().transaction("t_01").rule_id == "r_rent"


def test_recategorise_all_touches_the_store_version(view, store):
    before = store.version
    cat.recategorise_all(store, rules=cat.seed_rules())
    assert store.version > before


# ------------------------------------------------------------------ seed install


def test_ensure_seed_rules_installs_once():
    store = Store(doc=StoreDocument(), data_dir=None)
    assert cat.ensure_seed_rules(store) == len(cat.seed_rules())
    assert cat.ensure_seed_rules(store) == 0
    assert len(store.doc.rules) == len(cat.seed_rules())


def test_ensure_seed_rules_does_not_duplicate_a_users_own_rule():
    store = Store(doc=StoreDocument(rules=[Rule(id="r1", match="rent", category="Housing")]),
                  data_dir=None)
    added = cat.ensure_seed_rules(store)
    assert added == len(cat.seed_rules()) - 1
    assert sum(1 for r in store.doc.rules if r.match == "rent") == 1


def test_ensure_seed_rules_skips_a_keyword_the_store_already_has(store):
    # The fixture has rules for RENT, INTERNET and SALARY, so those seeds are not added.
    added = cat.ensure_seed_rules(store)
    assert added == len(cat.seed_rules()) - 3
    for keyword in ("rent", "internet", "salary"):
        assert sum(1 for r in store.doc.rules if r.match.casefold() == keyword) == 1


def test_ensure_seed_rules_leaves_a_seeded_store_alone(store):
    cat.ensure_seed_rules(store)
    assert cat.ensure_seed_rules(store) == 0


# ---------------------------------------------------------------------- helpers


def test_categories_in_use_collects_from_everywhere(view):
    names = cat.categories_in_use(view)
    assert "Rent" in names
    assert "Utilities" in names
    assert names == sorted(names, key=str.casefold)


def test_coverage_by_category_includes_the_uncategorised_bucket(view):
    found = cat.coverage_by_category(view, date(2026, 10, 1), date(2026, 10, 31))
    assert found == {"Rent": pytest.approx(520.00), "Utilities": pytest.approx(79.00),
                     "Uncategorised": pytest.approx(4.50)}


def test_coverage_by_category_is_largest_first(view):
    found = cat.coverage_by_category(view)
    assert list(found) == ["Rent", "Utilities", "Subscriptions", "Uncategorised"]


# --------------------------------------------------------------- richer rules


def test_equals_compares_the_whole_normalised_text():
    rule = Rule(id="r", match="COLES NORTHLAND", category="Groceries",
                match_type="equals")
    assert cat.rule_matches(rule, "COLES NORTHLAND") is True
    assert cat.rule_matches(rule, "COLES NORTHLAND VIC") is False


def test_regex_searches_the_description_and_merchant():
    rule = Rule(id="r", match=r"^UBER\s+\*", category="Transport", match_type="regex")
    assert cat.rule_matches(rule, "UBER * TRIP") is True
    assert cat.rule_matches(rule, "MY UBER") is False


def test_an_invalid_regex_matches_nothing_instead_of_raising():
    rule = Rule(id="r", match="[", category="Transport", match_type="regex")
    assert cat.rule_matches(rule, "anything") is False


def test_merchant_matches_only_the_merchant_key():
    rule = Rule(id="r", match="NETFLIX", category="Subscriptions",
                match_type="merchant")
    assert cat.rule_matches(rule, "CARD PURCHASE 123456", "NETFLIX") is True
    assert cat.rule_matches(rule, "NETFLIX.COM 4482", None) is True
    assert cat.rule_matches(rule, "CARD PURCHASE 123456", "SPOTIFY") is False


def test_amount_bounds_compare_the_size_of_the_movement():
    rule = Rule(id="r", match="COLES", category="Groceries",
                amount_min=10.0, amount_max=100.0)
    assert cat.rule_matches(rule, "COLES", amount=-50.0) is True
    assert cat.rule_matches(rule, "COLES", amount=50.0) is True
    assert cat.rule_matches(rule, "COLES", amount=-5.0) is False
    assert cat.rule_matches(rule, "COLES", amount=-150.0) is False


def test_an_inactive_rule_matches_nothing():
    rule = Rule(id="r", match="COLES", category="Groceries", active=False)
    assert cat.rule_matches(rule, "COLES") is False


def test_a_tag_only_rule_annotates_without_shadowing_a_category_rule():
    tagging = Rule(id="r_tag", match="COLES", category="", tags=["weekly"], priority=50)
    categorising = Rule(id="r_cat", match="COLES", category="Groceries", priority=1)

    decision = cat.categorise("COLES", [tagging, categorising], count_hit=True)

    assert decision.category == "Groceries"
    assert decision.tags == ["weekly"]
    assert tagging.hit_count == 1
    assert categorising.hit_count == 1


def test_a_tag_only_rule_does_not_clear_an_existing_category_on_a_re_run():
    txn = Transaction(id="t1", date=date(2026, 10, 1), amount=-10.0,
                      description="COLES", account="a_1", category="Groceries",
                      category_source="llm")
    store = _memory_store(txn)

    cat.recategorise_all(store, rules=[Rule(id="r_tag", match="COLES",
                                            category="", tags=["weekly"])])

    assert txn.category == "Groceries"
    assert txn.tags == ["weekly"]


def test_hit_count_only_moves_when_a_rule_actually_files_a_row():
    rule = Rule(id="r", match="COLES", category="Groceries")
    rules = [rule]

    cat.categorise("COLES", rules)
    assert rule.hit_count == 0

    decision = cat.categorise("COLES", rules, count_hit=True)
    assert rule.hit_count == 1
    assert decision.source == "rule"
    assert decision.confidence == 1.0


# --------------------------------------------------------------------- memory


def _memory_store(*transactions):
    return Store(doc=StoreDocument(transactions=list(transactions)),
                 settings=Settings(), data_dir=None)


def _txn(txn_id, category=None, source="", description="ACME PTY", merchant="ACME"):
    return Transaction(id=txn_id, date=date(2026, 10, 1), amount=-10.0,
                       description=description, merchant=merchant, account="a_1",
                       category=category, category_source=source)


def test_memory_needs_two_prior_rows_and_sixty_percent_agreement():
    store = _memory_store(_txn("p1", "Dining & Takeaway", "user"),
                          _txn("p2", "Dining & Takeaway", "user"),
                          _txn("t"))

    found = cat.memory_category(store.view(), "ACME PTY", "ACME", exclude_id="t")

    assert found is not None
    assert found.category == "Dining & Takeaway"
    assert found.confidence == 0.9
    assert found.occurrences == 2


def test_one_prior_row_is_not_memory():
    store = _memory_store(_txn("p1", "Dining & Takeaway", "user"), _txn("t"))
    assert cat.memory_category(store.view(), "ACME PTY", "ACME", exclude_id="t") is None


def test_a_split_merchant_is_a_question_not_a_memory():
    store = _memory_store(_txn("p1", "Dining & Takeaway", "user"),
                          _txn("p2", "Groceries", "user"),
                          _txn("t"))
    assert cat.memory_category(store.view(), "ACME PTY", "ACME", exclude_id="t") is None


def test_memory_ignores_the_row_being_classified():
    store = _memory_store(_txn("t", "Dining & Takeaway", "user"))
    assert cat.memory_category(store.view(), "ACME PTY", "ACME", exclude_id="t") is None


# --------------------------------------------------------------- subscriptions


def test_a_regular_merchant_no_seed_knows_is_proposed_as_a_subscription():
    store = _memory_store(
        Transaction(id="s1", date=date(2026, 1, 1), amount=-10.0,
                    description="ZORBULON", merchant="ZORBULON", account="a_1"),
        Transaction(id="s2", date=date(2026, 2, 1), amount=-10.0,
                    description="ZORBULON", merchant="ZORBULON", account="a_1"),
        Transaction(id="s3", date=date(2026, 3, 1), amount=-10.0,
                    description="ZORBULON", merchant="ZORBULON", account="a_1"),
    )

    found = cat.suggest_rules(store.view())

    zorbulon = next(s for s in found if s.merchant == "ZORBULON")
    assert zorbulon.category == "Subscriptions"


# ------------------------------------------------------- retro-applying one rule


def test_a_rule_reaches_unclaimed_and_model_set_rows_but_not_a_person():
    untouched = Transaction(id="t1", date=date(2026, 10, 1), amount=-10.0,
                            description="COLES", account="a_1")
    modelled = Transaction(id="t2", date=date(2026, 10, 2), amount=-11.0,
                           description="COLES", account="a_1",
                           category="Shopping", category_source="llm")
    personal = Transaction(id="t3", date=date(2026, 10, 3), amount=-12.0,
                           description="COLES", account="a_1",
                           category="Shopping", category_source="user")
    store = _memory_store(untouched, modelled, personal)
    rule = Rule(id="r", match="COLES", category="Groceries")

    changed = cat.apply_rule_to_history(store, rule)

    assert changed == 2
    assert untouched.category == "Groceries"
    assert modelled.category == "Groceries"
    assert personal.category == "Shopping"
    assert rule.hit_count == 2


def test_accepting_a_suggestion_without_a_category_is_refused(store):
    suggestion = cat.RuleSuggestion(merchant="ZORBULON", occurrences=2, total=20.0,
                                    category=None)
    with pytest.raises(ValueError):
        cat.accept_suggestion(store, suggestion)
    assert not any(rule.match == "ZORBULON" for rule in store.doc.rules)


def test_memory_ignores_provisional_guesses_and_transfers():
    provisional = _memory_store(
        Transaction(id="p1", date=date(2026, 10, 1), amount=-10.0,
                    description="ACME PTY", merchant="ACME", account="a_1",
                    category="Housing", category_source="llm", needs_review=True),
        Transaction(id="p2", date=date(2026, 10, 2), amount=-10.0,
                    description="ACME PTY", merchant="ACME", account="a_1",
                    category="Housing", category_source="llm", needs_review=True),
        _txn("t"))
    transfer = _memory_store(
        Transaction(id="p1", date=date(2026, 10, 1), amount=-10.0,
                    description="ACME PTY", merchant="ACME", account="a_1",
                    category="Transfers", category_source="detected"),
        Transaction(id="p2", date=date(2026, 10, 2), amount=-10.0,
                    description="ACME PTY", merchant="ACME", account="a_1",
                    category="Transfers", category_source="detected"),
        _txn("t"))

    assert cat.memory_category(provisional.view(), "ACME PTY", "ACME",
                               exclude_id="t") is None
    assert cat.memory_category(transfer.view(), "ACME PTY", "ACME",
                               exclude_id="t") is None


def test_a_legacy_hand_set_category_is_not_overwritten_by_a_rule():
    legacy = Transaction(id="t1", date=date(2026, 10, 1), amount=-10.0,
                         description="COLES", account="a_1", category="Mine")
    store = _memory_store(legacy)

    changed = cat.apply_rule_to_history(
        store, Rule(id="r", match="COLES", category="Groceries"))

    assert changed == 0
    assert legacy.category == "Mine"


def test_coverage_by_category_does_not_count_transfers_as_spending():
    store = _memory_store(
        Transaction(id="t1", date=date(2026, 10, 1), amount=-50.0,
                    description="COLES", account="a_1", category="Groceries"),
        Transaction(id="t2", date=date(2026, 10, 2), amount=-100.0,
                    description="TO SAVINGS", account="a_1", category="Transfers"),
    )

    assert cat.coverage_by_category(store.view()) == {"Groceries": 50.0}


def test_memory_ignores_the_unfiled_bucket():
    store = _memory_store(
        _txn("p1", "Uncategorised", "bank"),
        _txn("p2", "Uncategorised", "bank"),
        _txn("t"))

    assert cat.memory_category(store.view(), "ACME PTY", "ACME",
                               exclude_id="t") is None


def test_recategorise_corrects_automatic_categories_but_not_a_person():
    automatic = [
        Transaction(id="l", date=date(2026, 10, 1), amount=-10.0, description="COLES",
                    account="a_1", category="Shopping", category_source="llm"),
        Transaction(id="m", date=date(2026, 10, 2), amount=-10.0, description="COLES",
                    account="a_1", category="Shopping", category_source="memory"),
        Transaction(id="b", date=date(2026, 10, 3), amount=-10.0, description="COLES",
                    account="a_1", category="Shopping", category_source="bank"),
    ]
    protected = [
        Transaction(id="u", date=date(2026, 10, 4), amount=-10.0, description="COLES",
                    account="a_1", category="Shopping", category_source="user"),
        Transaction(id="h", date=date(2026, 10, 5), amount=-10.0, description="COLES",
                    account="a_1", category="Shopping"),
    ]
    store = _memory_store(*automatic, *protected)

    cat.recategorise_all(store, rules=[Rule(id="r", match="COLES",
                                            category="Groceries")])

    assert [txn.category for txn in store.doc.transactions[:3]] == \
        ["Groceries", "Groceries", "Groceries"]
    assert [txn.category for txn in store.doc.transactions[3:]] == \
        ["Shopping", "Shopping"]


def test_retro_applying_a_rule_closes_the_questions_it_answers():
    from finance_tool.engine import review

    store = _memory_store(Transaction(
        id="t1", date=date(2026, 10, 1), amount=-10.0, description="COLES",
        account="a_1"))
    txn = store.view().transaction("t1")
    review.queue(store, txn)
    assert review.open_count(store.view()) == 1

    cat.apply_rule_to_history(store, Rule(id="r", match="COLES",
                                          category="Groceries"))

    assert review.open_count(store.view()) == 0
    assert txn.category == "Groceries"
    assert txn.needs_review is False
