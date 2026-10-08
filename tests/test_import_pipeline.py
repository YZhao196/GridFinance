"""The Phase 2 gate (§35).

A fixture statement imports end to end; dedupe discards a re-imported file exactly; and
categorisation coverage clears §24's measured bar of 80% by count *and* by dollar value.
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from finance_tool.engine import categorise as cat
from finance_tool.engine import importers as imp
from finance_tool.engine import ledger as led
from finance_tool.store.entities import Account, Item, StoreDocument, EXPENSE, INCOME
from finance_tool.store.store import Store

from fixtures import build_store

STATEMENTS = Path(__file__).resolve().parent / "statements"
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
FIXED_NOW = datetime(2026, 10, 7, 12, 0, 0)


def statement(name: str) -> Path:
    return STATEMENTS / name


@pytest.fixture
def store():
    """A store with the seeded rules installed and one account to import into."""
    store = Store(
        doc=StoreDocument(
            accounts=[Account(id="a_everyday", name="Everyday", kind="transaction",
                              balance=0.0, currency="AUD")],
        ),
        data_dir=None,
        clock=lambda: FIXED_NOW,
    )
    cat.ensure_seed_rules(store)
    return store


# ------------------------------------------------------------------ end to end


def test_a_fixture_statement_imports_end_to_end(store):
    summary = imp.import_file(store, statement("statement_preamble.csv"),
                              account="a_everyday", now=FIXED_NOW)

    assert summary.read == 15
    assert summary.skipped == 0
    assert summary.imported == 15
    assert summary.duplicates == 0
    assert summary.categorised == 14
    assert summary.uncategorised == 1
    assert summary.describe() == "15 read, 15 new"

    assert len(store.doc.transactions) == 15
    assert store.doc.items == []          # an import never invents a plan item


def test_imported_rows_carry_their_source_and_import_time(store):
    imp.import_file(store, statement("statement_signed.csv"), account="a_everyday",
                    now=FIXED_NOW)

    txn = store.view().transactions[0]
    assert txn.source == "statement_signed.csv"
    assert txn.account == "a_everyday"
    assert txn.imported_at == FIXED_NOW
    assert txn.fingerprint


def test_imported_amounts_and_dates_match_the_statement(store):
    imp.import_file(store, statement("statement_signed.csv"), account="a_everyday",
                    now=FIXED_NOW)

    rows = sorted(store.view().transactions, key=lambda t: t.date)
    assert [(t.date, t.amount) for t in rows] == [
        (date(2026, 10, 1), -520.00),
        (date(2026, 10, 3), -4.50),
        (date(2026, 10, 5), -79.00),
    ]


def test_both_same_day_coffees_survive_the_whole_pipeline(store):
    """§21's named bug, checked where it would actually be felt."""
    imp.import_file(store, statement("statement_preamble.csv"), account="a_everyday",
                    now=FIXED_NOW)

    coffees = [t for t in store.view().transactions if t.description == "CAFE LATTE 0091"]
    assert len(coffees) == 3          # two on the 22nd, one elsewhere
    assert sorted(t.fingerprint for t in coffees) == sorted({t.fingerprint for t in coffees})


def test_importing_the_same_file_twice_changes_nothing(store):
    first = imp.import_file(store, statement("statement_preamble.csv"),
                            account="a_everyday", now=FIXED_NOW)
    version_after_first = store.version
    second = imp.import_file(store, statement("statement_preamble.csv"),
                             account="a_everyday", now=FIXED_NOW)

    assert first.imported == 15
    assert second.imported == 0
    assert second.duplicates == 15
    assert len(store.doc.transactions) == 15
    assert store.version == version_after_first       # nothing was written


def test_an_ofx_import_dedupes_on_the_aggregators_id(store):
    first = imp.import_file(store, statement("statement.ofx"), account="a_everyday",
                            now=FIXED_NOW)
    second = imp.import_file(store, statement("statement.ofx"), account="a_everyday",
                             now=FIXED_NOW)

    assert first.read == 5
    assert first.skipped == 1
    assert first.imported == 5
    assert second.imported == 0
    assert second.duplicates == 5


def test_a_partial_import_is_counted_and_shown(store):
    summary = imp.import_file(store, statement("statement_junk.csv"),
                              account="a_everyday", now=FIXED_NOW)

    assert summary.read == 2
    assert summary.skipped == 2
    assert summary.describe() == "2 read, 2 skipped, 2 new"
    assert len(summary.skipped_samples) == 2
    assert summary.skipped_samples[0].startswith("line 2:")


