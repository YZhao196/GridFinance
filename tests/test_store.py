import json
import sqlite3
from datetime import date, datetime

import pytest

from finance_tool.store.entities import Canvas, Item, PlacedWidget, EXPENSE, INCOME, signed
from finance_tool.store.migrations import (
    MigrationError,
    SCHEMA_VERSION,
    document_version,
    needs_migration,
    run_migrations,
)
from finance_tool import paths
from finance_tool.store.store import Store, StoreError, atomic_write_json, new_id

from fixtures import (
    build_document,
    build_settings,
    build_store,
    corrupt_store_dir,
    seed_store_dir,
)


# --------------------------------------------------------------- round trip (§8)


def test_store_round_trips_a_fixture_document(tmp_path):
    """Phase 0's gate: the store round-trips a fixture document."""
    original = build_store(tmp_path)
    original.add_canvas("Dashboard")
    original.save()

    reloaded = Store.load(tmp_path)

    assert reloaded.doc == original.doc
    assert reloaded.settings == original.settings

    rent = next(i for i in reloaded.doc.items if i.id == "d_rent")
    assert rent.start == date(2026, 1, 1)
    assert rent.overrides == {date(2026, 11, 1): {"amount": 545.0}}
    assert [s.value for s in reloaded.doc.snapshots] == [9100.0, 9950.0, 10400.0]
    assert reloaded.view().transaction("t_07").fingerprint == "fp_cafe_0"
    assert reloaded.view().item("d_salary").amount == 3200.0


def test_save_writes_one_database_and_leaves_no_stray_files(tmp_path):
    store = build_store(tmp_path)
    store.save()

    assert (tmp_path / "store.db").exists()
    assert not (tmp_path / "store.json").exists()
    assert not (tmp_path / "settings.json").exists()
    assert [p.name for p in tmp_path.glob("*.tmp")] == []
    # Saving is a commit: what was written is what loads back.
    store.close()
    assert Store.load(tmp_path).doc.items


def test_atomic_write_replaces_rather_than_truncating(tmp_path):
    target = tmp_path / "thing.json"
    atomic_write_json(target, {"a": 1})
    atomic_write_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}
    assert list(tmp_path.glob("*.tmp")) == []


def test_version_is_a_monotonic_counter(tmp_path):
    """§28 memoises hooks on store.version, so it must move on every write."""
    store = build_store(tmp_path)
    store.save()
    first = store.version
    store.save()
    assert store.version == first + 1
    store.mark_paid("d_net", date(2026, 9, 5))
    assert store.version == first + 2


def test_a_store_with_no_directory_refuses_to_write_rather_than_escaping(tmp_path):
    """The bug this guards: defaulting a dir-less store to the app's data directory meant
    a throwaway store that saved wrote into the user's real data.

    The check is "nothing *changed*" rather than "nothing exists": the app's own store is
    the user's real data and legitimately lives there, so asserting its absence would make
    this fail on any machine that has ever run the app.
    """
    from finance_tool import paths

    before = sorted(p.name for p in paths.DATA_DIR.glob("*")) \
        if paths.DATA_DIR.exists() else None

    scratch = Store(doc=build_document(), settings=build_settings(), data_dir=None)

    assert scratch.has_directory is False
    assert scratch.store_path is None
    for action in (scratch.save, scratch.save_settings, scratch.backup):
        with pytest.raises(StoreError, match="has no directory"):
            action()

    after = sorted(p.name for p in paths.DATA_DIR.glob("*")) \
        if paths.DATA_DIR.exists() else None
    assert after == before


def test_only_load_puts_a_directory_in_play(tmp_path):
    """A constructed store has none; `load(data_dir)` is the one thing that gives it one.

    Note the test deliberately does not call the bare `Store.load()`: that resolves the
    app's own directory and creates it, which is precisely the side effect being avoided.
    """
    assert Store(doc=build_document(), settings=build_settings()).data_dir is None
    assert Store.load(tmp_path).data_dir == tmp_path


# ------------------------------------------------- reading without writing (§8, §34)


