"""Upgrading a store written by the previous schema (§12).

The promise is narrow and testable: an old database gains the columns its entities now
have, keeps every row, and is copied before anything is changed. A read-only open must
not migrate at all.
"""

import sqlite3

import pytest

from finance_tool.store import db as dbm
from finance_tool.store.store import Store

# What Stage 0 added. The fixture writes today's schema and then removes exactly these,
# so it is a real previous-version store rather than a hand-copied guess at one.
STAGE0_COLUMNS = {
    "accounts": ("provider", "masked", "balance_as_of"),
    "transactions": (
        "merchant", "note", "category_source", "category_confidence",
        "needs_review", "import_id", "status", "value_date", "currency",
    ),
    "rules": ("name", "match_type", "amount_min", "amount_max", "active", "hit_count"),
}
STAGE0_TABLES = ("reviews", "imports", "messages")


def _old_store(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / dbm.DB_NAME
    conn = sqlite3.connect(path)
    try:
        dbm.ensure_schema(conn)
        for table, columns in STAGE0_COLUMNS.items():
            for column in columns:
                conn.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        for table in STAGE0_TABLES:
            conn.execute(f'DROP TABLE "{table}"')
        conn.execute(
            "INSERT INTO transactions "
            "(id, date, amount, description, account, category, tags, fingerprint, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_old", "2026-09-01", -12.5, "Old row", "a_1", "Groceries", "[]", "fp",
             "csv"),
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()
    return path


def test_an_old_store_gains_its_columns_and_keeps_its_rows(tmp_path):
    _old_store(tmp_path)

    store = Store.load(tmp_path)

    assert [t.description for t in store.doc.transactions] == ["Old row"]
    row = store.doc.transactions[0]
    assert row.merchant is None            # absence stays absence
    assert row.status == "posted"          # a required field takes its dataclass default
    assert row.needs_review is False
    assert row.category_confidence is None
    assert any("upgraded" in note for note in store.notes)
    store.close()


def test_the_old_database_is_copied_before_it_is_changed(tmp_path):
    _old_store(tmp_path)

    Store.load(tmp_path).close()

    backups = list((tmp_path / "backups").iterdir())
    assert backups, "a migration must leave the previous state behind"
    assert any("pre-migration" in path.name for path in backups)


def test_migrating_twice_does_nothing_the_second_time(tmp_path):
    path = _old_store(tmp_path)

    Store.load(tmp_path).close()
    first = {p.name for p in (tmp_path / "backups").iterdir()}

    again = Store.load(tmp_path)
    again.close()

    assert {p.name for p in (tmp_path / "backups").iterdir()} == first
    assert not any("upgraded" in note for note in again.notes)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
    finally:
        conn.close()


def test_a_new_store_is_stamped_with_the_current_version(tmp_path):
    store = Store.load(tmp_path)
    store.close()

    conn = sqlite3.connect(tmp_path / dbm.DB_NAME)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == dbm.SCHEMA_VERSION
        columns = {row[1] for row in conn.execute('PRAGMA table_info("transactions")')}
        assert {"merchant", "status", "value_date", "category_source"} <= columns
    finally:
        conn.close()


def test_a_failed_migration_is_not_treated_as_corruption(tmp_path, monkeypatch):
    """A failed upgrade must stop, loudly, without moving the user's store aside."""
    path = _old_store(tmp_path)
    before = path.read_bytes()

    def explode(connection):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(dbm, "upgrade_schema", explode)

    with pytest.raises(dbm.StoreDatabaseError, match="could not be upgraded"):
        Store.load(tmp_path)

    assert path.exists()
    assert path.read_bytes() == before
    assert list((tmp_path / "backups").glob("corrupt-store-*")) == []


def test_a_read_only_open_of_an_old_store_leaves_it_alone(tmp_path):
    path = _old_store(tmp_path)
    before = path.read_bytes()

    store = Store.load(tmp_path, create=False)

    assert any("older" in note for note in store.notes)
    assert path.read_bytes() == before
    assert not (tmp_path / "backups").exists()
    assert store.doc.transactions[0].status == "posted"   # defaults still apply
    store.close()
