"""The store: load, mutate, save (§8, §12).

SQLite is the store. `data/store.db` holds the same document the JSON file used to, with one
file instead of two and a write that touches one row instead of serialising everything.
`store/db.py` owns the schema; this module owns the object graph the engine reads.

The document is still held in memory as data objects, because that is what every widget and
every engine function walks. The database is what makes a *write* small and a *query* cheap,
and what makes `save()` a transaction rather than a rename.

Atomic writes survive for **JSON exports** — the human-readable copy you can still take from
Settings — in `atomic_write_json`. A read-only facade (:class:`StoreView`) is what the engine
and the data hooks see, so no hook can write through it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TypeVar

from finance_tool import paths
from finance_tool.store import db as dbm
from finance_tool.store import history
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
)
from finance_tool.store.migrations import SCHEMA_VERSION

E = TypeVar("E")

ID_PREFIX = {
    "items": "d",
    "accounts": "a",
    "transactions": "t",
    "rules": "r",
    "goals": "g",
    "tracker": "k",
    "people": "p",
    "snapshots": "s",
    "canvases": "c",
}


class StoreError(Exception):
    """Something went wrong that the user needs to see, never a silent fallback."""


def new_id(kind: str) -> str:
    """A stable, generated id — never reused (§10.1)."""
    return f"{ID_PREFIX.get(kind, 'x')}_{uuid.uuid4().hex[:10]}"


def atomic_write_json(path: Path, payload: Any) -> None:
    """Serialise to ``<path>.tmp`` then replace, so a crash cannot corrupt the target.

    A failed attempt cleans up its own temp file — leaving one behind would make the next
    run look like it had crashed — and a locked target is reported as a locked target.
    On Windows ``os.replace`` raises ``PermissionError`` when something else has the file
    open (an editor, a sync client, OneDrive), and a bare traceback for that is no use to
    the person looking at it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _discard(tmp)
        raise StoreError(f"could not write {path.name}: {exc}") from exc

    try:
        os.replace(tmp, path)
    except OSError as exc:
        _discard(tmp)
        raise StoreError(
            f"could not save {path.name}: {exc}. If another program has it open, close "
            "it and save again.") from exc


def _discard(tmp: Path) -> None:
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _tail_recurrence(recurrence: dict[str, Any] | None) -> dict[str, Any] | None:
    """The rule for a split's second half.

    A COUNT is counted from a series' *first* occurrence, so the tail must not inherit it:
    a six-occurrence series split in half would otherwise run six more times. The tail is
    left open-ended, and its end date is the user's to set — which is what "the future is a
    new definition you can edit freely" means.
    """
    if not recurrence:
        return None
    text = str(recurrence.get("rrule") or "")
    if not text:
        return dict(recurrence)
    kept = [part for part in text.split(";")
            if not part.strip().upper().startswith("COUNT=")]
    return {"rrule": ";".join(kept)}


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------- view


