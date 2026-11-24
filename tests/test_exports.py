"""Readable financial-year summaries and the assistant profile (§32)."""

from datetime import date

import pytest

from finance_tool.engine import exports
from finance_tool.store.entities import Account, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store, StoreError


def _txn(txn_id, when, amount, description, account="a_1", **kwargs):
    return Transaction(id=txn_id, date=when, amount=amount, description=description,
                       account=account, **kwargs)


def _store(tmp_path, *transactions):
    return Store(data_dir=tmp_path, doc=StoreDocument(
        accounts=[Account(id="a_1", name="Everyday"), Account(id="a_2", name="Savings")],
        transactions=list(transactions),
    ), settings=Settings())


def test_a_financial_year_runs_july_to_june():
    assert exports.financial_year(date(2026, 6, 30)) == "2025-26"
    assert exports.financial_year(date(2026, 7, 1)) == "2026-27"
    assert exports.fy_bounds("2026-27") == (date(2026, 7, 1), date(2027, 6, 30))


def test_the_summary_has_months_categories_and_merchants(tmp_path):
    store = _store(
        tmp_path,
        _txn("t1", date(2026, 7, 5), 2500.0, "SALARY ACME"),
        _txn("t2", date(2026, 7, 8), -100.0, "COLES NORTHLAND", category="Groceries"),
        _txn("t3", date(2026, 8, 2), -50.0, "CALTEX FUEL", category="Transport"),
        _txn("t4", date(2026, 8, 3), -100.0, "TO SAVINGS", account="a_1",
             category="Transfers"),
        _txn("t5", date(2026, 8, 3), 100.0, "FROM EVERYDAY", account="a_2",
             category="Transfers"),
    )

    markdown = exports.summary_markdown(store.view(), "2026-27")

    assert "# Financial year 2026-27" in markdown
    assert "## Monthly" in markdown
    assert "## Spending by category" in markdown
    assert "## Top merchants" in markdown
    assert "Groceries" in markdown
    assert "$100.00" in markdown
    assert "Coles Northland" in markdown
    assert "Transfers" not in markdown          # moving your own money is not spending
    assert "| August 2026 | $50.00 | $0.00 | -$50.00 |" in markdown


def test_export_year_writes_the_file_under_the_financial_year(tmp_path):
    store = _store(tmp_path, _txn("t1", date(2026, 7, 5), -10.0, "COLES"))
    out = tmp_path / "exports"

    path = exports.export_year(store, "2026-27", out_dir=out)

    assert path == out / "FY2026-27" / "summary.md"
    assert "Financial year 2026-27" in path.read_text(encoding="utf-8")


def test_export_all_covers_every_year_the_ledger_mentions(tmp_path):
    store = _store(
        tmp_path,
        _txn("t1", date(2026, 7, 5), -10.0, "COLES"),
        _txn("t2", date(2027, 8, 5), -20.0, "COLES"),
    )
    out = tmp_path / "exports"

    paths = exports.export_all(store, today=date(2027, 9, 1), out_dir=out)

    assert [path.parent.name for path in paths] == ["FY2026-27", "FY2027-28"]


def test_an_empty_ledger_exports_the_current_year(tmp_path):
    store = _store(tmp_path)

    paths = exports.export_all(store, today=date(2026, 11, 5), out_dir=tmp_path / "exports")

    assert [path.parent.name for path in paths] == ["FY2026-27"]


def test_a_scratch_store_refuses_to_export_into_the_app_folder():
    store = Store(doc=StoreDocument(), settings=Settings(), data_dir=None)

    with pytest.raises(StoreError):
        exports.export_all(store, today=date(2026, 11, 5))


def test_the_profile_is_created_once_and_never_overwritten(tmp_path):
    path = tmp_path / "profile.md"

    first = exports.ensure_profile(path)
    assert "About me" in first.read_text(encoding="utf-8")

    path.write_text("my own notes\n", encoding="utf-8")
    exports.ensure_profile(path)

    assert path.read_text(encoding="utf-8") == "my own notes\n"
