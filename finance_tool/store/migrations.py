"""Schema versioning (§12).

Migrations are pure functions ``vN_to_vN1(doc) -> doc`` applied in sequence against the
raw dict, before any dataclass sees it. A failing migration leaves the original file
untouched — the backup taken by the caller is the recovery path.
"""

from __future__ import annotations

from typing import Any, Callable

SCHEMA_VERSION = 1

Migration = Callable[[dict[str, Any]], dict[str, Any]]

# Populated as the schema evolves: MIGRATIONS[1] takes a v1 document to v2.
MIGRATIONS: dict[int, Migration] = {}


class MigrationError(Exception):
    """A migration is missing, or one raised. Never swallowed."""


def document_version(doc: dict[str, Any]) -> int:
    """The schema version of a raw document.

    A document with no ``schema_version`` predates versioning; v1 is the shape it is
    already in, so the migrations below simply have nothing to do.
    """
    raw = doc.get("schema_version")
    if raw is None:
        return SCHEMA_VERSION
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"schema_version is not an integer: {raw!r}") from exc


def needs_migration(doc: dict[str, Any], target: int = SCHEMA_VERSION) -> bool:
    return document_version(doc) < target


def run_migrations(
    doc: dict[str, Any],
    target: int = SCHEMA_VERSION,
    migrations: dict[int, Migration] | None = None,
) -> dict[str, Any]:
    """Apply every migration between the document's version and ``target``.

    Returns a new dict; the input is never mutated. Raises :class:`MigrationError` if a
    step is missing or raises, so the caller can leave the file alone.
    """
    table = MIGRATIONS if migrations is None else migrations
    version = document_version(doc)

    if version > target:
        raise MigrationError(
            f"store is at schema v{version}, newer than this build understands (v{target})"
        )

    current = dict(doc)
    while version < target:
        step = table.get(version)
        if step is None:
            raise MigrationError(f"no migration registered from schema v{version} to v{version + 1}")
        try:
            current = step(current)
        except Exception as exc:  # surfaced, never silent
            raise MigrationError(f"migration v{version} -> v{version + 1} failed: {exc}") from exc
        if not isinstance(current, dict):
            raise MigrationError(f"migration v{version} -> v{version + 1} did not return a document")
        version += 1
        current["schema_version"] = version

    current["schema_version"] = target
    return current