def test_a_read_only_load_does_not_create_the_directory(tmp_path):
    """The bug this guards: `--shot` pointed at the real data directory, so taking a
    screenshot created `data/` and seeded it — a picture that edited what it depicted."""
    missing = tmp_path / "not-yet"

    store = Store.load(missing, create=False)

    assert store.read_only is True
    assert not missing.exists()
    assert store.doc.items == [] and store.doc.transactions == []


def test_a_read_only_store_refuses_every_write(tmp_path):
    store = Store.load(tmp_path, create=False)

    for action in (store.save, store.save_settings, store.backup):
        with pytest.raises(StoreError, match="read-only"):
            action()
    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.db")) == []


def test_a_read_only_load_will_not_even_open_a_store_it_could_modify(tmp_path):
    """Read-only is enforced by SQLite, not by us remembering. A write is refused by the
    engine, so this is a guarantee rather than a promise."""
    seed_store_dir(tmp_path)

    store = Store.load(tmp_path, create=False)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store._conn.execute("INSERT INTO items (id) VALUES ('x')")


def test_a_read_only_load_of_a_corrupt_store_reports_without_moving_it(tmp_path):
    """Setting a bad file aside is a write. A read-only read still has to say what it found."""
    target = corrupt_store_dir(tmp_path)
    before = target.read_bytes()

    store = Store.load(tmp_path, create=False)

    assert target.read_bytes() == before
    assert not (tmp_path / "backups").exists()
    assert any("could not be read" in note for note in store.notes)


def test_a_read_only_load_reads_a_healthy_store(tmp_path):
    """Read-only means it cannot write — not that it cannot read."""
    seed_store_dir(tmp_path)

    store = Store.load(tmp_path, create=False)

    assert [item.id for item in store.doc.items] == \
        [item.id for item in build_document().items]


def test_missing_files_load_as_an_empty_store(tmp_path):
    store = Store.load(tmp_path)
    assert store.doc.items == []
    assert store.notes == []
    assert store.settings.currency == "AUD"


# --------------------------------------------------------------- corrupt input (§32)


def test_corrupt_store_is_set_aside_and_reported(tmp_path):
    target = corrupt_store_dir(tmp_path)

    store = Store.load(tmp_path)

    assert store.doc.items == []
    assert len(store.notes) == 1
    assert "could not be read" in store.notes[0]
    quarantined = list((tmp_path / "backups").glob("corrupt-store-*.db"))
    assert len(quarantined) == 1
    # The bad file is preserved rather than destroyed, and moved rather than copied: left
    # in place it would break every later open, so there would be nothing to recover from.
    assert quarantined[0].read_bytes() == b"this is not a database, whatever it says"
    assert target.read_bytes() != b"this is not a database, whatever it says"


def test_a_store_that_could_not_be_read_still_starts_and_can_be_written(tmp_path):
    """The app must come up and be usable after a bad file, not fail on every launch."""
    corrupt_store_dir(tmp_path)

    store = Store.load(tmp_path)
    store.add_item(Item(id="d_new", name="New", type=EXPENSE, amount=1.0,
                        start=date(2026, 1, 1)))
    store.save()
    store.close()

    assert [i.id for i in Store.load(tmp_path).doc.items] == ["d_new"]


# --------------------------------------------------------------- migration (§12)


def test_document_version_defaults_to_current_when_absent():
    assert document_version({}) == SCHEMA_VERSION
    assert needs_migration({}) is False


def test_document_version_rejects_nonsense():
    with pytest.raises(MigrationError):
        document_version({"schema_version": "one"})


def test_migrations_apply_in_sequence_and_do_not_mutate_the_input():
    original = {"schema_version": 1, "items": [{"id": "d_a"}]}
    table = {
        1: lambda doc: {**doc, "accounts": []},
        2: lambda doc: {**doc, "goals": []},
    }

    migrated = run_migrations(original, target=3, migrations=table)

    assert migrated["schema_version"] == 3
    assert migrated["accounts"] == []
    assert migrated["goals"] == []
    # The caller's dict is untouched, so a failure downstream cannot corrupt it.
    assert original == {"schema_version": 1, "items": [{"id": "d_a"}]}


def test_a_missing_migration_step_is_an_error_not_a_silent_skip():
    with pytest.raises(MigrationError, match="no migration registered"):
        run_migrations({"schema_version": 1}, target=3, migrations={1: lambda d: d})


