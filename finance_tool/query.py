"""Read-only SQL over the store, for the console and the assistant's last resort (§24).

The guarantee is SQLite's, not ours: the database is opened with ``mode=ro``, so a
statement that tried to write would be refused by the engine even if the text checks
missed it. The checks exist to give a clear answer instead of an engine error.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from finance_tool import paths
from finance_tool.store import db as dbm

READ_START = re.compile(r"^(select|with)\b", re.IGNORECASE)

# The SQLite authorizer is what actually enforces read-only: the text checks cannot tell
# "delete" the keyword from 'delete' inside a description the user is searching for.
_DENY_ACTIONS = {
    getattr(sqlite3, name) for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_ATTACH", "SQLITE_DETACH",
        "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT",
    ) if hasattr(sqlite3, name)
}
_DENY_FUNCTIONS = {"load_extension", "readfile", "writefile"}


def _authorizer(action, arg1, arg2, dbname, source):
    if action in _DENY_ACTIONS:
        return sqlite3.SQLITE_DENY
    if (action == sqlite3.SQLITE_FUNCTION
            and str(arg2 or "").lower() in _DENY_FUNCTIONS):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _strip_comments(text: str) -> str:
    """Remove SQL comments outside string literals, so a trailing comment cannot swallow
    the closing parenthesis the LIMIT wrapper adds."""
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    out.append(text[index + 1])
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
            out.append(char)
        elif text.startswith("--", index):
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end == -1 else end + 2
            continue
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _has_separator(text: str) -> bool:
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == ";":
            return True
        index += 1
    return False

SCHEMA_DOC = """
items(id, parent_id, name, type, amount, start, end, category, tags, note, subscription)
accounts(id, name, kind, balance, currency, external_id, provider, masked, balance_as_of)
transactions(id, date, amount, description, account, category, tags, merchant, note,
             category_source, category_confidence, needs_review, external_id, source,
             status, value_date, rule_id, import_id)
rules(id, match, category, tags, priority, name, match_type, amount_min, amount_max,
      active, hit_count)
goals(id, name, target, saved), tracker(id, name, quantity, per_use_amount, purchased_on)
people(id, name, email, phone), snapshots(date, value)
reviews(id, transaction_id, question, reason, status, suggested_category, resolved_category)
imports(id, filename, source_type, account, rows_total, rows_new, rows_duplicate, status)
messages(id, role, content, thinking)

Amounts are decimal dollars: transactions are negative for money out, positive for money
in. Dates are ISO strings (YYYY-MM-DD), so they compare lexicographically.
""".strip()


def run_query(sql: str, *, path: Path | None = None, limit: int = 200) -> dict[str, Any]:
    text = _strip_comments((sql or "").strip()).strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise ValueError("Empty query.")
    if _has_separator(text):
        raise ValueError("Run one statement at a time.")
    if not READ_START.match(text):
        raise ValueError("Only SELECT and WITH queries are allowed.")

    target = Path(path) if path is not None else paths.DATA_DIR / dbm.DB_NAME
    if not target.exists():
        raise ValueError(f"there is no store at {target}")
    connection = dbm.connect(target, create=False)
    try:
        connection.set_authorizer(_authorizer)
        cap = max(1, min(int(limit), 5000))
        cursor = connection.execute(f"SELECT * FROM ({text}) LIMIT {cap}")
        columns = [description[0] for description in cursor.description]
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.DatabaseError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        connection.close()
    return {"columns": columns, "rows": rows, "count": len(rows)}
