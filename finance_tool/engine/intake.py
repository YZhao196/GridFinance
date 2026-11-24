"""Turning files into imports, and an inbox into a queue (§19–§21).

The import pipeline itself lives in :mod:`finance_tool.engine.importers`; this module adds
the things a file has that a row does not — a name, a hash, a history entry, a place it
came from and a place it moves to once read.

Like the rest of the engine it does not save. It mutates the store and bumps
``version``; the caller decides when that becomes durable. ``scan_inbox`` is the one
exception because it is the whole operation, and it saves once at the end rather than
once per file.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from finance_tool import paths
from finance_tool.engine import importers as imp
from finance_tool.store.entities import Account, ImportRecord
from finance_tool.store.store import Store, new_id

INBOX_SUFFIXES = {".csv", ".txt", ".ofx", ".qfx", ".pdf", ".xml"}


@dataclass(frozen=True)
class ImportOutcome:
    """What happened to one file, in terms a history list can render."""

    filename: str
    status: str                     # ok | already_imported | error
    account: str = ""
    summary: imp.ImportSummary | None = None
    record_id: str | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        if self.status == "already_imported":
            return f"{self.filename}: already imported"
        if self.status == "error":
            return f"{self.filename}: {self.error or 'could not be imported'}"
        return f"{self.filename}: {self.summary.describe() if self.summary else 'ok'}"


def resolve_account(store: Store, parsed: imp.ParseResult,
                    wanted: str | None = None) -> Account | str:
    """The account a statement belongs to, or the reason it cannot be decided.

    Explicit id first, then the account name the statement gives, then a sole account.
    Refusing when more than one account could match is deliberate: importing a month of
    transactions into the wrong account is not a mistake that is cheap to undo.
    """
    view = store.view()
    if wanted:
        found = view.account(wanted)
        if found is None:
            return f"no account with id {wanted!r}"
        problem = _currency_problem(found, parsed.meta or {})
        return problem or found

    meta = parsed.meta or {}
    name = str(meta.get("account_name") or "").strip()
    if name:
        matches = [a for a in view.accounts if a.name.casefold() == name.casefold()]
        if len(matches) == 1:
            problem = _currency_problem(matches[0], meta)
            return problem or matches[0]

    masked = str(meta.get("account_number_masked") or "").strip()
    if masked:
        matches = [a for a in view.accounts if a.masked and a.masked == masked]
        if len(matches) == 1:
            problem = _currency_problem(matches[0], meta)
            return problem or matches[0]

    if len(view.accounts) == 1:
        account = view.accounts[0]
        problem = _currency_problem(account, meta)
        return problem or account
    if not view.accounts:
        return "there is no account to import into; add one first"
    return "the statement does not name an account and there is more than one; choose one"


def _currency_problem(account: Account, meta: dict) -> str:
    """Why a statement cannot go into this account, or ``""`` when it can."""
    currency = str(meta.get("currency") or "").upper()
    if account.currency and currency and account.currency.upper() != currency:
        return (f"the statement is in {currency} but account {account.name!r} is in "
                f"{account.currency}; add a {currency} account or import it elsewhere")
    return ""


def import_path(
    store: Store,
    path: str | Path,
    *,
    account: str | None = None,
    move_processed: bool = False,
    processed_dir: Path | None = None,
    now: datetime | None = None,
) -> ImportOutcome:
    """Read one statement file into the store and record what happened."""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        reason = f"could not read the file ({exc})"
        record = _error_record(store, target.name, "", "", reason, now=now)
        return ImportOutcome(filename=target.name, status="error", error=reason,
                             record_id=record.id)

    digest = hashlib.sha256(data).hexdigest()
    prior = next((record for record in store.doc.imports
                  if record.file_hash == digest and record.status == "ok"
                  and (account is None or record.account == account)), None)
    if prior is not None:
        warning = ""
        if move_processed:
            _, warning = _move(target, processed_dir, prior.id)
        return ImportOutcome(
            filename=target.name, status="already_imported", account=prior.account,
            record_id=prior.id,
            warnings=["this exact file was imported before; nothing changed"]
                     + ([warning] if warning else []),
        )

    parsed = imp.parse_file(target)
    if not parsed.rows:
        reason = "; ".join(parsed.warnings) or "no transaction rows were read"
        record = _error_record(store, target.name, digest, parsed.format, reason,
                               rows_total=parsed.read, rows_skipped=len(parsed.skipped),
                               now=now)
        return ImportOutcome(filename=target.name, status="error", error=reason,
                             record_id=record.id, warnings=list(parsed.warnings))

    chosen = resolve_account(store, parsed, account)
    if isinstance(chosen, str):
        record = _error_record(store, target.name, digest, parsed.format, chosen,
                               rows_total=parsed.read, rows_skipped=len(parsed.skipped),
                               now=now)
        return ImportOutcome(filename=target.name, status="error", error=chosen,
                             record_id=record.id, warnings=list(parsed.warnings))

    record_id = new_id("imports")
    summary = imp.import_parse_result(store, parsed, account=chosen.id,
                                      import_id=record_id, now=now)
    record = ImportRecord(
        id=record_id,
        filename=target.name,
        file_hash=digest,
        source_type=parsed.format,
        account=chosen.id,
        rows_total=summary.total,
        rows_new=summary.imported,
        rows_duplicate=summary.duplicates,
        rows_skipped=summary.skipped,
        status="ok",
        imported_at=now or datetime.now(),
        span_start=summary.span[0] if summary.span else None,
        span_end=summary.span[1] if summary.span else None,
    )
    store.add("imports", record)

    warnings = list(parsed.warnings)
    if move_processed:
        moved, warning = _move(target, processed_dir, record_id)
        if not moved:
            warnings.append(warning)

    return ImportOutcome(filename=target.name, status="ok", account=chosen.id,
                         summary=summary, record_id=record_id, warnings=warnings)


def _error_record(store: Store, filename: str, digest: str, source_type: str, error: str,
                  *, rows_total: int = 0, rows_skipped: int = 0,
                  now: datetime | None = None) -> ImportRecord:
    """A failed import is still history: the file, the reason and when it happened.

    A file that keeps failing is recorded once, not once per poll: the inbox will retry
    it every scan, and history that grows a row every fifteen minutes is not history.
    """
    for existing in store.doc.imports:
        if existing.status != "error":
            continue
        if digest and existing.file_hash == digest:
            return existing
        if not digest and not existing.file_hash and existing.filename == filename:
            return existing
    record = ImportRecord(
        id=new_id("imports"), filename=filename, file_hash=digest,
        source_type=source_type, status="error", error=error,
        rows_total=rows_total, rows_skipped=rows_skipped,
        imported_at=now or datetime.now(),
    )
    store.add("imports", record)
    return record


def _move(path: Path, processed_dir: Path | None, record_id: str) -> tuple[bool, str]:
    """Move a read file out of the inbox; a failure is reported, not raised."""
    destination_dir = processed_dir or paths.PROCESSED_DIR
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination_dir / f"{record_id}-{path.name}"))
        return True, ""
    except OSError as exc:
        return False, f"{path.name} was imported but could not be moved out of the inbox ({exc})"


def scan_inbox(
    store: Store,
    *,
    folder: Path | None = None,
    processed_dir: Path | None = None,
    account: str | None = None,
    now: datetime | None = None,
    save: bool = True,
) -> list[ImportOutcome]:
    """Import every statement waiting in the inbox, moving each one out once read."""
    source = Path(folder) if folder is not None else paths.INBOX_DIR
    source.mkdir(parents=True, exist_ok=True)

    pairs: list[tuple[Path, ImportOutcome]] = []
    before = store.version
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.casefold() not in INBOX_SUFFIXES:
            continue
        try:
            outcome = import_path(store, path, account=account, move_processed=False,
                                  now=now)
        except Exception as exc:  # one bad file must not stop the folder
            try:
                record = _error_record(store, path.name, "", "", str(exc), now=now)
                record_id = record.id
            except Exception:
                record_id = None
            outcome = ImportOutcome(filename=path.name, status="error", error=str(exc),
                                    record_id=record_id)
        pairs.append((path, outcome))

    if save and store.version != before:
        store.save()

    # The file moves only after the store is durable. Moving first would leave a failed
    # save with the statements out of the inbox and the transactions only in memory.
    outcomes: list[ImportOutcome] = []
    for path, outcome in pairs:
        if save and outcome.status in {"ok", "already_imported"} and path.exists():
            _, warning = _move(path, processed_dir, outcome.record_id or "imported")
            if warning:
                outcome = replace(outcome, warnings=outcome.warnings + [warning])
        outcomes.append(outcome)
    return outcomes