def test_a_raising_migration_names_the_step():
    def explode(doc):
        raise ValueError("bad date")

    with pytest.raises(MigrationError, match="v1 -> v2 failed: bad date"):
        run_migrations({"schema_version": 1}, target=2, migrations={1: explode})


def test_a_migration_must_return_a_document():
    with pytest.raises(MigrationError, match="did not return a document"):
        run_migrations({"schema_version": 1}, target=2, migrations={1: lambda doc: None})


def test_the_schema_version_is_recorded_in_the_database(tmp_path):
    """SQLite keeps its own version, so it is one integer rather than a field inside a
    document that might not have been read yet."""
    from finance_tool.store import db as dbm

    seed_store_dir(tmp_path)
    connection = dbm.connect(tmp_path / dbm.DB_NAME)
    try:
        assert dbm.schema_version(connection) == dbm.SCHEMA_VERSION
    finally:
        connection.close()


def test_a_store_from_a_newer_build_is_refused_and_left_untouched(tmp_path):
    from finance_tool.store import db as dbm

    seed_store_dir(tmp_path)
    connection = dbm.connect(tmp_path / dbm.DB_NAME)
    connection.execute(f"PRAGMA user_version = {dbm.SCHEMA_VERSION + 5}")
    connection.commit()
    connection.close()

    store = Store.load(tmp_path)

    assert len(store.notes) == 1
    assert "newer than this build" in store.notes[0]
    # Left alone, not downgraded to something this build half-understands.
    connection = dbm.connect(tmp_path / dbm.DB_NAME)
    try:
        assert dbm.schema_version(connection) == dbm.SCHEMA_VERSION + 5
    finally:
        connection.close()


def test_backup_writes_a_database_that_loads(tmp_path):
    """A snapshot has to be restorable, not merely present. Copied bytes would not do:
    once a write-ahead log is in play, the file on its own is not the whole story."""
    store = build_store(tmp_path)
    store.save()
    target = store.backup(reason="manual")
    store.close()

    assert target is not None and target.exists() and target.suffix == ".db"
    restored = Store.load(target.parent.parent)
    assert [i.id for i in restored.doc.items] == [i.id for i in build_document().items]


# --------------------------------------------------------------- the read-only view (§28)


def test_view_exposes_no_mutation_api():
    store = build_store()
    view = store.view()
    for forbidden in ("add", "update", "remove", "save", "set_override", "mark_paid"):
        assert not hasattr(view, forbidden), f"StoreView must not expose {forbidden}()"


def test_view_lookups_and_helpers():
    view = build_store().view()

    assert view.item("d_rent").name == "Rent"
    assert view.item("nope") is None
    assert view.item_name("d_rent") == "Rent"
    assert view.person_name("p_alex") == "Alex"
    assert view.account_by_external("sf_savings").id == "a_savings"
    assert view.account_by_external("nope") is None
    assert {c.name for c in view.children_of("d_utilities")} == {"Electricity", "Water"}
    # 14 items, two of them children of the Utilities node.
    assert len(view.roots()) == 12
    assert [a.name for a in view.foreign_accounts()] == ["US Checking"]


def test_currency_defaults_to_the_settings_currency():
    store = build_store()
    store.settings.currency = "NZD"
    assert store.view().currency == "NZD"


# --------------------------------------------------------------- per-occurrence state (§9)


def test_set_and_clear_an_override():
    store = build_store()

    store.set_override("d_rent", date(2026, 12, 1), "amount", 560.0)
    assert store.view().item("d_rent").overrides[date(2026, 12, 1)] == {"amount": 560.0}

    store.clear_override("d_rent", date(2026, 12, 1), "amount")
    assert date(2026, 12, 1) not in store.view().item("d_rent").overrides


def test_clear_all_overrides_for_an_occurrence_leaves_other_occurrences_alone():
    store = build_store()
    store.set_override("d_rent", date(2026, 12, 1), "amount", 560.0)
    store.set_override("d_rent", date(2026, 12, 1), "note", "rise")

    store.clear_override("d_rent", date(2026, 12, 1))

    # The fixture's own November override is untouched: clearing December's is not a
    # licence to rewrite the series (§9).
    assert store.view().item("d_rent").overrides == {date(2026, 11, 1): {"amount": 545.0}}


