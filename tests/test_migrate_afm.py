"""The one-way AFM migration, against a synthetic AFM database."""

import base64
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from finance_tool.engine import categorise as cat
from finance_tool.migrate_afm import AFM_PRIORITY_BASE, MigrationError, main, migrate
from finance_tool.store.entities import (
    ChatMessage,
    Item,
    Settings,
    StoreDocument,
    Transaction,
)
from finance_tool.store.secrets import load_secrets
from finance_tool.store.store import Store

SCHEMA = """
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL DEFAULT 'import', external_id TEXT,
    name TEXT NOT NULL, account_type TEXT NOT NULL DEFAULT 'transaction',
    currency TEXT NOT NULL DEFAULT 'AUD', bsb TEXT, number_masked TEXT,
    current_balance_cents INTEGER, available_balance_cents INTEGER, balance_as_of TEXT,
    is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, external_id));
CREATE TABLE categories (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, parent_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'expense');
CREATE TABLE imports (
    id INTEGER PRIMARY KEY, filename TEXT NOT NULL, file_hash TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL, account_id INTEGER, rows_total INTEGER DEFAULT 0,
    rows_new INTEGER DEFAULT 0, rows_duplicate INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok', error TEXT, imported_at TEXT DEFAULT (datetime('now')));
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, external_id TEXT,
    fingerprint TEXT NOT NULL, posted_date TEXT NOT NULL, value_date TEXT,
    description TEXT NOT NULL, merchant TEXT, amount_cents INTEGER NOT NULL,
    currency TEXT DEFAULT 'AUD', running_balance_cents INTEGER, direction TEXT,
    category_id INTEGER, category_source TEXT, category_confidence REAL,
    needs_review INTEGER DEFAULT 0, status TEXT DEFAULT 'posted', notes TEXT,
    import_id INTEGER, created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')), UNIQUE (account_id, fingerprint));
CREATE TABLE rules (
    id INTEGER PRIMARY KEY, name TEXT, match_type TEXT DEFAULT 'contains',
    pattern TEXT NOT NULL, amount_min_cents INTEGER, amount_max_cents INTEGER,
    category_id INTEGER NOT NULL, priority INTEGER DEFAULT 100,
    is_active INTEGER DEFAULT 1, hit_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE review_items (
    id INTEGER PRIMARY KEY, transaction_id INTEGER NOT NULL UNIQUE,
    question TEXT NOT NULL, reason TEXT DEFAULT 'low_confidence', options_json TEXT,
    suggested_category_id INTEGER, status TEXT DEFAULT 'open', answer TEXT,
    resolved_category_id INTEGER, created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT);
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT, tool_calls_json TEXT,
    created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _secret(value: str) -> str:
    return "plain:" + base64.b64encode(value.encode()).decode()


def _afm_db(tmp_path: Path) -> Path:
    directory = tmp_path / "afm" / "data"
    directory.mkdir(parents=True)
    path = directory / "finance.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO categories (id, name, kind) VALUES (1, 'Groceries', 'expense')")
    conn.execute("INSERT INTO categories (id, name, kind) VALUES (2, 'Income', 'income')")
    conn.execute("INSERT INTO categories (id, name, kind) VALUES (3, 'Transfers', 'transfer')")
    conn.execute(
        "INSERT INTO accounts (id, source, external_id, name, account_type, currency, "
        "number_masked, current_balance_cents, balance_as_of) "
        "VALUES (1, 'import', 'ext-1', 'Everyday', 'transaction', 'AUD', '****1234', "
        "100000, '2026-10-31')")
    conn.execute(
        "INSERT INTO imports (id, filename, file_hash, source_type, account_id, rows_total, "
        "rows_new, imported_at) VALUES (1, 'oct.csv', 'hash-1', 'csv', 1, 2, 2, "
        "'2026-10-02 09:00:00')")
    transactions = [
        (1, 1, "x1", "f1", "2026-10-01", None, "COLES", "Coles", -1000, 1, "rule", 1.0, 0,
         "posted", "weekly", 1),
        (2, 1, None, "f2", "2026-10-01", None, "COLES", "Coles", -1000, 1, "rule", 1.0, 0,
         "posted", None, 1),
        (3, 1, None, "f3", "2026-10-02", "2026-10-03", "SALARY ACME", None, 2000, 2, "llm",
         0.4, 1, "pending", None, None),
    ]
    for (txn_id, account, external, fingerprint, posted, value, description, merchant,
         amount, category, source, confidence, review, status, notes, import_id) in transactions:
        conn.execute(
            "INSERT INTO transactions (id, account_id, external_id, fingerprint, posted_date, "
            "value_date, description, merchant, amount_cents, category_id, category_source, "
            "category_confidence, needs_review, status, notes, import_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-02 09:00:00')",
            (txn_id, account, external, fingerprint, posted, value, description, merchant,
             amount, category, source, confidence, review, status, notes, import_id))
    conn.execute(
        "INSERT INTO rules (id, name, match_type, pattern, amount_min_cents, "
        "amount_max_cents, category_id, priority, is_active, hit_count) "
        "VALUES (1, 'Coles', 'contains', 'COLES', 100, 5000, 1, 100, 1, 5)")
    conn.execute(
        "INSERT INTO rules (id, name, match_type, pattern, category_id, priority) "
        "VALUES (2, 'Salary', 'regex', '^SAL', 2, 5)")
    conn.execute(
        "INSERT INTO review_items (id, transaction_id, question, reason, options_json, "
        "suggested_category_id, status) VALUES (1, 3, 'What was this?', 'low_confidence', "
        "'[\"Income\", \"Groceries\"]', 2, 'open')")
    conn.execute("INSERT INTO chat_messages (id, role, content) VALUES (1, 'user', 'hello')")
    conn.execute(
        "INSERT INTO chat_messages (id, role, content, tool_calls_json) VALUES "
        "(2, 'assistant', '', '{\"tool_calls\":[{\"id\":\"c1\",\"name\":\"get_finance_context\","
        "\"arguments\":{}}]}')")
    conn.execute(
        "INSERT INTO chat_messages (id, role, content, tool_calls_json) VALUES "
        "(3, 'tool', '{}', '{\"tool_call_id\":\"c1\",\"name\":\"get_finance_context\","
        "\"is_error\":false}')")
    settings = {
        "llm.provider": "anthropic",
        "llm.model": "model-x",
        "llm.effort": "high",
        "ai.writes": "0",
        "feed.provider": "redbark",
        "onboarded": "1",
        "secret:llm:anthropic": _secret("key-a"),
        "secret:feed:redbark": _secret("key-r"),
    }
    conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", settings.items())
    conn.execute("UPDATE transactions SET currency = 'USD' WHERE id = 3")
    conn.commit()
    conn.close()
    (directory / "profile.md").write_text("# My profile\n", encoding="utf-8")
    return path


def test_the_migration_moves_everything_and_balances(tmp_path):
    source = _afm_db(tmp_path)
    before = source.read_bytes()
    destination = tmp_path / "gf"

    report = migrate(source, destination)

    assert report.balanced is True
    assert (report.accounts, report.transactions, report.rules, report.reviews,
            report.imports, report.messages) == (1, 3, 2, 1, 1, 3)
    assert source.read_bytes() == before          # read-only on the source

    store = Store.load(destination)
    try:
        account = store.doc.accounts[0]
        assert account.name == "Everyday"
        assert account.provider == "import"
        assert account.balance == 1000.0
        assert account.masked == "****1234"
        assert account.balance_as_of == date(2026, 10, 31)

        assert len(store.doc.transactions) == 3
        coles = [txn for txn in store.doc.transactions if txn.description == "COLES"]
        assert len(coles) == 2
        assert len({txn.fingerprint for txn in coles}) == 2
        assert all(txn.category == "Groceries" for txn in coles)
        assert all(txn.merchant == "Coles" for txn in coles)
        assert all(txn.import_id == store.doc.imports[0].id for txn in coles)
        assert coles[0].note == "weekly"
        assert all(txn.currency == "AUD" for txn in coles)

        salary = next(txn for txn in store.doc.transactions if "SALARY" in txn.description)
        assert salary.category == "Income"
        assert salary.category_source == "llm"
        assert salary.needs_review is True
        assert salary.status == "pending"
        assert salary.value_date == date(2026, 10, 3)
        assert salary.currency == "USD"

        rules = {rule.name: rule for rule in store.doc.rules}
        assert rules["Coles"].priority == AFM_PRIORITY_BASE - 100
        assert rules["Salary"].priority == AFM_PRIORITY_BASE - 5
        assert rules["Coles"].amount_min == 1.0
        assert rules["Coles"].amount_max == 50.0
        assert rules["Salary"].match_type == "regex"

        review = store.doc.reviews[0]
        assert review.status == "open"
        assert review.suggested_category == "Income"
        assert review.options == ["Income", "Groceries"]

        messages = store.doc.messages
        assert [message.role for message in messages] == ["user", "assistant", "tool"]
        assert messages[1].tool_calls[0]["name"] == "get_finance_context"
        assert messages[2].tool_calls[0]["tool_call_id"] == "c1"

        assert store.settings.ai_provider == "anthropic"
        assert store.settings.ai_model == "model-x"
        assert store.settings.ai_writes is False
        assert store.settings.feed_provider == "redbark"
        assert store.settings.enable_bank_sync is True
        assert store.settings.onboarded is True
    finally:
        store.close()

    secrets = load_secrets(destination / "secrets.json")
    assert secrets.llm_anthropic == "key-a"
    assert secrets.feed_redbark == "key-r"
    assert (destination / "profile.md").read_text(encoding="utf-8") == "# My profile\n"


def test_a_dry_run_writes_nothing(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"

    report = migrate(source, destination, dry_run=True)

    assert report.balanced is True
    assert report.transactions == 3
    assert report.dry_run is True
    assert not destination.exists()


def test_a_destination_with_data_is_refused_without_force(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    existing = Store(data_dir=destination, doc=StoreDocument(
        transactions=[Transaction(id="t1", date=date(2026, 1, 1), amount=-1.0,
                                  description="mine", account="a")],
        items=[Item(id="d1", name="Rent")],
    ), settings=Settings())
    existing.save()
    existing.close()

    with pytest.raises(MigrationError, match="already has a GridFinance store"):
        migrate(source, destination)


def test_force_backs_up_and_replaces(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    existing = Store(data_dir=destination, doc=StoreDocument(
        transactions=[Transaction(id="t1", date=date(2026, 1, 1), amount=-1.0,
                                  description="mine", account="a")]),
        settings=Settings())
    existing.save()
    existing.close()

    report = migrate(source, destination, force=True)

    assert report.backup
    assert Path(report.backup).exists()
    store = Store.load(destination)
    try:
        assert len(store.doc.transactions) == 3
        assert not any(txn.description == "mine" for txn in store.doc.transactions)
    finally:
        store.close()


def test_an_unreadable_transaction_stops_the_migration_before_writing(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET posted_date = 'not-a-date' WHERE id = 3")
    conn.commit()
    conn.close()
    destination = tmp_path / "gf"

    with pytest.raises(MigrationError, match="nothing was written"):
        migrate(source, destination)

    assert not (destination / "store.db").exists()


def test_a_destination_holding_only_chat_counts_as_data(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    existing = Store(data_dir=destination, doc=StoreDocument(
        messages=[ChatMessage(id="m1", role="user", content="hello")]),
        settings=Settings())
    existing.save()
    existing.close()

    with pytest.raises(MigrationError, match="already has a GridFinance store"):
        migrate(source, destination)


def test_offsetting_unreadable_rows_still_stop_the_migration(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET account_id = 99, amount_cents = 5000 WHERE id = 1")
    conn.execute("UPDATE transactions SET account_id = 99, amount_cents = -5000 WHERE id = 2")
    conn.commit()
    conn.close()
    destination = tmp_path / "gf"

    with pytest.raises(MigrationError, match="nothing was written"):
        migrate(source, destination)

    assert not (destination / "store.db").exists()


def test_the_destination_must_be_outside_the_afm_tree(tmp_path):
    source = _afm_db(tmp_path)

    with pytest.raises(MigrationError, match="outside"):
        migrate(source, source.parent)
    with pytest.raises(MigrationError, match="outside"):
        migrate(source, source.parent / "gf")


def test_equal_priority_rules_keep_their_afm_order(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO rules (id, name, match_type, pattern, category_id, priority) "
        "VALUES (3, 'Coles Online', 'contains', 'COLES ONLINE', 1, 100)")
    conn.commit()
    conn.close()
    destination = tmp_path / "gf"

    migrate(source, destination)

    store = Store.load(destination)
    try:
        ordered = cat.order_rules([rule for rule in store.doc.rules
                                   if rule.priority == AFM_PRIORITY_BASE - 100])
        assert [rule.name for rule in ordered] == ["Coles", "Coles Online"]
    finally:
        store.close()


def test_extreme_afm_priorities_keep_their_order(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("DELETE FROM rules")
    conn.execute(
        "INSERT INTO rules (id, name, match_type, pattern, category_id, priority) "
        "VALUES (3, 'Late', 'contains', 'X', 1, 3000)")
    conn.execute(
        "INSERT INTO rules (id, name, match_type, pattern, category_id, priority) "
        "VALUES (4, 'Early', 'contains', 'Y', 1, 2000)")
    conn.commit()
    conn.close()
    destination = tmp_path / "gf"

    migrate(source, destination)

    store = Store.load(destination)
    try:
        migrated = [rule for rule in store.doc.rules if rule.name in {"Early", "Late"}]
        assert [rule.name for rule in cat.order_rules(migrated)] == ["Early", "Late"]
    finally:
        store.close()


def test_a_rule_pointing_at_a_missing_category_stops_everything(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute(
        "INSERT INTO rules (id, name, match_type, pattern, category_id, priority) "
        "VALUES (3, 'Orphan', 'contains', 'X', 99, 1)")
    conn.commit()
    conn.close()
    destination = tmp_path / "gf"

    with pytest.raises(MigrationError, match="could not be mapped"):
        migrate(source, destination)

    assert not (destination / "store.db").exists()


def test_force_replaces_old_secrets_and_profile(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    existing = Store(data_dir=destination, doc=StoreDocument(
        transactions=[Transaction(id="t1", date=date(2026, 1, 1), amount=-1.0,
                                  description="mine", account="a")]),
        settings=Settings())
    existing.save()
    existing.close()
    from finance_tool.store.secrets import Secrets, save_secrets

    save_secrets(Secrets(simplefin_access_url="old"),
                 destination / "secrets.json")
    (destination / "profile.md").write_text("old profile\n", encoding="utf-8")

    report = migrate(source, destination, force=True)

    secrets = load_secrets(destination / "secrets.json")
    assert secrets.simplefin_access_url is None
    assert secrets.llm_anthropic == "key-a"
    assert (destination / "profile.md").read_text(encoding="utf-8") == "# My profile\n"
    backup = Path(report.backup)
    assert (backup.parent / f"{backup.stem}-secrets.json").exists()
    assert (backup.parent / f"{backup.stem}-profile.md").exists()


def test_an_empty_database_is_not_an_afm_database(tmp_path):
    source = tmp_path / "afm" / "data" / "finance.db"
    source.parent.mkdir(parents=True)
    sqlite3.connect(source).close()

    with pytest.raises(MigrationError, match="does not look like"):
        migrate(source, tmp_path / "gf")


def test_a_malformed_amount_is_a_migration_error(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET amount_cents = 'not-a-number' WHERE id = 1")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="malformed"):
        migrate(source, tmp_path / "gf")


def test_a_live_write_ahead_log_stops_the_migration(tmp_path):
    source = _afm_db(tmp_path)
    Path(f"{source}-wal").write_bytes(b"pending rows")

    with pytest.raises(MigrationError, match="write-ahead log"):
        migrate(source, tmp_path / "gf")


def test_dry_run_reports_what_a_real_run_would_refuse(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET account_id = 99 WHERE id = 1")
    conn.commit()
    conn.close()

    report = migrate(source, tmp_path / "gf", dry_run=True)

    assert report.problems
    assert "could not be mapped" in report.problems[0]
    assert not (tmp_path / "gf").exists()


def test_a_foreign_destination_is_not_treated_as_empty(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    conn = sqlite3.connect(destination / "store.db")
    conn.execute("CREATE TABLE settings (value TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="already has a GridFinance store"):
        migrate(source, destination)


def test_force_replaces_secrets_and_profile_even_without_a_database(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    from finance_tool.store.secrets import Secrets, save_secrets

    save_secrets(Secrets(simplefin_access_url="old"),
                 destination / "secrets.json")
    (destination / "profile.md").write_text("old\n", encoding="utf-8")

    report = migrate(source, destination, force=True)

    assert report.imports == 1
    assert load_secrets(destination / "secrets.json").simplefin_access_url is None
    assert (destination / "profile.md").read_text(encoding="utf-8") == "# My profile\n"
    assert any((destination / "backups").glob("afm-pre-migration-*-secrets.json"))


def test_a_missing_category_is_warned_not_silent(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET category_id = 99 WHERE id = 1")
    conn.commit()
    conn.close()

    report = migrate(source, tmp_path / "gf")

    assert any("unknown category" in warning for warning in report.warnings)


def test_a_missing_source_is_refused(tmp_path):
    with pytest.raises(MigrationError, match="no AI Finance Manager database"):
        migrate(tmp_path / "missing.db", tmp_path / "gf")


def test_a_missing_optional_source_column_is_a_migration_error(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("ALTER TABLE accounts DROP COLUMN current_balance_cents")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="missing a column"):
        migrate(source, tmp_path / "gf")


def test_an_overflowing_amount_is_a_migration_error(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET amount_cents = 1e400 WHERE id = 1")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="malformed"):
        migrate(source, tmp_path / "gf")


def test_a_foreign_empty_store_is_not_overwritten_without_force(tmp_path):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    conn = sqlite3.connect(destination / "store.db")
    conn.execute("CREATE TABLE items (id TEXT, name TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="already has a GridFinance store"):
        migrate(source, destination)


def test_a_rule_with_amount_bounds_is_warned_about(tmp_path):
    source = _afm_db(tmp_path)

    report = migrate(source, tmp_path / "gf")

    assert any("signed" in warning and "magnitude" in warning
               for warning in report.warnings)


def test_a_missing_balance_is_warned_not_a_silent_zero(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("UPDATE accounts SET current_balance_cents = NULL")
    conn.commit()
    conn.close()
    destination = tmp_path / "gf"

    report = migrate(source, destination)

    assert any("no recorded balance" in warning for warning in report.warnings)
    store = Store.load(destination)
    try:
        assert store.doc.accounts[0].balance == 0.0
    finally:
        store.close()


def test_a_file_that_cannot_be_moved_leaves_the_old_store_intact(tmp_path, monkeypatch):
    source = _afm_db(tmp_path)
    destination = tmp_path / "gf"
    destination.mkdir()
    existing = Store(data_dir=destination, doc=StoreDocument(
        transactions=[Transaction(id="t1", date=date(2026, 1, 1), amount=-1.0,
                                  description="mine", account="a")]),
        settings=Settings())
    existing.save()
    existing.close()
    (destination / "profile.md").write_text("old profile\n", encoding="utf-8")

    from finance_tool import migrate_afm as mod

    real_replace = mod.os.replace

    def guarded(src, dst):
        if Path(dst).name == "profile.md":
            raise PermissionError("file is open")
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", guarded)

    with pytest.raises(MigrationError, match="Nothing has been changed"):
        migrate(source, destination, force=True)

    store = Store.load(destination)
    try:
        assert [txn.description for txn in store.doc.transactions] == ["mine"]
    finally:
        store.close()


def test_a_live_write_ahead_log_can_be_copied_when_allowed(tmp_path):
    source = _afm_db(tmp_path)
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode = wal")
    conn.execute("UPDATE transactions SET description = 'COLES LATE' WHERE id = 3")
    conn.commit()
    try:
        wal = Path(f"{source}-wal")
        assert wal.exists() and wal.stat().st_size > 0

        report = migrate(source, tmp_path / "gf", allow_live=True)
    finally:
        conn.close()

    assert any("write-ahead log" in warning for warning in report.warnings)
    assert report.transactions == 3


def test_the_cli_returns_one_on_refusal_and_on_dry_run_problems(tmp_path, capsys):
    assert main(["--afm-db", str(tmp_path / "missing.db"),
                 "--data-dir", str(tmp_path / "gf")]) == 1
    assert "migration refused" in capsys.readouterr().out

    source = _afm_db(tmp_path)
    assert main(["--afm-db", str(source), "--data-dir", str(tmp_path / "gf"),
                 "--dry-run"]) == 0

    conn = sqlite3.connect(source)
    conn.execute("UPDATE transactions SET account_id = 99 WHERE id = 1")
    conn.commit()
    conn.close()
    assert main(["--afm-db", str(source), "--data-dir", str(tmp_path / "gf"),
                 "--dry-run"]) == 1
