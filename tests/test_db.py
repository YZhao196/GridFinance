"""The SQLite layer itself: schema, mapping, and what the engine guarantees.

These test `store/db.py` directly rather than through `Store`, because two of its promises
are about the *schema* — that it is derived from the entities, and that a column is real
where it matters — and neither is visible from the store's public API.
"""

import dataclasses
import sqlite3
import types
import typing
from datetime import date, datetime

import pytest

from finance_tool.store import db as dbm
from finance_tool.store.entities import (
    Account,
    Canvas,
    Goal,
    Item,
    Person,
    PlacedWidget,
    Rule,
    Settings,
    Snapshot,
    StoreDocument,
    TrackerItem,
    Transaction,
    EXPENSE,
)

from fixtures import build_document, build_settings


@pytest.fixture
def conn():
    connection = dbm.connect(None)
    dbm.ensure_schema(connection)
    yield connection
    connection.close()


# --------------------------------------------------------------------------- schema


def test_the_schema_comes_from_the_entities(conn):
    """Adding a field to an entity is still a one-line change: the column follows it.

    That is the same promise `codec.py` makes for JSON, and it is why the schema is derived
    rather than written out by hand — a hand-written schema drifts the first time someone
    adds a field in a hurry.
    """
    stored = {name for name, _kind in dbm.columns_for(Item)}
    assert stored == {f.name for f in dataclasses.fields(Item)}

    columns = {row["name"] for row in conn.execute('PRAGMA table_info("items")')}
    assert columns == stored | {"id"}


def test_a_scalar_is_a_real_column_and_a_structure_is_json():
    """The hybrid choice, stated as a test: what you would sort or filter on is a column."""
    kinds = dict(dbm.columns_for(Item))

    assert kinds["name"] == "TEXT"
    assert kinds["amount"] == "REAL"
    assert kinds["start"] == "TEXT"           # a date is its ISO form
    assert kinds["priority"] == "INTEGER"
    assert kinds["subscription"] == "INTEGER"  # a bool is 0 or 1
    assert kinds["type"] == "TEXT"             # a Literal of strings is still a scalar
    assert kinds["recurrence"] == "JSON"
    assert kinds["overrides"] == "JSON"
    assert kinds["tags"] == "JSON"
    assert kinds["shared"] == "JSON"


def test_a_literal_of_strings_is_stored_as_a_scalar_not_a_structure():
    """`Item.type` is *income* or *expense* — exactly the column a sheet wants to filter on."""
    assert dbm.sql_type(typing.Literal["income", "expense"]) == "TEXT"
    assert dbm.sql_type(typing.Literal[1, 2]) == "JSON"     # not a string: no column for it


def test_an_optional_unwraps_to_its_inner_type():
    assert dbm.sql_type(date | None) == "TEXT"
    assert dbm.sql_type(float | None) == "REAL"
    assert dbm.sql_type(list[str] | None) == "JSON"