@dataclass(frozen=True)
class StoreView:
    """Read-only facade over a store snapshot (§28).

    The engine and every data hook receive this. There is deliberately no mutation
    method on it, so a hook *cannot* write even by accident.
    """

    doc: StoreDocument
    settings: Settings
    version: int = 0
    currency: str = "AUD"

    # -- collections ------------------------------------------------------

    @property
    def items(self) -> list[Item]:
        return self.doc.items

    @property
    def accounts(self) -> list[Account]:
        return self.doc.accounts

    @property
    def transactions(self) -> list[Transaction]:
        return self.doc.transactions

    @property
    def rules(self) -> list[Rule]:
        return self.doc.rules

    @property
    def goals(self) -> list[Goal]:
        return self.doc.goals

    @property
    def tracker(self) -> list[TrackerItem]:
        return self.doc.tracker

    @property
    def people(self) -> list[Person]:
        return self.doc.people

    @property
    def snapshots(self) -> list[Snapshot]:
        return self.doc.snapshots

    # -- lookup -----------------------------------------------------------

    def _find(self, collection: str, entity_id: str | None) -> Any | None:
        if entity_id is None:
            return None
        for entity in getattr(self.doc, collection):
            if getattr(entity, "id", None) == entity_id:
                return entity
        return None

    def item(self, entity_id: str | None) -> Item | None:
        return self._find("items", entity_id)

    def account(self, entity_id: str | None) -> Account | None:
        return self._find("accounts", entity_id)

    def person(self, entity_id: str | None) -> Person | None:
        return self._find("people", entity_id)

    def rule(self, entity_id: str | None) -> Rule | None:
        return self._find("rules", entity_id)

    def goal(self, entity_id: str | None) -> Goal | None:
        return self._find("goals", entity_id)

    def transaction(self, entity_id: str | None) -> Transaction | None:
        return self._find("transactions", entity_id)

    # -- small conveniences the engine leans on ---------------------------

    def item_name(self, entity_id: str | None) -> str:
        found = self.item(entity_id)
        return found.name if found else ""

    def person_name(self, entity_id: str | None) -> str:
        found = self.person(entity_id)
        return found.name if found else ""

    def children_of(self, parent_id: str | None) -> list[Item]:
        return [item for item in self.doc.items if item.parent_id == parent_id]

    def roots(self) -> list[Item]:
        return self.children_of(None)

    def account_by_external(self, external_id: str | None) -> Account | None:
        if external_id is None:
            return None
        for account in self.doc.accounts:
            if account.external_id == external_id:
                return account
        return None

    def foreign_accounts(self) -> list[Account]:
        """Accounts whose currency does not match the app's (§22): flagged, never summed."""
        return [a for a in self.doc.accounts if a.currency and a.currency != self.currency]


# --------------------------------------------------------------------------- store


