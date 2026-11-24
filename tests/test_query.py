"""The read-only SQL console (§24)."""

from datetime import date

import pytest

from finance_tool import query
from finance_tool.assistant.tools import execute_tool
from finance_tool.store.entities import Account, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store


def _store(tmp_path):
    store = Store(data_dir=tmp_path, doc=StoreDocument(
        accounts=[Account(id="a_1", name="Everyday", kind="transaction",
                          balance=100.0, currency="AUD")],
        transactions=[
            Transaction(id="t1", date=date(2026, 10, 1), amount=-10.0,
                        description="COLES", account="a_1"),
            Transaction(id="t2", date=date(2026, 10, 2), amount=-20.0,
                        description="CALTEX", account="a_1"),
        ],
    ), settings=Settings())
    store.save()
    return store


def test_a_select_returns_columns_and_rows(tmp_path):
    store = _store(tmp_path)

    result = query.run_query(
        "SELECT id, description FROM transactions ORDER BY date", path=store.db_path)

    assert result["columns"] == ["id", "description"]
    assert result["rows"] == [{"id": "t1", "description": "COLES"},
                              {"id": "t2", "description": "CALTEX"}]
    assert result["count"] == 2
    store.close()


def test_sql_keywords_inside_literals_or_functions_are_allowed(tmp_path):
    store = _store(tmp_path)

    result = query.run_query(
        "SELECT replace(description, 'COLES', 'SHOP') AS label "
        "FROM transactions WHERE description != 'create update delete'",
        path=store.db_path)

    assert result["rows"] == [{"label": "SHOP"}, {"label": "CALTEX"}]
    store.close()


def test_the_limit_is_clamped(tmp_path):
    store = _store(tmp_path)

    result = query.run_query("SELECT id FROM transactions ORDER BY date",
                             path=store.db_path, limit=1)

    assert result["count"] == 1
    assert result["rows"] == [{"id": "t1"}]
    store.close()


@pytest.mark.parametrize("sql", [
    "",
    "DELETE FROM transactions",
    "SELECT 1; SELECT 2",
    "PRAGMA user_version",
    "UPDATE transactions SET amount = 0",
])
def test_only_a_single_read_statement_is_accepted(tmp_path, sql):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        query.run_query(sql, path=store.db_path)
    store.close()


def test_literals_comments_and_pragma_functions_are_read_as_written(tmp_path):
    store = _store(tmp_path)

    literal = query.run_query("SELECT 'a;b' AS label -- a trailing comment",
                              path=store.db_path)
    assert literal["rows"] == [{"label": "a;b"}]

    info = query.run_query("SELECT name FROM pragma_table_info('transactions')",
                           path=store.db_path)
    assert "amount" in {row["name"] for row in info["rows"]}
    store.close()


def test_a_missing_store_is_an_error_not_an_empty_result(tmp_path):
    with pytest.raises(ValueError):
        query.run_query("SELECT 1", path=tmp_path / "nothing.db")


def test_the_assistant_tool_wraps_the_query(tmp_path):
    store = _store(tmp_path)

    result = execute_tool(store, "query_sql",
                          {"sql": "SELECT id FROM transactions ORDER BY date"},
                          today=date(2026, 11, 5))

    assert '"count": 2' in result
    store.close()


def test_a_scratch_store_cannot_query(tmp_path):
    store = Store(doc=StoreDocument(), settings=Settings(), data_dir=None)

    result = execute_tool(store, "query_sql", {"sql": "SELECT 1"},
                          today=date(2026, 11, 5))

    assert "no database file" in result