def test_every_collection_has_a_table(conn):
    tables = {row["name"] for row in
              conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(dbm.TABLES) <= tables
    assert {"settings", "canvases"} <= tables


# -------------------------------------------------------------------- round tripping


@pytest.mark.parametrize("table", sorted(dbm.TABLES))
def test_every_entity_round_trips_through_its_table(conn, table):
    for entity in getattr(build_document(), table):
        dbm.write_one(conn, table, entity)
    conn.commit()
    assert dbm.read_all(conn, table) == getattr(build_document(), table)


def test_a_date_keyed_dictionary_survives_as_dates(conn):
    """`overrides` is keyed by date, and a JSON object can only have string keys. Coming
    back as strings would make every `date in item.overrides` check silently False."""
    original = next(i for i in build_document().items if i.id == "d_rent")

    dbm.write_one(conn, "items", original)
    conn.commit()
    back = dbm.read_all(conn, "items")[0]

    assert back.overrides == original.overrides
    assert all(isinstance(key, date) for key in back.overrides)
    assert back.paid == original.paid and back.cancelled == original.cancelled


def test_a_snapshot_is_keyed_by_its_date(conn):
    """A snapshot has no id; its date is the identity, and the mapping has to know that."""
    snapshot = Snapshot(date=date(2026, 9, 30), value=10400.0)

    dbm.write_one(conn, "snapshots", snapshot)
    conn.commit()

    assert dbm.read_all(conn, "snapshots") == [snapshot]
    assert dbm.delete_one(conn, "snapshots", date(2026, 9, 30)) is True
    assert dbm.read_all(conn, "snapshots") == []


def test_a_canvas_keeps_its_widgets_and_its_order(conn):
    canvases = [Canvas(id="c_b", name="Second", widgets=[
        PlacedWidget(widget="hero_pl", x=0, y=0, w=6, h=5, config={"k": "v"})]),
        Canvas(id="c_a", name="First", widgets=[])]

    dbm.write_canvases(conn, canvases)
    conn.commit()

    assert dbm.read_canvases(conn) == canvases          # order is the data


def test_settings_do_not_keep_a_second_copy_of_the_canvases(conn):
    """One source of truth, or the two drift and the canvas list becomes a coin toss."""
    settings = build_settings()
    dbm.write_canvases(conn, settings.canvases)
    dbm.write_settings(conn, settings)
    conn.commit()

    row = conn.execute('SELECT data FROM "settings" WHERE id = 1').fetchone()
    assert "c_" not in row["data"]                     # no canvas ids in the blob
    assert dbm.read_settings(conn) == settings         # but they come back


def test_settings_round_trip_when_there_are_none(conn):
    assert dbm.read_settings(conn) is None


# ------------------------------------------------------------------- versioning


def test_the_schema_version_is_stamped_once_and_not_overwritten(conn):
    assert dbm.schema_version(conn) == dbm.SCHEMA_VERSION

    conn.execute(f"PRAGMA user_version = {dbm.SCHEMA_VERSION + 3}")
    conn.commit()
    dbm.ensure_schema(conn)

    assert dbm.schema_version(conn) == dbm.SCHEMA_VERSION + 3


# ------------------------------------------------------------------ connections


def test_an_in_memory_connection_is_writable(conn):
    dbm.write_one(conn, "items", Item(id="d_x", name="x", type=EXPENSE, amount=1.0,
                                      start=date(2026, 1, 1)))
    conn.commit()
    assert len(dbm.read_all(conn, "items")) == 1


def test_a_read_only_connection_is_refused_by_sqlite_not_by_us(tmp_path):
    """The point of opening with a URI: the guarantee is the engine's, not a flag we have
    agreed to check before writing."""
    from fixtures import seed_store_dir

    seed_store_dir(tmp_path)
    connection = dbm.connect(tmp_path / dbm.DB_NAME, create=False)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO items (id) VALUES ('x')")
    finally:
        connection.close()


def test_a_read_only_open_of_a_missing_file_is_an_error_not_a_creation(tmp_path):
    missing = tmp_path / "nothing-here.db"

    with pytest.raises(dbm.StoreDatabaseError):
        dbm.connect(missing, create=False)
    assert not missing.exists()


def test_a_connection_is_not_left_holding_a_transaction(tmp_path):
    """An uncommitted write must not be visible, and closing must not lose a committed one."""
    path = tmp_path / "store.db"
    connection = dbm.connect(path)
    dbm.ensure_schema(connection)
    dbm.write_one(connection, "items", Item(id="d_a", name="a", type=EXPENSE, amount=1.0,
                                            start=date(2026, 1, 1)))
    connection.commit()
    connection.close()

    again = dbm.connect(path)
    try:
        assert [i.id for i in dbm.read_all(again, "items")] == ["d_a"]
    finally:
        again.close()


def test_a_document_written_and_read_back_is_identical(conn):
    """The whole point, in one assertion: nothing is lost between the objects and the file."""
    document = build_document()
    for table in dbm.TABLES:
        for entity in getattr(document, table):
            dbm.write_one(conn, table, entity)
    conn.commit()

    back = StoreDocument(schema_version=1)
    for table in dbm.TABLES:
        setattr(back, table, dbm.read_all(conn, table))

    assert back == document
