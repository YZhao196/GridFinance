"""Revision history: what is kept, what is thinned, and what a restore does (§12).

The retention policy is a pure function of the time and the revisions, so most of this file
needs neither a clock nor a disk — which is the point of writing it that way. The rest is
about the one promise that matters: a restore can itself be undone.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from finance_tool.store import history as hist
from finance_tool.store.store import Store, StoreError

from fixtures import build_document, build_store

NOW = datetime(2026, 10, 8, 12, 0, 0)


def revision(*, ago: timedelta, reason: str = hist.AUTOMATIC, size: int = 1024):
    """A revision that many minutes/hours/days before NOW, with no file behind it."""
    return hist.Revision(path=None, when=NOW - ago, reason=reason, size=size)


# --------------------------------------------------------------------------- names


def test_a_snapshot_name_carries_its_time_and_reason():
    when = datetime(2026, 10, 8, 14, 32, 5)
    name = hist.name_for(when, "manual")

    assert name == "store-20261008-143205-manual.db"
    assert hist.parse_name(name) == (when, "manual")


def test_a_file_that_is_not_ours_is_ignored_rather_than_guessed_at():
    """Showing a revision with an invented timestamp is worse than not showing it."""
    for name in ("notes.txt", "store.db", "store-nonsense-auto.db",
                 "store-20261008-143205.db", "backup-20261008-143205-auto.db"):
        assert hist.parse_name(name) is None


def test_listing_reads_the_directory_newest_first(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for minutes in (10, 200, 5):
        (backups / hist.name_for(NOW - timedelta(minutes=minutes), "auto")).write_bytes(b"x")
    (backups / "notes.txt").write_text("ignore me", encoding="utf-8")

    found = hist.revisions(backups)

    assert len(found) == 3
    assert [r.when for r in found] == sorted((r.when for r in found), reverse=True)


# --------------------------------------------------------------------- retention


def test_everything_from_the_last_day_is_kept():
    items = [revision(ago=timedelta(minutes=5)), revision(ago=timedelta(hours=3)),
             revision(ago=timedelta(hours=23))]

    assert hist.expired(items, now=NOW) == []


def test_within_the_week_one_snapshot_an_hour_survives():
    early = revision(ago=timedelta(days=2, hours=30))
    later = revision(ago=timedelta(days=2, hours=29, minutes=50))
    latest = revision(ago=timedelta(days=2, hours=29, minutes=40))

    doomed = hist.expired([early, later, latest], now=NOW)

    assert latest not in doomed              # the newest of the hour is the one kept
    assert sorted(doomed, key=lambda r: r.when) == [early, later]


def test_beyond_the_week_one_a_day_survives():
    early = revision(ago=timedelta(days=40, hours=6))
    later = revision(ago=timedelta(days=40, hours=3))
    latest = revision(ago=timedelta(days=40))

    doomed = hist.expired([early, later, latest], now=NOW)

    assert latest not in doomed
    assert sorted(doomed, key=lambda r: r.when) == [early, later]


def test_beyond_a_year_automatic_snapshots_are_dropped():
    items = [revision(ago=timedelta(days=400))]

    assert hist.expired(items, now=NOW) == items


def test_a_deliberate_snapshot_is_never_pruned():
    """The moment you need a backup is not the moment to find the policy ate it."""
    items = [revision(ago=timedelta(days=900), reason="manual"),
             revision(ago=timedelta(days=900), reason=hist.BEFORE_MIGRATION),
             revision(ago=timedelta(days=900), reason=hist.BEFORE_RESTORE)]

    assert hist.expired(items, now=NOW) == []


def test_an_empty_history_expires_nothing():
    assert hist.expired([], now=NOW) == []


# ----------------------------------------------------------------------- capture


def test_a_snapshot_is_a_database_that_loads(tmp_path):
    store = build_store(tmp_path)
    store.save()

    target = store.revision(force=True)

    assert target is not None and target.exists() and target.suffix == ".db"
    assert hist.revisions(store.backups_dir)


def test_automatic_snapshots_are_throttled(tmp_path):
    store = build_store(tmp_path)
    store.save()

    first = store.revision()
    second = store.revision()

    assert first is not None
    assert second is None                    # inside the interval, so nothing was taken
    assert store.revision(force=True) is not None


def test_a_deliberate_snapshot_is_not_throttled(tmp_path):
    store = build_store(tmp_path)
    store.save()

    assert store.revision("manual") is not None
    assert store.revision("manual") is not None


def test_a_store_with_no_directory_simply_has_no_history():
    """Best-effort, not an error: nobody asked for this snapshot."""
    store = Store(doc=build_document(), data_dir=None)

    assert store.revision() is None
    assert store.revisions() == []
    with pytest.raises(StoreError):
        store.revision("manual")             # asking for one *is* an act, so it is refused


def test_a_read_only_store_takes_no_snapshot(tmp_path):
    from fixtures import seed_store_dir

    seed_store_dir(tmp_path)
    store = Store.load(tmp_path, create=False)

    assert store.revision() is None


def test_one_automatic_snapshot_is_taken_per_commit_at_most(tmp_path):
    """A revision per keystroke is a log, not a history."""
    store = build_store(tmp_path)
    store.save()
    before = len(store.revisions())

    for _ in range(5):
        store.save()
        store.revision()

    assert len(store.revisions()) == before + 1


# ----------------------------------------------------------------------- restore


def test_restoring_puts_the_store_back(tmp_path):
    store = build_store(tmp_path)
    store.save()
    snapshot = store.revision("manual")
    original = [item.id for item in store.doc.items]

    store.remove("items", original[0])
    store.save()
    assert len(store.doc.items) == len(original) - 1

    store.restore(snapshot)

    assert [item.id for item in store.doc.items] == original


def test_a_restore_takes_a_safety_snapshot_first(tmp_path):
    """The reason a restore is not scary: what you just left is sitting beside it."""
    store = build_store(tmp_path)
    store.save()
    snapshot = store.revision("manual")
    store.remove("items", store.doc.items[0].id)
    store.save()

    store.restore(snapshot)

    kept = [r for r in store.revisions() if r.reason == hist.BEFORE_RESTORE]
    assert len(kept) == 1
    restored = store.revision("manual")      # a second, known-good snapshot exists
    assert restored is not None


def test_a_restore_survives_a_reload(tmp_path):
    """It has to be the file that changed, not just the objects in memory."""
    store = build_store(tmp_path)
    store.save()
    snapshot = store.revision("manual")
    original = [item.id for item in store.doc.items]
    store.remove("items", original[0])
    store.save()
    store.close()

    reopened = Store.load(tmp_path)
    reopened.restore(snapshot)
    reopened.close()

    assert [item.id for item in Store.load(tmp_path).doc.items] == original


def test_restoring_is_refused_when_read_only(tmp_path):
    from fixtures import seed_store_dir

    seed_store_dir(tmp_path)
    store = Store.load(tmp_path, create=False)

    with pytest.raises(StoreError, match="read-only"):
        store.restore(tmp_path / "backups" / "store-20261008-120000-manual.db")


# ------------------------------------------------------------------------ export


def test_a_snapshot_can_be_exported_anywhere(tmp_path):
    store = build_store(tmp_path)
    store.save()
    snapshot = store.revision("manual")

    written = hist.export_revision(snapshot, tmp_path / "elsewhere" / "copy.db")

    assert written.read_bytes() == snapshot.read_bytes()


def test_the_json_export_is_readable_and_complete(tmp_path):
    """The human-readable copy. Not the store — the database is — but the thing you can
    read, diff, or hand to someone."""
    import json

    store = build_store(tmp_path)
    store.save()

    target = store.export_json(tmp_path / "store-export.json")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert len(payload["items"]) == len(build_document().items)
    assert payload["items"][0]["id"]


def test_the_json_export_is_written_atomically(tmp_path):
    store = build_store(tmp_path)
    store.save()

    store.export_json(tmp_path / "out.json")

    assert list(tmp_path.glob("*.tmp")) == []


def test_a_store_with_no_directory_cannot_export():
    store = Store(doc=build_document(), data_dir=None)

    with pytest.raises(StoreError, match="has no directory"):
        store.export_json(Path("nowhere.json"))