def test_cancel_and_uncancel_an_occurrence():
    store = build_store()
    store.cancel_occurrence("d_net", date(2026, 12, 5))
    store.cancel_occurrence("d_net", date(2026, 12, 5))  # idempotent
    assert store.view().item("d_net").cancelled == [date(2026, 12, 5)]
    store.uncancel_occurrence("d_net", date(2026, 12, 5))
    assert store.view().item("d_net").cancelled == []


def test_mark_paid_is_idempotent_and_reversible():
    store = build_store()
    store.mark_paid("d_net", date(2026, 11, 5))
    store.mark_paid("d_net", date(2026, 11, 5))
    assert store.view().item("d_net").paid == [date(2026, 11, 5)]
    store.mark_paid("d_net", date(2026, 11, 5), paid=False)
    assert store.view().item("d_net").paid == []


def test_share_paid_is_per_person_per_occurrence():
    """§16: 'has Sam paid me back' is about a specific bill, not a subscription."""
    store = build_store()
    occ = date(2026, 10, 12)

    store.mark_share_paid("d_spotify", occ, "p_sam")
    assert store.view().item("d_spotify").shares_paid[occ] == ["p_sam"]

    store.mark_share_paid("d_spotify", occ, "p_alex")
    assert set(store.view().item("d_spotify").shares_paid[occ]) == {"p_sam", "p_alex"}

    store.mark_share_paid("d_spotify", occ, "p_sam", paid=False)
    assert store.view().item("d_spotify").shares_paid[occ] == ["p_alex"]


def test_marking_a_share_unpaid_clears_the_empty_bucket():
    store = build_store()
    occ = date(2026, 10, 12)
    store.mark_share_paid("d_spotify", occ, "p_sam")
    store.mark_share_paid("d_spotify", occ, "p_sam", paid=False)
    assert occ not in store.view().item("d_spotify").shares_paid


def test_mutations_on_an_unknown_item_raise():
    store = build_store()
    with pytest.raises(StoreError, match="no item with id"):
        store.mark_paid("d_nope", date(2026, 10, 1))


# --------------------------------------------------------------- splitting a series (§9, §37)


def test_split_series_moves_the_future_and_leaves_the_past_alone():
    store = build_store()
    occ = date(2026, 11, 1)

    original, tail = store.split_series_from("d_rent", occ)

    # The past half keeps its history, exactly.
    assert original.end == date(2026, 10, 31)
    assert original.paid == [date(2026, 9, 1), date(2026, 10, 1)]
    assert original.overrides == {}

    # The future half starts at the split and carries the state from there on.
    assert tail.start == occ
    assert tail.end is None
    assert tail.overrides == {date(2026, 11, 1): {"amount": 545.0}}
    assert tail.id != original.id
    assert tail.name == original.name

    assert store.view().item(tail.id) is not None


def test_split_at_an_occurrence_with_no_state_at_or_after_it():
    store = build_store()
    original, tail = store.split_series_from("d_net", date(2026, 12, 5))
    assert tail.overrides == {}
    assert tail.paid == []
    assert original.end == date(2026, 12, 4)


# --------------------------------------------------------------- item lifecycle


def test_delete_item_removes_descendants_by_default():
    store = build_store()
    removed = store.delete_item("d_utilities")
    assert removed == 3
    assert store.view().item("d_elec") is None
    assert store.view().item("d_water") is None


def test_delete_item_keeps_children_when_not_recursive():
    store = build_store()
    store.delete_item("d_utilities", recursive=False)
    assert store.view().item("d_elec") is not None


def test_add_item_stamps_created_and_updated(tmp_path):
    store = build_store(tmp_path)
    item = store.add_item(Item(id="d_new", name="New", type=EXPENSE, amount=10.0))
    assert item.created_at == datetime(2026, 10, 7, 12, 0, 0)
    assert item.updated_at == item.created_at


