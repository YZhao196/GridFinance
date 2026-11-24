"""The persistent review queue (§24)."""

from datetime import date, datetime

from finance_tool.engine import review
from finance_tool.store.entities import Rule, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store

NOW = datetime(2026, 11, 5, 12, 0, 0)


def _store(*transactions):
    return Store(doc=StoreDocument(transactions=list(transactions)),
                 settings=Settings(), data_dir=None)


def _txn(txn_id="t1", description="MYSTERY SHOP", amount=-42.0, **kwargs):
    return Transaction(id=txn_id, date=kwargs.pop("date", date(2026, 10, 1)),
                       amount=amount, description=description, account="a_1", **kwargs)


def test_queue_asks_one_question_and_flags_the_row():
    store = _store(_txn())
    txn = store.view().transaction("t1")

    item = review.queue(store, txn, options=["Groceries", "Housing"], now=NOW)

    assert item.status == review.OPEN
    assert txn.needs_review is True
    assert "MYSTERY SHOP" in item.question
    assert "$42.00" in item.question
    assert item.options == ["Groceries", "Housing"]
    assert review.open_count(store.view()) == 1


def test_queue_replaces_the_question_rather_than_duplicating_it():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    first = review.queue(store, txn, question="first?", now=NOW)

    second = review.queue(store, txn, question="second?", reason="no_ai", now=NOW)

    assert second.id == first.id
    assert len(store.doc.reviews) == 1
    assert second.question == "second?"
    assert second.reason == "no_ai"


def test_resolving_with_a_category_sets_it_as_the_users():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    resolved = review.resolve(store, item.id, "Groceries", answer="weekly shop", now=NOW)

    assert resolved.status == review.RESOLVED
    assert resolved.answer == "weekly shop"
    assert txn.category == "Groceries"
    assert txn.category_source == "user"
    assert txn.category_confidence == 1.0
    assert txn.needs_review is False
    assert review.open_count(store.view()) == 0


def test_resolving_without_a_category_clears_the_flag_and_keeps_it_uncategorised():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    review.resolve(store, item.id, None, now=NOW)

    assert txn.category is None
    assert txn.needs_review is False
    assert review.open_count(store.view()) == 0


def test_a_null_answer_clears_a_provisional_model_guess():
    store = _store(_txn(category="Housing", category_source="llm",
                        category_confidence=0.4))
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    review.resolve(store, item.id, None, now=NOW)

    assert txn.category is None
    assert txn.category_source == ""
    assert txn.category_confidence is None
    assert txn.needs_review is False


def test_resolving_with_a_rule_creates_it_and_reaches_history():
    other = _txn("t2", description="MYSTERY SHOP 9988")
    store = _store(_txn(), other)
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    review.resolve(store, item.id, "Groceries", make_rule=True,
                   rule_pattern="MYSTERY SHOP", now=NOW)

    rule = next(r for r in store.doc.rules if r.match == "MYSTERY SHOP")
    assert rule.category == "Groceries"
    assert rule.match_type == "contains"
    assert other.category == "Groceries"
    assert other.category_source == "rule"
    assert other.rule_id == rule.id


def test_making_a_rule_from_a_merchant_matches_the_next_statement_line():
    store = _store(_txn("t1", "CARD PURCHASE 123456", merchant="Acme Pty Ltd"),
                   _txn("t2", "CARD PURCHASE 999999", date=date(2026, 10, 2),
                        merchant="Acme Pty Ltd"))
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    review.resolve(store, item.id, "Groceries", make_rule=True, now=NOW)

    rule = store.doc.rules[0]
    assert rule.match_type == "merchant"
    sibling = store.view().transaction("t2")
    assert sibling.category == "Groceries"
    assert sibling.rule_id == rule.id


def test_a_whitespace_answer_is_treated_as_no_category():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    review.resolve(store, item.id, "   ", now=NOW)

    assert txn.category is None
    assert txn.needs_review is False
    assert item.status == review.RESOLVED


def test_requeuing_the_same_question_does_not_bump_the_version_again():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    review.queue(store, txn, options=["A"], now=NOW)
    version = store.version

    review.queue(store, txn, options=["A"], now=NOW)

    assert store.version == version


def test_categorising_to_the_same_value_does_not_bump_the_version_again():
    store = _store(_txn())
    review.categorise_transaction(store, "t1", "Housing", now=NOW)
    version = store.version

    review.categorise_transaction(store, "t1", "Housing", now=NOW)

    assert store.version == version


def test_skip_clears_the_flag_and_leaves_the_row_uncategorised():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    assert review.skip(store, item.id, now=NOW) is True

    assert item.status == review.SKIPPED
    assert txn.needs_review is False
    assert txn.category is None


def test_skipping_does_not_enact_a_provisional_guess():
    store = _store(_txn(category="Housing", category_source="llm",
                        category_confidence=0.4))
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)

    review.skip(store, item.id, now=NOW)

    assert txn.category is None
    assert txn.category_source == ""
    assert txn.category_confidence is None
    assert txn.needs_review is False


def test_resolving_a_closed_or_missing_item_returns_nothing():
    store = _store(_txn())
    assert review.resolve(store, "rv_missing", "Groceries") is None

    txn = store.view().transaction("t1")
    item = review.queue(store, txn, now=NOW)
    review.resolve(store, item.id, "Groceries", now=NOW)

    assert review.resolve(store, item.id, "Housing", now=NOW) is None


def test_categorising_a_row_closes_its_question():
    store = _store(_txn())
    txn = store.view().transaction("t1")
    review.queue(store, txn, now=NOW)

    review.categorise_transaction(store, "t1", "Housing", source="user", now=NOW)

    assert txn.category == "Housing"
    assert txn.needs_review is False
    assert review.open_count(store.view()) == 0


def test_open_items_are_newest_first():
    store = _store(_txn("t1", date=date(2026, 9, 1)),
                   _txn("t2", date=date(2026, 10, 1)))
    for txn in store.doc.transactions:
        review.queue(store, txn, now=NOW)

    assert [item.transaction_id for item in review.open_items(store.view())] == \
        ["t2", "t1"]


def test_the_queue_survives_a_save_and_reload(tmp_path):
    store = Store(data_dir=tmp_path, doc=StoreDocument(transactions=[_txn()]),
                  settings=Settings())
    txn = store.view().transaction("t1")
    item = review.queue(store, txn, options=["A"], now=NOW)
    store.save()
    store.close()

    again = Store.load(tmp_path)

    assert review.open_count(again.view()) == 1
    assert again.doc.reviews[0].id == item.id
    assert again.doc.reviews[0].options == ["A"]
    assert again.view().transaction("t1").needs_review is True
    again.close()