class Store:
    """Owns the document, the settings, and their files on disk."""

    def __init__(
        self,
        doc: StoreDocument | None = None,
        settings: Settings | None = None,
        *,
        data_dir: Path | None = None,
        clock: Callable[[], datetime] = datetime.now,
        create: bool = True,
    ) -> None:
        self.doc = doc if doc is not None else StoreDocument(schema_version=SCHEMA_VERSION)
        self.settings = settings if settings is not None else Settings()
        # None means the store has *no* directory — an in-memory database — rather than
        # "wherever the app keeps its data". Defaulting to the real data directory meant a
        # throwaway store that saved would escape into the user's data; now it fails loudly
        # instead. The entry point passes the real directory explicitly.
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self._clock = clock
        self.version = 0
        # A read-only store can be read but never written. SQLite enforces this rather than
        # us promising to: `load(create=False)` opens the file with `mode=ro`, so a
        # screenshot physically cannot alter the store it is photographing.
        self.read_only = not create
        # Things the user must be told: an unreadable database, a refused migration (§32).
        self.notes: list[str] = []
        # When the last automatic revision was taken, for the throttle. Kept in memory: a
        # restart taking one snapshot is right, not a leak.
        self._revision_at: datetime | None = None
        # The store's save counter as this handle last saw it, so two windows can tell
        # they disagree instead of one silently overwriting the other.
        self._generation = 0

        self.db_path = None if self.data_dir is None else self.data_dir / dbm.DB_NAME
        self._conn = self._open_database(create=create)
        if create and (doc is not None or settings is not None):
            self._hydrate()

    def _open_database(self, *, create: bool) -> sqlite3.Connection:
        """Open the database, or an in-memory one where there is nothing to open.

        A read-only load of a store that does not exist yet is an empty in-memory store
        rather than an error: a missing store has always loaded blank, and asking to *look*
        at one must not bring it into being.

        A file that is not a database fails here rather than in `_read` — SQLite rejects it
        on the first statement — so the recovery lives here too.
        """
        if self.db_path is None or (not create and not self.db_path.exists()):
            connection = dbm.connect(None)
            if create:
                dbm.ensure_schema(connection)
            return connection

        self.data_dir.mkdir(parents=True, exist_ok=True)
        connection = None
        try:
            connection = dbm.connect(self.db_path, create=create)
            if create:
                dbm.ensure_schema(connection)
        except sqlite3.DatabaseError as exc:
            if not create:
                raise
            if dbm.is_busy(exc):
                # Another process — or a sync client — has the file for a moment. The
                # answer is to say so and stop, never to move the user's store aside and
                # carry on with an empty one.
                if connection is not None:
                    connection.close()
                raise dbm.StoreBusyError(
                    "the store is open in another window, or being synced. Close the "
                    "other window and try again — nothing has been changed.") from exc
            # The handle has to be released before the file can be moved aside, and on
            # Windows the move will simply fail while it is held.
            if connection is not None:
                connection.close()
            return self._recover_database(exc)
        return connection

    def _recover_database(self, exc: Exception) -> sqlite3.Connection:
        """Set an unreadable database aside and start again.

        Unlike the JSON store, the bad file has to *move*: left in place it would break every
        subsequent open, so there would be nothing to recover from either. Losing data
        silently is the one forbidden move — hence the note, and the copy in `backups/`.
        """
        self.doc = StoreDocument(schema_version=SCHEMA_VERSION)
        self.settings = Settings()
        self.read_only = False
        held = getattr(self, "_conn", None)
        if held is not None:
            try:
                held.close()
            except sqlite3.Error:
                pass
        target: Path | None = None
        moved = False
        if self.db_path is not None:
            self.backups_dir.mkdir(parents=True, exist_ok=True)
            target = self.backups_dir / f"corrupt-store-{_stamp(self._clock())}.db"
            try:
                # `os.replace` first — it is atomic within a device, where `shutil.move`
                # falls back to copy-then-delete and can fail half way.
                os.replace(self.db_path, target)
                moved = True
            except OSError:
                try:
                    shutil.move(str(self.db_path), str(target))
                    moved = True
                except OSError:
                    moved = False

        if not moved and self.db_path is not None and self.db_path.exists():
            # The file cannot be set aside, so a fresh store cannot be made at that path.
            # Refusing loudly is the only honest option: carrying on would either fail on
            # every statement or, worse, look like it had worked.
            raise dbm.StoreDatabaseError(
                f"the store could not be read ({exc}) and could not be set aside because "
                f"the file is in use. It is at {self.db_path}, unchanged — close whatever "
                "is holding it and try again.") from exc

        connection = dbm.connect(self.db_path, create=True)
        dbm.ensure_schema(connection)
        self.notes.append(
            f"the store could not be read ({exc}); it has been moved to {target.name} and "
            "the app has started empty. Nothing was discarded — the old file is in "
            "data/backups/.")
        return connection

    def _hydrate(self) -> None:
        """Write a supplied document and settings into the database.

        `Store(doc=...)` used to mean "here is the state, hold it". It still does — the
        database is a store of that state, so it starts with it rather than empty.
        """
        for table in dbm.TABLES:
            for entity in getattr(self.doc, table):
                dbm.write_one(self._conn, table, entity)
        dbm.write_canvases(self._conn, self.settings.canvases)
        dbm.write_settings(self._conn, self.settings)
        self._conn.commit()

    # -- paths ------------------------------------------------------------

    @property
    def has_directory(self) -> bool:
        """Whether this store can be persisted at all."""
        return self.data_dir is not None

    def _require_directory(self, action: str) -> Path:
        if self.data_dir is None:
            raise StoreError(
                f"this store has no directory, so it cannot {action}; "
                "construct it with data_dir=<path>")
        return self.data_dir

    def _require_writable(self, action: str) -> Path:
        """The directory, refusing a read-only load as well as a scratch store."""
        directory = self._require_directory(action)
        if self.read_only:
            raise StoreError(
                f"this store was opened read-only, so it cannot {action}")
        return directory

    @property
    def store_path(self) -> Path | None:
        """The database file. Settings live inside it, so there is one path, not two."""
        return self.db_path

    @property
    def backups_dir(self) -> Path | None:
        return None if self.data_dir is None else self.data_dir / "backups"

    @property
    def secrets_path(self) -> Path | None:
        return None if self.data_dir is None else self.data_dir / "secrets.json"

    # -- load -------------------------------------------------------------

    @classmethod
    def load(
        cls,
        data_dir: Path | None = None,
        *,
        clock: Callable[[], datetime] = datetime.now,
        create: bool = True,
    ) -> "Store":
        """Open the store, reading the whole document into memory.

        An unreadable database is moved aside rather than destroyed, and the reason is
        recorded in ``notes`` so Settings can surface it (§32). The app never starts blank
        without saying so.

        ``create=False`` opens read-only through a SQLite URI: the directory is not
        created, nothing is written, and a store that would need migrating is left alone.
        Loading to *look* cannot change what is looked at, because SQLite itself refuses.
        """
        directory = Path(data_dir) if data_dir is not None else paths.DATA_DIR
        store = cls(data_dir=directory, clock=clock, create=create)
        if create:
            (directory / "backups").mkdir(parents=True, exist_ok=True)
        store._read()
        store.version = 1
        return store

    def _read(self) -> None:
        """Load the document and settings. A corrupt file is set aside and reported."""
        try:
            document = StoreDocument(schema_version=SCHEMA_VERSION)
            for table in dbm.TABLES:
                setattr(document, table, dbm.read_all(self._conn, table))
            settings = dbm.read_settings(self._conn)
        except sqlite3.DatabaseError as exc:
            if self.read_only:
                # Read-only may not write, so the file is reported and left alone. It is
                # still untouched, which is the part that matters.
                self.doc = StoreDocument(schema_version=SCHEMA_VERSION)
                self.settings = Settings()
                if dbm.is_busy(exc):
                    raise dbm.StoreBusyError(
                        "the store is open in another window, or being synced.") from exc
                self.notes.append(
                    f"the store could not be read ({exc}). It has been left as it is.")
                return
            if dbm.is_busy(exc):
                # In use, not damaged. Reading it as damage would move it aside.
                raise dbm.StoreBusyError(
                    "the store is open in another window, or being synced. Close the "
                    "other window and try again — nothing has been changed.") from exc
            self._conn = self._recover_database(exc)
            return

        self.doc = document
        self.settings = settings if settings is not None else Settings()
        self._generation = dbm.read_generation(self._conn)
        version = dbm.schema_version(self._conn)
        if version > dbm.SCHEMA_VERSION:
            self.notes.append(
                f"the store is schema v{version}, newer than this build "
                f"(v{dbm.SCHEMA_VERSION}). It has been left untouched.")

    def backup(self, reason: str = "manual") -> Path | None:
        """Snapshot the store beside it. Called before any migration (§12).

        Uses SQLite's own backup, so the snapshot is consistent even if taken mid-write —
        copying the file would not be, once a write-ahead log is in play.
        """
        self._require_writable("back up")
        if self.db_path is None or not self.db_path.exists():
            return None
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        return history.capture(self._conn, self.backups_dir, when=self._clock(),
                               reason=reason)

    def close(self) -> None:
        """Release the file. Windows will not let a directory be removed while it is held."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # -- revision history --------------------------------------------------

    def revision(self, reason: str = history.AUTOMATIC, *,
                 when: datetime | None = None, force: bool = False) -> Path | None:
        """Take a snapshot, and prune what the policy has expired.

        An **automatic** snapshot is best-effort: it is throttled to one per
        `history.AUTOMATIC_INTERVAL`, a store with no directory simply has no history, and
        a read-only store takes none — none of which is an error, because nobody asked for
        it. An explicit reason is a deliberate act and is refused loudly if it cannot
        happen, the same way `backup()` is.
        """
        if reason == history.AUTOMATIC:
            if self.read_only or self.data_dir is None:
                return None
            if not force and self._recent_revision(when):
                return None
        else:
            self._require_writable("snapshot")

        moment = when or self._clock()
        target = history.capture(self._conn, self.backups_dir, when=moment, reason=reason)
        self._revision_at = moment
        history.prune(self.backups_dir, now=moment)
        return target

    def _recent_revision(self, when: datetime | None) -> bool:
        if self._revision_at is None:
            return False
        return (when or self._clock()) - self._revision_at < history.AUTOMATIC_INTERVAL

    def revisions(self) -> list[history.Revision]:
        return history.revisions(self.backups_dir)

    def export_json(self, destination: Path) -> Path:
        """Write a human-readable copy of the whole store — domain and preferences.

        Not the store itself — the database is — but the thing you can read, diff, keep in
        your own version control, or hand to someone else. This is what `atomic_write_json`
        is still for.
        """
        self._require_writable("export")
        payload = self.doc.to_dict()
        # Settings and canvases are half the store (§4.1), and an export that silently
        # omitted them would be a "whole store" copy that is missing the preferences.
        payload["settings"] = self.settings.to_dict()
        atomic_write_json(destination, payload)
        return destination

    def restore(self, snapshot: Path) -> None:
        """Put the store back to a snapshot, keeping the present one as a revision.

        The safety snapshot is the whole reason a restore is not scary: if the revision you
        picked turns out to be the wrong one, the state you just left is sitting beside it.
        """
        self._require_writable("restore")
        history.capture(self._conn, self.backups_dir, when=self._clock(),
                        reason=history.BEFORE_RESTORE)
        history.restore_into(snapshot, self._conn)
        self._read()
        # A restore replaces the counter along with everything else; claim the store for
        # this window rather than inheriting the snapshot's number.
        self._generation = dbm.bump_generation(self._conn)
        self._conn.commit()
        self._revision_at = self._clock()
        self.version += 1

    # -- save -------------------------------------------------------------

    def save(self) -> None:
        """Write the document and commit, in one transaction (§28 cache invalidation).

        The whole document rather than only what changed, because the editors mutate
        entities in place and then commit — the store has no way to know which rows moved.
        SQLite makes that one transaction instead of one file rewrite.

        **A failure rolls back.** Without that, a failed save left its `DELETE FROM` sitting
        uncommitted on the connection, and the *next* commit — even `save_settings` —
        baked the partial delete in. Fourteen items went that way.
        """
        self._require_writable("save")
        with self._transaction():
            self._check_writer()
            self._write_document()
            self._write_settings_rows()
            self._generation = dbm.bump_generation(self._conn)
        self.version += 1

    def save_settings(self) -> None:
        self._require_writable("save its settings")
        with self._transaction():
            self._check_writer()
            self._write_settings_rows()
            self._generation = dbm.bump_generation(self._conn)
        self.version += 1

    def _check_writer(self) -> None:
        """Refuse to overwrite a change another window made since this one loaded.

        The store writes the whole document, so two windows are last-writer-wins and the
        loser is silent: their transactions are deleted by the other window's next save,
        with no warning at all. One counter turns that into a refusal the user can act on.
        """
        current = dbm.read_generation(self._conn)
        if current != self._generation:
            raise StoreError(
                "another window has changed the store since this one loaded it. Reopen "
                "this window to see the current data — nothing has been written.")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Commit the block, or leave the database exactly as it was.

        The promise in §4.4 is about *failure*, not about success: an exception part way
        through has to take the writes with it, or the next commit finishes a job nobody
        asked for.
        """
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        self._conn.commit()

    def _write_document(self) -> None:
        for table in dbm.TABLES:
            self._conn.execute(f'DELETE FROM "{table}"')
            for entity in getattr(self.doc, table):
                dbm.write_one(self._conn, table, entity)

    def _write_settings_rows(self) -> None:
        dbm.write_settings(self._conn, self.settings)
        dbm.write_canvases(self._conn, self.settings.canvases)

    # -- view -------------------------------------------------------------

    def view(self) -> StoreView:
        return StoreView(
            doc=self.doc,
            settings=self.settings,
            version=self.version,
            currency=self.settings.currency,
        )

    # -- generic mutation --------------------------------------------------

    def _collection(self, name: str) -> list[Any]:
        return getattr(self.doc, name)

    @staticmethod
    def _key_of(collection: str, entity: Any) -> Any:
        """The entity's primary key. Most have an ``id``; a snapshot is its date."""
        _cls, key = dbm.TABLES[collection]
        return getattr(entity, key, None)

    def add(self, collection: str, entity: E) -> E:
        self._collection(collection).append(entity)
        self.version += 1
        return entity

    def get(self, collection: str, entity_id: str | None) -> Any | None:
        return self.view()._find(collection, entity_id)

    def remove(self, collection: str, entity_id: str) -> bool:
        target = self._collection(collection)
        for index, entity in enumerate(target):
            if self._key_of(collection, entity) == entity_id:
                del target[index]
                self.version += 1
                return True
        return False

    def update(self, collection: str, entity: E) -> E:
        """Replace an entity in place by id; the caller has already built the new value."""
        target = self._collection(collection)
        wanted = self._key_of(collection, entity)
        for index, existing in enumerate(target):
            if self._key_of(collection, existing) == wanted:
                target[index] = entity
                self.version += 1
                return entity
        raise StoreError(f"no {collection} entry with id {wanted!r}")

    # -- item API ----------------------------------------------------------

    def add_item(self, item: Item) -> Item:
        now = self._clock()
        if item.created_at is None:
            item.created_at = now
        item.updated_at = now
        return self.add("items", item)

    def update_item(self, item: Item) -> Item:
        item.updated_at = self._clock()
        return self.update("items", item)

    def delete_item(self, item_id: str, *, recursive: bool = True) -> int:
        """Delete an item, and by default its descendants. Returns the count removed."""
        doomed = {item_id}
        if recursive:
            changed = True
            while changed:
                changed = False
                for item in self.doc.items:
                    if item.parent_id in doomed and item.id not in doomed:
                        doomed.add(item.id)
                        changed = True
        before = len(self.doc.items)
        self.doc.items[:] = [i for i in self.doc.items if i.id not in doomed]
        removed = before - len(self.doc.items)
        if removed:
            self.version += 1
        return removed

    def _require_item(self, item_id: str) -> Item:
        found = self.view().item(item_id)
        if found is None:
            raise StoreError(f"no item with id {item_id!r}")
        return found

    # The per-occurrence mutation API of §10.1 — the only place a past occurrence's
    # *state* changes without rewriting the series.

    def set_override(self, item_id: str, occ: date, field_name: str, value: Any) -> Item:
        item = self._require_item(item_id)
        item.overrides.setdefault(occ, {})[field_name] = value
        return self.update_item(item)

    def clear_override(self, item_id: str, occ: date, field_name: str | None = None) -> Item:
        item = self._require_item(item_id)
        if field_name is None:
            item.overrides.pop(occ, None)
        else:
            bucket = item.overrides.get(occ)
            if bucket is not None:
                bucket.pop(field_name, None)
                if not bucket:
                    item.overrides.pop(occ, None)
        return self.update_item(item)

    def cancel_occurrence(self, item_id: str, occ: date) -> Item:
        item = self._require_item(item_id)
        if occ not in item.cancelled:
            item.cancelled.append(occ)
        return self.update_item(item)

    def uncancel_occurrence(self, item_id: str, occ: date) -> Item:
        item = self._require_item(item_id)
        item.cancelled = [d for d in item.cancelled if d != occ]
        return self.update_item(item)

    def mark_paid(self, item_id: str, occ: date, paid: bool = True) -> Item:
        item = self._require_item(item_id)
        if paid and occ not in item.paid:
            item.paid.append(occ)
        elif not paid:
            item.paid = [d for d in item.paid if d != occ]
        return self.update_item(item)

    def mark_share_paid(self, item_id: str, occ: date, person_id: str, paid: bool = True) -> Item:
        """Per person, per occurrence (§16)."""
        item = self._require_item(item_id)
        holders = item.shares_paid.setdefault(occ, [])
        if paid and person_id not in holders:
            holders.append(person_id)
        elif not paid:
            item.shares_paid[occ] = [p for p in holders if p != person_id]
            if not item.shares_paid[occ]:
                item.shares_paid.pop(occ, None)
        return self.update_item(item)

    def split_series_from(self, item_id: str, occ: date) -> tuple[Item, Item]:
        """End this item the day before ``occ`` and start a second from it (§9, §37).

        The past stays exactly as it was; the future is a new definition the user can
        edit freely. Occurrence state at or after ``occ`` moves with the new half.
        """
        original = self._require_item(item_id)
        from datetime import timedelta

        tail = Item(
            id=new_id("items"),
            parent_id=original.parent_id,
            name=original.name,
            type=original.type,
            amount=original.amount,
            start=occ,
            end=original.end,
            recurrence=_tail_recurrence(original.recurrence),
            due=original.due,
            priority=original.priority,
            tags=list(original.tags),
            note=original.note,
            category=original.category,
            subscription=original.subscription,
            shared=dict(original.shared) if original.shared else None,
            overrides={d: dict(v) for d, v in original.overrides.items() if d >= occ},
            cancelled=[d for d in original.cancelled if d >= occ],
            paid=[d for d in original.paid if d >= occ],
            shares_paid={d: list(v) for d, v in original.shares_paid.items() if d >= occ},
            ideal=original.ideal,
            cancel_by=original.cancel_by,
            expanded=original.expanded,
            created_at=self._clock(),
        )

        original.end = occ - timedelta(days=1)
        original.overrides = {d: v for d, v in original.overrides.items() if d < occ}
        original.cancelled = [d for d in original.cancelled if d < occ]
        original.paid = [d for d in original.paid if d < occ]
        original.shares_paid = {d: v for d, v in original.shares_paid.items() if d < occ}
        self.update_item(original)
        self.add_item(tail)
        return original, tail

    # -- settings / canvases ----------------------------------------------

    def canvas(self, canvas_id: str | None) -> Canvas | None:
        if canvas_id is None:
            return None
        for canvas in self.settings.canvases:
            if canvas.id == canvas_id:
                return canvas
        return None

    def active_canvas(self) -> Canvas | None:
        return self.canvas(self.settings.canvas_active)

    def add_canvas(self, name: str, widgets: Sequence[PlacedWidget] | None = None) -> Canvas:
        canvas = Canvas(id=new_id("canvases"), name=name, widgets=list(widgets or []))
        self.settings.canvases.append(canvas)
        self.settings.canvas_active = canvas.id
        self.version += 1
        return canvas

    def rename_canvas(self, canvas_id: str, name: str) -> Canvas:
        canvas = self.canvas(canvas_id)
        if canvas is None:
            raise StoreError(f"no canvas with id {canvas_id!r}")
        canvas.name = name
        self.version += 1
        return canvas

    def delete_canvas(self, canvas_id: str) -> bool:
        """Refused when it is the last one (§26). Returns whether it was deleted."""
        if len(self.settings.canvases) <= 1:
            return False
        before = len(self.settings.canvases)
        self.settings.canvases[:] = [c for c in self.settings.canvases if c.id != canvas_id]
        if len(self.settings.canvases) == before:
            return False
        if self.settings.canvas_active == canvas_id:
            self.settings.canvas_active = self.settings.canvases[0].id
        self.version += 1
        return True

    def reorder_canvas(self, canvas_id: str, new_index: int) -> None:
        canvases = self.settings.canvases
        for index, canvas in enumerate(canvases):
            if canvas.id == canvas_id:
                canvases.insert(max(0, min(new_index, len(canvases) - 1)), canvases.pop(index))
                self.version += 1
                return

    def iter_items(self) -> Iterator[Item]:
        return iter(self.doc.items)