def test_an_import_into_the_fixture_store_keeps_the_ledger_intact():
    """Nothing about importing touches the plan — the two are peers, not inputs (§1)."""
    store = build_store()
    before = len(store.doc.items)
    imp.import_file(store, statement("statement_signed.csv"), account="a_everyday",
                    now=FIXED_NOW)
    assert len(store.doc.items) == before


# ---------------------------------------------------------- §24's coverage bar


def test_categorisation_coverage_clears_the_measured_bar(store):
    """§24: ≥80% categorised, by count *and* by dollar value, on a fixture import."""
    imp.import_file(store, statement("statement_preamble.csv"), account="a_everyday",
                    now=FIXED_NOW)

    coverage = led.budget_coverage(store.view(), SEPTEMBER)

    assert coverage.total_count == 14
    assert coverage.categorised_count == 13
    assert coverage.by_count == pytest.approx(13 / 14)
    assert coverage.by_value == pytest.approx((1172.59 - 15.00) / 1172.59)
    assert coverage.meets(0.80) is True


def test_the_uncovered_row_is_named_rather_than_hidden(store):
    imp.import_file(store, statement("statement_preamble.csv"), account="a_everyday",
                    now=FIXED_NOW)

    leftover = led.uncategorised_transactions(store.view(), SEPTEMBER)
    assert len(leftover) == 1
    assert leftover[0].description == "MAGAZINE SUB 3312"


def test_the_import_summary_and_the_engine_agree_on_coverage(store):
    summary = imp.import_file(store, statement("statement_preamble.csv"),
                              account="a_everyday", now=FIXED_NOW)
    coverage = led.budget_coverage(store.view(), SEPTEMBER)

    # The summary counts every row it categorised; the engine's coverage is a question
    # about spending, so the income row is not part of it.
    assert summary.categorised == 14
    assert coverage.categorised_count == 13
    assert summary.categorised - coverage.categorised_count == 1


def test_a_suggested_rule_closes_the_last_gap(store):
    imp.import_file(store, statement("statement_preamble.csv"), account="a_everyday",
                    now=FIXED_NOW)
    assert led.budget_coverage(store.view(), SEPTEMBER).meets(1.0) is False

    rule = cat.Rule(id="r_mag", match="MAGAZINE", category="Subscriptions", priority=10)
    store.doc.rules.append(rule)
    cat.recategorise_all(store, rules=store.doc.rules, overwrite=False)

    coverage = led.budget_coverage(store.view(), SEPTEMBER)
    assert coverage.by_count == 1.0
    assert coverage.by_value == 1.0
    assert coverage.meets(1.0) is True


def test_the_imported_statement_reconciles_against_a_plan():
    """The full loop: a plan, a statement, and the two meeting."""
    store = Store(
        doc=StoreDocument(
            accounts=[Account(id="a_everyday", name="Everyday", kind="transaction",
                              balance=0.0, currency="AUD")],
            items=[
                Item(id="d_rent", name="Rent", type=EXPENSE, amount=520.00,
                     start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
                     due="1", category="Rent"),
                Item(id="d_net", name="Internet", type=EXPENSE, amount=79.00,
                     start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=5"},
                     due="5", category="Utilities"),
                Item(id="d_salary", name="Salary", type=INCOME, amount=3200.00,
                     start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
                     category="Income"),
            ],
        ),
        data_dir=None,
        clock=lambda: FIXED_NOW,
    )
    cat.ensure_seed_rules(store)
    imp.import_file(store, statement("statement_signed.csv"), account="a_everyday",
                    now=FIXED_NOW)

    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert result.matched_count == 2           # rent and internet, both to the cent
    assert result.unmatched_count == 1         # the coffee has no plan line
    assert all(m.is_exact for m in result.matches)

    summary = led.plan_summary(store.view(), OCTOBER)
    assert summary.actual.expenses == pytest.approx(603.50)
    assert summary.plan.expenses == pytest.approx(599.00)   # rent 520 + internet 79


# ------------------------------------------------------------------ reconciliation


def plan_store(items, transactions=()):
    return Store(
        doc=StoreDocument(items=list(items), transactions=list(transactions)),
        data_dir=None,
    )


def rent_item(**kwargs):
    defaults = dict(
        id="d_rent", name="Rent", type=EXPENSE, amount=520.00, start=date(2026, 1, 1),
        recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}, due="1", category="Rent",
    )
    defaults.update(kwargs)
    return Item(**defaults)


