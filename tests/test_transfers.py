"""Transfers between the user's own accounts (§24)."""

from datetime import date

from finance_tool.engine import ledger
from finance_tool.engine import review
from finance_tool.engine import transfers
from finance_tool.store.entities import Account, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def _txn(txn_id, when, amount, account, description="MOVEMENT", **kwargs):
    return Transaction(id=txn_id, date=when, amount=amount, account=account,
                       description=description, **kwargs)


def _store(*transactions):
    return Store(doc=StoreDocument(
        accounts=[Account(id="a_1", name="Everyday"), Account(id="a_2", name="Savings")],
        transactions=list(transactions),
    ), settings=Settings(), data_dir=None)


def test_an_equal_and_opposite_pair_across_accounts_is_filed_as_a_transfer():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", "TO SAVINGS")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2", "FROM EVERYDAY")
    store = _store(debit, credit)

    pairs = transfers.detect_transfers(store.view())
    assert len(pairs) == 1
    assert pairs[0].amount == 100.0
    assert pairs[0].day_gap == 1

    assert transfers.apply_transfers(store) == 1

    assert debit.category == "Transfers"
    assert credit.category == "Transfers"
    assert debit.category_source == credit.category_source == "detected"
    totals = ledger.actual_totals(store.view(), OCTOBER)
    assert totals.expenses == 0.0 and totals.income == 0.0


def test_pairing_finds_the_largest_set_not_the_first_fit():
    # First-fit takes d1-c0 and d2-c1, leaving d0(c) unable to use c2(c). The maximum is
    # d1-c0, d2-c2, d0-c1 — three pairs, not two.
    debit_one = _txn("d1", date(2026, 10, 2), -50.0, "c")
    debit_two = _txn("d2", date(2026, 10, 3), -50.0, "a")
    debit_three = _txn("d0", date(2026, 10, 4), -50.0, "c")
    credit_zero = _txn("c0", date(2026, 10, 2), 50.0, "b")
    credit_one = _txn("c1", date(2026, 10, 2), 50.0, "b")
    credit_two = _txn("c2", date(2026, 10, 4), 50.0, "c")
    store = _store(debit_one, debit_two, debit_three,
                   credit_zero, credit_one, credit_two)

    pairs = transfers.detect_transfers(store.view())

    assert len(pairs) == 3
    assert sorted(pair.debit_id for pair in pairs) == ["d0", "d1", "d2"]


def test_the_same_account_is_not_a_transfer():
    store = _store(_txn("d", date(2026, 10, 1), -100.0, "a_1"),
                   _txn("c", date(2026, 10, 2), 100.0, "a_1"))

    assert transfers.detect_transfers(store.view()) == []


def test_different_amounts_and_too_wide_a_window_do_not_pair():
    no_amount = _store(_txn("d", date(2026, 10, 1), -100.0, "a_1"),
                       _txn("c", date(2026, 10, 2), 90.0, "a_2"))
    assert transfers.detect_transfers(no_amount.view()) == []

    too_late = _store(_txn("d", date(2026, 10, 1), -100.0, "a_1"),
                      _txn("c", date(2026, 10, 5), 100.0, "a_2"))
    assert transfers.detect_transfers(too_late.view()) == []


def test_a_person_set_category_is_never_overwritten():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", "TO SAVINGS",
                 category="Groceries", category_source="user")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2", "FROM EVERYDAY")
    store = _store(debit, credit)

    assert transfers.detect_transfers(store.view()) == []
    assert transfers.apply_transfers(store) == 0
    assert debit.category == "Groceries"


def test_a_rule_set_category_is_never_overwritten():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", category="Housing",
                 category_source="rule")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2")
    store = _store(debit, credit)

    assert transfers.apply_transfers(store) == 0
    assert debit.category == "Housing"


def test_one_credit_cannot_pair_two_debits():
    debit_one = _txn("d1", date(2026, 10, 1), -50.0, "a_1")
    debit_two = _txn("d2", date(2026, 10, 2), -50.0, "a_1")
    credit = _txn("c", date(2026, 10, 2), 50.0, "a_2")
    store = _store(debit_one, debit_two, credit)

    pairs = transfers.detect_transfers(store.view())
    assert len(pairs) == 1
    transfers.apply_transfers(store, pairs)
    assert sorted(txn.id for txn in store.doc.transactions
                  if txn.category == "Transfers") == sorted([pairs[0].debit_id, "c"])


def test_applying_a_transfer_closes_any_question_about_the_rows():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2")
    store = _store(debit, credit)
    for txn in store.doc.transactions:
        review.queue(store, txn)

    transfers.apply_transfers(store)

    assert review.open_count(store.view()) == 0
    assert debit.needs_review is False and credit.needs_review is False


def test_the_counterpart_of_an_already_filed_transfer_is_still_filed():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", category="Transfers",
                 category_source="rule")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2")
    store = _store(debit, credit)

    pairs = transfers.detect_transfers(store.view())
    assert len(pairs) == 1
    assert transfers.apply_transfers(store, pairs) == 1

    assert debit.category_source == "rule"     # already filed, not rewritten
    assert credit.category == "Transfers"
    totals = ledger.actual_totals(store.view(), OCTOBER)
    assert totals.income == 0.0 and totals.expenses == 0.0


def test_three_days_pairs_and_four_does_not():
    three = _store(_txn("d", date(2026, 10, 1), -100.0, "a_1"),
                   _txn("c", date(2026, 10, 4), 100.0, "a_2"))
    four = _store(_txn("d", date(2026, 10, 1), -100.0, "a_1"),
                  _txn("c", date(2026, 10, 5), 100.0, "a_2"))

    assert len(transfers.detect_transfers(three.view())) == 1
    assert transfers.detect_transfers(four.view()) == []


def test_a_both_filed_pair_does_not_starve_an_unfiled_counterpart():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", category="Transfers",
                 category_source="detected")
    filed_credit = _txn("c1", date(2026, 10, 1), 100.0, "a_2", category="Transfers",
                        category_source="detected")
    unfiled_credit = _txn("c2", date(2026, 10, 2), 100.0, "a_2")
    store = _store(debit, filed_credit, unfiled_credit)

    pairs = transfers.detect_transfers(store.view())

    assert len(pairs) == 1
    assert pairs[0].credit_id == "c2"
    transfers.apply_transfers(store, pairs)
    assert unfiled_credit.category == "Transfers"


def test_a_pair_with_one_protected_side_files_neither():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", category="Groceries",
                 category_source="user")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2")
    store = _store(debit, credit)
    pair = transfers.TransferPair("d", "c", 100.0, 1, "a_1", "a_2")

    assert transfers.apply_transfers(store, [pair]) == 0
    assert debit.category == "Groceries"
    assert credit.category is None


def test_a_row_already_filed_as_a_transfer_is_not_paired_again():
    debit = _txn("d", date(2026, 10, 1), -100.0, "a_1", category="Transfers",
                 category_source="detected")
    credit = _txn("c", date(2026, 10, 2), 100.0, "a_2", category="Transfers",
                  category_source="detected")
    store = _store(debit, credit)

    assert transfers.detect_transfers(store.view()) == []
