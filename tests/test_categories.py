"""What a category means, and why transfers are not spending (§24)."""

from datetime import date

from finance_tool.engine import analytics as an
from finance_tool.engine import categories as cats
from finance_tool.engine import ledger as led
from finance_tool.store.entities import Settings, StoreDocument, Transaction
from finance_tool.store.store import Store

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


def test_the_taxonomy_classifies_what_it_claims_to():
    assert cats.kind_of("Salary") == cats.INCOME
    assert cats.kind_of("Groceries") == cats.EXPENSE
    assert cats.kind_of(cats.TRANSFERS) == cats.TRANSFER
    assert cats.kind_of(cats.CREDIT_CARD_PAYMENT) == cats.TRANSFER
    # A user-typed category is spending: counting it keeps a total honest.
    assert cats.kind_of("My Own Thing") == cats.EXPENSE
    assert cats.kind_of(None) == cats.EXPENSE


def test_the_taxonomy_keeps_its_parents():
    assert cats.parent_of("Fuel") == "Transport"
    assert cats.parent_of("Groceries") is None
    assert "Fuel" in cats.children_of("Transport")
    assert "Transport" in cats.top_level()


def _view():
    document = StoreDocument(transactions=[
        Transaction(id="t_out", date=date(2026, 10, 2), amount=-100.0,
                    description="to savings", category=cats.TRANSFERS),
        Transaction(id="t_in", date=date(2026, 10, 2), amount=100.0,
                    description="from everyday", category=cats.TRANSFERS),
        Transaction(id="t_food", date=date(2026, 10, 3), amount=-50.0,
                    description="coles", category="Groceries", tags=["weekly"]),
    ])
    return Store(doc=document, settings=Settings()).view()


def test_a_transfer_is_neither_income_nor_spending():
    totals = led.actual_totals(_view(), OCTOBER)

    assert totals.income == 0.0
    assert totals.expenses == 50.0
    assert totals.count == 1


def test_transfers_do_not_appear_in_spend_breakdowns():
    view = _view()

    assert an.spend_by_category(view, OCTOBER) == [("Groceries", 50.0)]
    assert an.spend_by_tag(view, OCTOBER) == [("weekly", 50.0)]
    assert dict(an.daily_spend(view, OCTOBER))[date(2026, 10, 2)] == 0.0
    assert [row.category for row in led.budget_vs_actual(view, OCTOBER)] == ["Groceries"]


def test_coverage_is_measured_over_real_spending_only():
    coverage = led.budget_coverage(_view(), OCTOBER)

    assert coverage.total_count == 1
    assert coverage.total_value == 50.0
    assert coverage.categorised_value == 50.0
