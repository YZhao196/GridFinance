"""The SQLite store (§8, §12).

One file, `data/store.db`, holding the same document the JSON store held. Three things
this buys over the JSON file, and they are the reason for the change:

* **A write touches one row.** The JSON store serialised the entire document on every
  edit; a spreadsheet over a decade of transactions wants to change a cell, not rewrite
  everything.
* **Queries are indexed.** Sorting and filtering a sheet is SQL, not a Python walk.
* **Atomicity is real.** A multi-row edit commits or does not happen, where before
  `tmp`+`rename` protected only the write itself.

The **schema is derived from the entities' own type hints**, so adding a field stays a
one-line change in `entities.py` — the same promise `codec.py` makes. Scalars that are
worth sorting, filtering or joining on become real columns; lists and dicts become JSON
columns, because they are per-occurrence bags keyed by a date and a table each would mean
several joins for a query nobody writes.

**Occurrences are still derived, never stored.** That is the single most important
property of this app's data model and nothing here changes it.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import types
import typing
from datetime import date, datetime
from pathlib import Path
from typing import Any, get_args, get_origin

from finance_tool.store.codec import decode_dataclass, encode
from finance_tool.store.entities import (
    Account,
    Canvas,
    Goal,
    Item,
    Person,
    Rule,
    Settings,
    Snapshot,
    TrackerItem,
    Transaction,
)

SCHEMA_VERSION = 1

DB_NAME = "store.db"

# Table name -> (dataclass, primary key). Order matters only for reading.
TABLES: dict[str, tuple[Any, str]] = {
    "items": (Item, "id"),
    "accounts": (Account, "id"),
    "transactions": (Transaction, "id"),
    "rules": (Rule, "id"),
    "goals": (Goal, "id"),
    "tracker": (TrackerItem, "id"),
    "people": (Person, "id"),
    "snapshots": (Snapshot, "date"),
}

SETTINGS_TABLE = "settings"
CANVAS_TABLE = "canvases"
META_TABLE = "meta"
GENERATION_KEY = "generation"

# How long to wait for a lock before giving up. The store lives inside the user's OneDrive
# folder, so a sync client holding the file for a second is an ordinary event, not a fault.
LOCK_TIMEOUT = 15

# `sqlite3.DatabaseError` covers both "this file is not a database" and "another process
# has it open". They need opposite responses — one is corruption to set aside, the other is
# a moment to wait through — and mistaking the second for the first moves the user's live
# store away and opens an empty one.
BUSY_WORDS = ("locked", "busy")

_SCALARS = {int: "INTEGER", bool: "INTEGER", float: "REAL"}


class StoreDatabaseError(Exception):
    """The database could not be opened, read or written. Never swallowed."""


class StoreBusyError(StoreDatabaseError):
    """Another process holds the store. Not corruption — and not something to quarantine."""


def is_busy(exc: Exception) -> bool:
    """Whether an error means "in use right now" rather than "unreadable"."""
    return any(word in str(exc).casefold() for word in BUSY_WORDS)


# --------------------------------------------------------------------------- schema


def _unwrap(tp: Any) -> Any:
    """The first non-``None`` member of a ``Union``, so ``date | None`` reads as ``date``."""
    if isinstance(tp, types.UnionType) or get_origin(tp) is typing.Union:
        for arg in get_args(tp):
            if arg is not type(None):
                return _unwrap(arg)
    return tp


def sql_type(tp: Any) -> str:
    """The column type for a field annotation. ``JSON`` means "stored as TEXT, loaded as a
    structure" — it is not a SQLite type, it is ours.

    A ``Literal`` of strings is a scalar: ``Item.type`` is *income* or *expense*, and that
    is exactly the kind of column a sheet wants to filter on.
    """
    base = _unwrap(tp)
    if base in _SCALARS:
        return _SCALARS[base]
    if base in (str, date, datetime):
        return "TEXT"
    if get_origin(base) is typing.Literal:
        if all(isinstance(arg, str) for arg in get_args(base)):
            return "TEXT"
    return "JSON"


def columns_for(cls: Any) -> list[tuple[str, str]]:
    """Column name and type for every field of an entity, in declaration order."""
    hints = typing.get_type_hints(cls)
    return [(f.name, sql_type(hints.get(f.name, Any))) for f in dataclasses.fields(cls)]


def create_table_sql(table: str, cls: Any, key: str, *, extra: tuple[str, ...] = ()) -> str:
    parts = [f'{key} TEXT PRIMARY KEY']
    parts += [f'"{name}" {kind}' for name, kind in columns_for(cls) if name != key]
    parts += list(extra)
    return f'CREATE TABLE IF NOT EXISTS "{table}" ({", ".join(parts)})'


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create every table and record the schema version in the one place SQLite keeps it.

    The version is only *stamped* on a database that has none. Writing it unconditionally
    would mean opening a store written by a newer build quietly relabelled it as this
    build's version — the opposite of leaving it alone.
    """
    for table, (cls, key) in TABLES.items():
        conn.execute(create_table_sql(table, cls, key))
    # `position` is the canvas order, which is not a field on Canvas — the list's order is.
    conn.execute(create_table_sql(CANVAS_TABLE, Canvas, "id", extra=("position INTEGER",)))
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{SETTINGS_TABLE}" '
                 '(id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)')
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" '
                 '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    if schema_version(conn) == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def read_generation(conn: sqlite3.Connection) -> int:
    """A counter bumped by every save, so two windows can tell they disagree.

    The store writes the whole document, so a second window's save silently overwrites
    anything the first has done since it loaded. The counter turns that into a refusal
    rather than a lost afternoon.
    """
    row = conn.execute(
        f'SELECT value FROM "{META_TABLE}" WHERE key = ?', (GENERATION_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def bump_generation(conn: sqlite3.Connection) -> int:
    current = read_generation(conn) + 1
    conn.execute(f'INSERT OR REPLACE INTO "{META_TABLE}" (key, value) VALUES (?, ?)',
                 (GENERATION_KEY, str(current)))
    return current


# ---------------------------------------------------------------------- row <-> entity


def _json_columns(cls: Any) -> set[str]:
    return {name for name, kind in columns_for(cls) if kind == "JSON"}


def to_row(cls: Any, entity: Any) -> dict[str, Any]:
    """An entity as a row: scalars as they are, structures as JSON text."""
    encoded = encode(entity)
    structured = _json_columns(cls)
    return {(name): (json.dumps(value) if name in structured else value)
            for name, value in encoded.items()}


def from_row(cls: Any, row: sqlite3.Row) -> Any:
    """A row as an entity, letting the codec turn ISO text back into dates."""
    structured = _json_columns(cls)
    data = {name: (json.loads(row[name]) if name in structured and row[name] is not None
                   else row[name])
            for name in row.keys()}
    return decode_dataclass(cls, data)


def read_all(conn: sqlite3.Connection, table: str) -> list[Any]:
    cls, _key = TABLES[table]
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return [from_row(cls, row) for row in rows]


def write_one(conn: sqlite3.Connection, table: str, entity: Any) -> None:
    cls, key = TABLES[table]
    row = to_row(cls, entity)
    names = list(row)
    placeholders = ", ".join("?" for _ in names)
    columns = ", ".join(f'"{name}"' for name in names)
    conn.execute(f'INSERT OR REPLACE INTO "{table}" ({columns}) VALUES ({placeholders})',
                 [row[name] for name in names])


def delete_one(conn: sqlite3.Connection, table: str, key_value: Any) -> bool:
    """Delete by primary key.

    The key is converted here rather than handed to SQLite as a ``date``: sqlite3's default
    date adapter is deprecated, and the value in the column is its ISO form anyway.
    """
    _cls, key = TABLES[table]
    if isinstance(key_value, (date, datetime)):
        key_value = key_value.isoformat()
    cursor = conn.execute(f'DELETE FROM "{table}" WHERE "{key}" = ?', (key_value,))
    return cursor.rowcount > 0


def read_canvases(conn: sqlite3.Connection) -> list[Canvas]:
    rows = conn.execute(f'SELECT * FROM "{CANVAS_TABLE}" ORDER BY position').fetchall()
    return [from_row(Canvas, row) for row in rows]


def write_canvases(conn: sqlite3.Connection, canvases: Any) -> None:
    """Rewrite the canvas order. Canvases are few and their order is the data, so the
    whole set is replaced rather than diffed row by row."""
    conn.execute(f'DELETE FROM "{CANVAS_TABLE}"')
    for position, canvas in enumerate(canvases):
        row = to_row(Canvas, canvas)
        names = list(row) + ["position"]
        values = [row[name] for name in row] + [position]
        columns = ", ".join(f'"{name}"' for name in names)
        placeholders = ", ".join("?" for _ in names)
        conn.execute(f'INSERT INTO "{CANVAS_TABLE}" ({columns}) VALUES ({placeholders})',
                     values)


def read_settings(conn: sqlite3.Connection) -> Settings | None:
    """The preferences, with the canvas list read back from its own table.

    Canvases live in `canvases` rather than inside this blob so that they can be ordered
    and indexed; keeping a second copy here would let the two drift apart.
    """
    row = conn.execute(f'SELECT data FROM "{SETTINGS_TABLE}" WHERE id = 1').fetchone()
    if row is None:
        return None
    settings = decode_dataclass(Settings, json.loads(row["data"]))
    settings.canvases = read_canvases(conn)
    return settings


def write_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    """Settings go in whole. There are nine of them and they are read together."""
    data = encode(settings)
    data["canvases"] = []          # the canvases table is the only copy (see read_settings)
    conn.execute(f'INSERT OR REPLACE INTO "{SETTINGS_TABLE}" (id, data) VALUES (1, ?)',
                 (json.dumps(data),))


# ------------------------------------------------------------------------ connection


def connect(path: Path | None, *, create: bool = True) -> sqlite3.Connection:
    """Open the database, or an in-memory one when there is no path.

    ``create=False`` opens read-only through a URI, which is a real guarantee rather than
    a flag we promise to respect: SQLite itself refuses the write. That is what makes
    ``--shot`` unable to alter the store it is photographing.
    """
    if path is None:
        conn = sqlite3.connect(":memory:")
    elif create:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT)
    else:
        if not path.exists():
            raise StoreDatabaseError(f"{path} does not exist")
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                               timeout=LOCK_TIMEOUT)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