def test_update_of_an_absent_entity_raises():
    store = build_store()
    with pytest.raises(StoreError, match="no items entry"):
        store.update("items", Item(id="d_ghost"))


# --------------------------------------------------------------- canvases (§26)


def test_delete_canvas_refuses_the_last_one():
    store = build_store()
    store.add_canvas("Dashboard")
    only = store.settings.canvases[0].id
    assert store.delete_canvas(only) is False
    assert len(store.settings.canvases) == 1


def test_delete_canvas_moves_active_to_a_survivor():
    store = build_store()
    first = store.add_canvas("Dashboard")
    second = store.add_canvas("Analysis")
    store.settings.canvas_active = second.id

    assert store.delete_canvas(second.id) is True
    assert store.settings.canvas_active == first.id
    assert [c.name for c in store.settings.canvases] == ["Dashboard"]


def test_canvas_widgets_survive_a_restart(tmp_path):
    store = build_store(tmp_path)
    canvas = store.add_canvas("Bills")
    canvas.widgets.append(PlacedWidget(widget="hero_pl", x=2, y=3, w=6, h=3))
    store.save()

    reloaded = Store.load(tmp_path)
    reloaded_canvas = reloaded.active_canvas()

    assert reloaded_canvas.name == "Bills"
    assert reloaded_canvas.widgets[0].x == 2
    assert reloaded_canvas.widgets[0].w == 6


def test_reorder_canvas_clamps_out_of_range_indexes():
    store = build_store()
    first = store.add_canvas("A")
    second = store.add_canvas("B")
    store.reorder_canvas(second.id, 99)
    assert [c.id for c in store.settings.canvases] == [first.id, second.id]


# --------------------------------------------------------------- ids


def test_new_ids_are_prefixed_and_unique():
    ids = {new_id("items") for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("d_") for i in ids)


# --------------------------------------------------------------- the two sign conventions


def test_signed_is_the_only_place_the_conventions_meet():
    assert signed(Item(id="a", type=INCOME, amount=3200.0)) == 3200.0
    assert signed(Item(id="b", type=EXPENSE, amount=520.0)) == -520.0
    # An item stored negative (however it got there) still reads by its type.
    assert signed(Item(id="c", type=EXPENSE, amount=-520.0)) == -520.0
    # A transaction carries its own sign and has no `type`, so it passes through.
    assert signed(type("T", (), {"amount": -4.5})()) == -4.5


def test_fixture_settings_are_the_expected_shape():
    settings = build_settings()
    assert settings.currency == "AUD"
    assert settings.lens == "month"
    assert settings.enable_bank_sync is False


def test_build_document_ids_are_unique():
    doc = build_document()
    ids = [i.id for i in doc.items] + [p.id for p in doc.people] + [a.id for a in doc.accounts]
    assert len(ids) == len(set(ids))


# ================================================================ frozen builds (§5)


def test_a_built_app_writes_beside_the_executable_not_inside_it(monkeypatch, tmp_path):
    """Guarded: a one-file build unpacks the program into a temporary directory and deletes
    it on exit. An app that worked out its own location from `__file__` would put the store
    in there and throw the user's data away every time they closed the window — the single
    worst thing this program could do."""
    import importlib
    import sys

    from finance_tool import paths

    where = tmp_path / "somewhere"
    where.mkdir()
    unpacked = tmp_path / "_MEI1234"
    unpacked.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(where / "Finance Tool.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(unpacked), raising=False)
    try:
        built = importlib.reload(paths)

        assert built.APP_ROOT == where
        assert built.DATA_DIR == where / "data"
        # Bundled read-only files really do come out of the unpack directory.
        assert built.RESOURCE_ROOT == unpacked
        assert all(str(d).startswith(str(unpacked)) for d in built.FONT_DIRS)
    finally:
        monkeypatch.undo()
        importlib.reload(paths)


def test_run_from_source_the_two_roots_are_the_same(monkeypatch, tmp_path):
    import importlib
    import sys

    from finance_tool import paths

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    try:
        source = importlib.reload(paths)
        assert source.APP_ROOT == source.RESOURCE_ROOT
        assert source.DATA_DIR.parent == source.APP_ROOT
    finally:
        monkeypatch.undo()
        importlib.reload(paths)
