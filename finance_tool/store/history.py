"""Revision history: snapshots of the store you can go back to (§12).

A finance app's worst failure is not a crash — it is a bad edit that is *plausible*. A
mis-keyed amount, an import pointed at the wrong account, a recategorisation run that
overwrote hand-set categories: none of those throw, and all of them are discovered a week
later. The JSON store survived them only because `backups/` existed; nothing listed it,
nothing restored it, and it filled up with anything at all.

This is the same idea, made deliberate:

* **A snapshot is the database itself**, taken through SQLite's own backup so it is
  consistent even mid-write. It restores *exactly* — an export and re-import would have to
  round-trip every entity and would fail on the day someone adds a field.
* **Automatic snapshots are throttled and pruned; deliberate ones are not.** `store.backup()`
  before a migration and anything the user asks for by hand are kept until the user removes
  them, because the moment you need a backup is not the moment to discover the retention
  policy ate it.
* **The pruning policy is a pure function.** Whether a revision has expired depends on the
  time, the revision and nothing else, so it can be tested without a clock, a disk or a
  store.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

STAMP_FORMAT = "%Y%m%d-%H%M%S"

# The reason on a snapshot the app took by itself. Anything else is deliberate.
AUTOMATIC = "auto"
BEFORE_RESTORE = "before-restore"
BEFORE_MIGRATION = "pre-migration"

# At most one automatic snapshot per window. A revision per keystroke is not history, it is
# a log — and the point of a revision is that it is a state you can return to.
AUTOMATIC_INTERVAL = timedelta(minutes=10)

# How long the automatic history reaches back, and how finely.
KEEP_ALL = timedelta(days=1)
HOURLY_FOR = timedelta(days=7)
DAILY_FOR = timedelta(days=365)

_NAME = re.compile(r"^store-(\d{8}-\d{6})-(.+?)(?:-\d+)?\.db$")


@dataclass(frozen=True)
class Revision:
    """One snapshot on disk."""

    path: Path
    when: datetime
    reason: str
    size: int

    @property
    def automatic(self) -> bool:
        return self.reason == AUTOMATIC

    @property
    def label(self) -> str:
        """"7 Oct 2026, 14:32 — auto", which is what a person needs to pick one."""
        return f"{self.when:%d %b %Y, %H:%M} — {self.reason.replace('-', ' ')}"

    @property
    def detail(self) -> str:
        return f"{self.size / 1024:.0f} KB"


def name_for(when: datetime, reason: str) -> str:
    return f"store-{when.strftime(STAMP_FORMAT)}-{reason}.db"


def unique_path(backups_dir: Path, when: datetime, reason: str) -> Path:
    """A name nothing is using yet.

    The stamp is only to the second, so two deliberate snapshots taken in the same second
    would share a path and the second would silently overwrite the first — which is the
    one thing a backup must never do.
    """
    base = name_for(when, reason)
    candidate = backups_dir / base
    if not candidate.exists():
        return candidate
    stem, suffix = base[:-len(".db")], ".db"
    for counter in range(2, 1000):
        candidate = backups_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
    return backups_dir / f"{stem}-{when.strftime('%f')}{suffix}"


def parse_name(name: str) -> tuple[datetime, str] | None:
    """The time and reason in a snapshot's name, or ``None`` if it is not one of ours.

    A file that does not parse is ignored rather than guessed at: showing a revision with
    an invented timestamp would be worse than not showing it.
    """
    match = _NAME.match(name)
    if match is None:
        return None
    try:
        when = datetime.strptime(match.group(1), STAMP_FORMAT)
    except ValueError:
        return None
    return when, match.group(2)


def revisions(backups_dir: Path | None) -> list[Revision]:
    """Every readable snapshot, newest first."""
    if backups_dir is None or not backups_dir.is_dir():
        return []
    found: list[Revision] = []
    for path in backups_dir.iterdir():
        if not path.is_file():
            continue
        parsed = parse_name(path.name)
        if parsed is None:
            continue
        when, reason = parsed
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append(Revision(path=path, when=when, reason=reason, size=size))
    found.sort(key=lambda revision: revision.when, reverse=True)
    return found


def expired(
    items: list[Revision],
    *,
    now: datetime,
    keep_all: timedelta = KEEP_ALL,
    hourly_for: timedelta = HOURLY_FOR,
    daily_for: timedelta = DAILY_FOR,
) -> list[Revision]:
    """Which revisions to delete, oldest first.

    Keep everything from the last day; then one an hour for a week; then one a day for a
    year; then nothing. A deliberate snapshot is never in the answer — the automatic
    history is what gets thinned, and a snapshot you asked for is not cluttering anything.
    """
    doomed: list[Revision] = []
    seen_hours: set[tuple[int, int, int, int]] = set()
    seen_days: set[tuple[int, int, int]] = set()

    for revision in sorted(items, key=lambda r: r.when, reverse=True):
        age = now - revision.when
        if revision.automatic and age > daily_for:
            doomed.append(revision)
            continue
        if not revision.automatic:
            continue
        if age <= keep_all:
            continue
        if age <= hourly_for:
            bucket = (revision.when.year, revision.when.month, revision.when.day,
                      revision.when.hour)
            if bucket in seen_hours:
                doomed.append(revision)
            else:
                seen_hours.add(bucket)
            continue
        bucket = (revision.when.year, revision.when.month, revision.when.day)
        if bucket in seen_days:
            doomed.append(revision)
        else:
            seen_days.add(bucket)
    return list(reversed(doomed))


def capture(connection: sqlite3.Connection, backups_dir: Path, *, when: datetime,
            reason: str) -> Path:
    """Snapshot the live database into ``backups_dir`` and return where it landed.

    Through SQLite's own backup rather than a file copy: a copy of a database being written
    to is not a state that ever existed.
    """
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(backups_dir, when, reason)
    connection.commit()
    destination = sqlite3.connect(target)
    try:
        connection.backup(destination)
    finally:
        destination.close()
    return target


def restore_into(snapshot: Path, connection: sqlite3.Connection) -> None:
    """Replace the live database's contents with a snapshot's.

    Written *into* the open connection rather than over the file, so nothing has to be
    closed and reopened around it and a failure leaves the live store as it was.
    """
    source = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)
    try:
        source.backup(connection)
    finally:
        source.close()


def export_revision(snapshot: Path, destination: Path) -> Path:
    """Copy a snapshot somewhere the user chose.

    A byte copy is right here and wrong for a restore: exporting wants the file as it is,
    and restoring wants its *contents* written into the live connection.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(snapshot.read_bytes())
    return destination


def prune(backups_dir: Path | None, *, now: datetime) -> list[Path]:
    """Delete the revisions that have expired. Returns what went."""
    gone: list[Path] = []
    for revision in expired(revisions(backups_dir), now=now):
        try:
            revision.path.unlink()
            gone.append(revision.path)
        except OSError:
            continue
    return gone