def test_reconcile_itself_never_marks_anything():
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=-520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert result.matched_count == 1
    assert store.view().item("d_rent").paid == []


def test_applying_a_reconciliation_marks_the_occurrence_paid():
    """Asking and acting are separate calls: `reconcile` takes a read-only view, and
    `apply_reconciliation` takes the store so it can bump the version."""
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=-520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")

    version = store.version
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])
    marked = imp.apply_reconciliation(store, result)

    assert marked == 1
    assert store.version > version
    assert store.view().item("d_rent").paid == [date(2026, 10, 1)]


def test_applying_twice_marks_once():
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=-520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert imp.apply_reconciliation(store, result) == 1
    # Already paid, so the second attempt finds nothing to do and does not bump.
    version = store.version
    assert imp.apply_reconciliation(store, result) == 0
    assert store.version == version


def test_a_cent_out_is_still_a_match_but_a_dollar_out_is_not():
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=-520.01,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    assert imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1]).matched_count == 1

    store2 = plan_store([rent_item()])
    imp.import_rows(store2, [imp.RawTxn(date=date(2026, 10, 1), amount=-521.00,
                                        description="RENT PAYMENT")],
                    source="s.csv", account="a")
    assert imp.reconcile(store2.view(), start=OCTOBER[0], end=OCTOBER[1]).matched_count == 0


def test_a_transaction_outside_the_window_does_not_match():
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 20), amount=-520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    assert imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1]).matched_count == 0


def test_a_transaction_inside_the_window_matches_and_scores_by_distance():
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 6), amount=-520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert result.matched_count == 1
    assert result.matches[0].occurrence == date(2026, 10, 1)
    # Five days out, so a probable match rather than a certain one — the row can say so
    # instead of asserting a payment nobody made on the first.
    assert result.matches[0].is_exact is False


def test_an_already_paid_occurrence_is_not_matched_again():
    store = plan_store([rent_item(paid=[date(2026, 10, 1)])])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=-520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert result.matched_count == 0
    assert result.unmatched_count == 1


def test_two_transactions_cannot_both_claim_one_occurrence():
    store = plan_store([rent_item()])
    imp.import_rows(store, [
        imp.RawTxn(date=date(2026, 10, 1), amount=-520.00, description="RENT PAYMENT"),
        imp.RawTxn(date=date(2026, 10, 1), amount=-520.00, description="RENT PAYMENT"),
    ], source="s.csv", account="a")
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert result.matched_count == 1
    assert result.unmatched_count == 1


def test_the_category_can_carry_the_match_when_the_merchant_names_differ():
    store = plan_store([rent_item(name="Weekly rent")])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=-520.00,
                                       description="AGENT DISBURSEMENT")],
                    source="s.csv", account="a",
                    rules=[cat.Rule(id="r", match="AGENT", category="Rent")])
    assert imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1]).matched_count == 1


def test_unpaid_occurrences_are_listed_back():
    store = plan_store([rent_item()])
    result = imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1])

    assert result.matches == []
    assert result.unmatched_transactions == []
    assert (("d_rent", date(2026, 10, 1)) in result.unpaid_occurrences) is True


def test_income_transactions_are_not_reconciled_against_expense_items():
    store = plan_store([rent_item()])
    imp.import_rows(store, [imp.RawTxn(date=date(2026, 10, 1), amount=520.00,
                                       description="RENT PAYMENT")],
                    source="s.csv", account="a")
    assert imp.reconcile(store.view(), start=OCTOBER[0], end=OCTOBER[1]).matched_count == 0


# ------------------------------------------------------------------ rolled-up view


def test_reports_from_several_accounts_roll_into_one():
    rolled = imp.rolled_up_import_summaries([
        imp.ImportSummary(source="sync", read=10, skipped=1, imported=9, duplicates=1,
                          categorised=7, uncategorised=2),
        imp.ImportSummary(source="sync", read=4, skipped=0, imported=4, duplicates=0,
                          categorised=4, uncategorised=0),
    ])

    assert rolled.read == 14
    assert rolled.skipped == 1
    assert rolled.imported == 13
    assert rolled.duplicates == 1
    assert rolled.categorised == 11
    assert rolled.uncategorised == 2
    assert rolled.total == 15


def test_the_coverage_of_an_import_with_nothing_in_it_is_complete():
    assert imp.ImportSummary().coverage == 1.0
    assert imp.ImportSummary().is_empty is True
