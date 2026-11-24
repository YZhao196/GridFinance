"""The staged categoriser: rules, memory, then an optional classifier (§24)."""

from datetime import date, datetime

from finance_tool.engine import categorise as cat
from finance_tool.engine import classify
from finance_tool.engine import review
from finance_tool.store.entities import Rule, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store

NOW = datetime(2026, 11, 5, 12, 0, 0)


class FakeClassifier:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = []
        self.categories = []

    def classify(self, transactions, categories):
        self.calls.append(transactions)
        self.categories = categories
        if self.error:
            raise classify.ClassifyError(self.error)
        return self.results


def _txn(txn_id, description, amount=-20.0, merchant=None, **kwargs):
    return Transaction(id=txn_id, date=kwargs.pop("date", date(2026, 10, 1)),
                       amount=amount, description=description, merchant=merchant,
                       account=kwargs.pop("account", "a_1"), **kwargs)


def _store(*transactions, rules=(), **settings):
    return Store(doc=StoreDocument(transactions=list(transactions), rules=list(rules)),
                 settings=Settings(**settings), data_dir=None)


def test_rules_and_memory_decide_before_anything_is_asked():
    prior_a = _txn("p1", "ACME PTY", -10, merchant="ACME", category="Dining & Takeaway",
                   category_source="user")
    prior_b = _txn("p2", "ACME PTY", -11, merchant="ACME", category="Dining & Takeaway",
                   category_source="user")
    store = _store(
        _txn("t_rule", "COLES 1234"),
        _txn("t_memory", "ACME PTY", -12, merchant="ACME"),
        _txn("t_unknown", "ZORBULON"),
        prior_a, prior_b,
        rules=[Rule(id="r_coles", match="COLES", category="Groceries")],
    )

    stats = classify.classify_pending(store, now=NOW)

    assert stats.rules == 1
    assert stats.memory == 1
    assert stats.queued == 1
    assert store.view().transaction("t_rule").category == "Groceries"
    assert store.view().transaction("t_memory").category == "Dining & Takeaway"
    assert store.view().transaction("t_memory").category_source == "memory"
    unknown = store.view().transaction("t_unknown")
    assert unknown.category is None
    assert unknown.needs_review is True
    assert review.open_count(store.view()) == 1
    assert store.doc.reviews[0].reason == "no_ai"
    assert store.doc.rules[0].hit_count == 1


def test_a_rule_applied_by_the_classifier_keeps_its_identity():
    store = _store(_txn("t1", "COLES"),
                   rules=[Rule(id="r_coles", match="COLES", category="Groceries",
                               tags=["weekly"])])

    classify.classify_pending(store, now=NOW)

    txn = store.view().transaction("t1")
    assert txn.rule_id == "r_coles"
    assert txn.tags == ["weekly"]

    store.doc.rules[0].category = "Housing"
    cat.recategorise_all(store, rules=store.doc.rules, now=NOW)
    assert txn.category == "Housing"


def test_the_classifier_may_not_choose_a_transfer_or_the_unfiled_bucket():
    store = _store()

    options = classify.category_options(store.view())

    assert "Groceries" in options
    assert "Transfers" not in options
    assert "Credit Card Payment" not in options
    assert "Uncategorised" not in options


def test_a_confident_classifier_answer_is_applied():
    store = _store(_txn("t1", "ZORBULON"))
    fake = FakeClassifier([classify.Classification(0, "Housing", 0.9)])

    stats = classify.classify_pending(store, classifier=fake, now=NOW)

    txn = store.view().transaction("t1")
    assert stats.ai == 1
    assert txn.category == "Housing"
    assert txn.category_source == "llm"
    assert txn.needs_review is False
    assert review.open_count(store.view()) == 0
    assert fake.calls and "Housing" in fake.categories


def test_a_low_confidence_answer_is_a_suggestion_and_a_question():
    store = _store(_txn("t1", "ZORBULON"))
    fake = FakeClassifier([classify.Classification(0, "Housing", 0.4)])

    stats = classify.classify_pending(store, classifier=fake, now=NOW)

    txn = store.view().transaction("t1")
    assert stats.queued == 1
    assert txn.category == "Housing"          # visible, but provisional
    assert txn.category_source == "llm"
    assert txn.needs_review is True
    item = store.doc.reviews[0]
    assert item.reason == "low_confidence"
    assert item.suggested_category == "Housing"
    assert item.suggested_confidence == 0.4


def test_a_classifier_failure_queues_the_rows_instead_of_losing_them():
    store = _store(_txn("t1", "ZORBULON"))
    fake = FakeClassifier(error="provider is down")

    stats = classify.classify_pending(store, classifier=fake, now=NOW)

    assert stats.error == "provider is down"
    assert stats.queued == 1
    assert store.doc.reviews[0].reason == "ai_error"
    assert store.view().transaction("t1").needs_review is True


class BoomClassifier:
    def classify(self, transactions, categories):
        raise RuntimeError("network down")


def test_any_classifier_exception_queues_the_chunk():
    store = _store(_txn("t1", "ZORBULON"))

    stats = classify.classify_pending(store, classifier=BoomClassifier(), now=NOW)

    assert stats.error == "network down"
    assert stats.queued == 1
    assert store.doc.reviews[0].reason == "ai_error"
    assert store.view().transaction("t1").needs_review is True


def test_an_unknown_category_from_the_classifier_is_not_used():
    store = _store(_txn("t1", "ZORBULON"))
    fake = FakeClassifier([classify.Classification(0, "Not A Category", 0.99)])

    classify.classify_pending(store, classifier=fake, now=NOW)

    assert store.view().transaction("t1").category is None
    assert store.doc.reviews[0].reason == "unknown"


def test_a_queued_row_is_retried_when_a_classifier_appears():
    store = _store(_txn("t1", "ZORBULON"))
    classify.classify_pending(store, now=NOW)
    assert store.doc.reviews[0].reason == "no_ai"

    fake = FakeClassifier([classify.Classification(0, "Housing", 0.9)])
    stats = classify.classify_pending(store, classifier=fake, now=NOW)

    assert stats.ai == 1
    assert store.view().transaction("t1").category == "Housing"
    assert review.open_count(store.view()) == 0


def test_a_question_answered_with_leave_it_is_not_asked_again():
    store = _store(_txn("t1", "ZORBULON"))
    classify.classify_pending(store, now=NOW)
    item = store.doc.reviews[0]
    review.resolve(store, item.id, None, now=NOW)

    stats = classify.classify_pending(store, now=NOW)

    assert stats.queued == 0
    assert stats.remaining == 0
    assert review.open_count(store.view()) == 0


class EchoClassifier:
    def __init__(self):
        self.calls = []

    def classify(self, transactions, categories):
        self.calls.append(len(transactions))
        return [classify.Classification(index, "Housing", 0.9)
                for index in range(len(transactions))]


def test_the_classifier_is_called_in_chunks_of_forty():
    store = _store(*[_txn(f"t{index}", f"MERCHANT {index}") for index in range(41)])
    fake = EchoClassifier()

    stats = classify.classify_pending(store, classifier=fake, now=NOW)

    assert fake.calls == [40, 1]
    assert stats.ai == 41
    assert all(txn.category == "Housing" for txn in store.doc.transactions)


def test_remaining_counts_what_is_still_unanswered():
    store = _store(_txn("t1", "ZORBULON"), _txn("t2", "MYSTERY"))

    stats = classify.classify_pending(store, now=NOW)

    assert stats.remaining == 0          # both are queued, not silently unclassified
    assert review.open_count(store.view()) == 2
