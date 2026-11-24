"""One-way migration from AI Finance Manager's SQLite database (§32).

Read-only on the source, additive on the destination, and refusing to touch a store that
already has data unless the caller says so. The source is never modified, and a
pre-migration copy is kept when a destination is deliberately replaced.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from finance_tool import paths
from finance_tool.engine import importers as imp
from finance_tool.store import db as dbm
from finance_tool.store.entities import (
    Account,
    ChatMessage,
    ImportRecord,
    ReviewItem,
    Rule,
    Settings,
    StoreDocument,
    Transaction,
)
from finance_tool.store.secrets import (
    SECRET_KEYS,
    Secrets,
    decode_stored,
    load_secrets,
    save_secrets,
)
from finance_tool.store.store import Store, StoreError, new_id


class MigrationError(Exception):
    """The migration cannot proceed safely."""


@dataclass
class MigrationReport:
    accounts: int = 0
    transactions: int = 0
    rules: int = 0
    reviews: int = 0
    imports: int = 0
    messages: int = 0
    source_transactions: int = 0
    skipped_transactions: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    afm_total: float = 0.0
    migrated_total: float = 0.0
    dry_run: bool = False
    destination: str = ""
    backup: str = ""

    @property
    def balanced(self) -> bool:
        return abs(self.afm_total - self.migrated_total) < 0.005

    def describe(self) -> str:
        lines = [
            f"accounts {self.accounts}, transactions {self.transactions}, "
            f"rules {self.rules}, review items {self.reviews}, "
            f"imports {self.imports}, chat messages {self.messages}",
            f"total moved ${self.migrated_total:,.2f} from ${self.afm_total:,.2f}",
        ]
        if self.skipped:
            lines.append(f"skipped {len(self.skipped)} row(s): " + "; ".join(self.skipped[:5]))
        if self.problems:
            lines.extend(f"would be refused: {problem}" for problem in self.problems)
        if self.warnings:
            lines.extend(f"warning: {warning}" for warning in self.warnings)
        if self.backup:
            lines.append(f"previous store backed up to {self.backup}")
        if self.dry_run:
            lines.append("dry run: nothing was written")
        elif self.destination:
            lines.append(f"written to {self.destination}")
        return "\n".join(lines)


# AFM runs the lowest priority number first; GridFinance runs the highest value first.
# Subtracting from a large base reverses the order without ever collapsing two distinct
# AFM priorities onto one value, and stays above the seeded rules' negative priorities.
AFM_PRIORITY_BASE = 1_000_000

REQUIRED_TABLES = {
    "accounts": {"id", "name", "account_type", "currency"},
    "categories": {"id", "name"},
    "transactions": {"id", "account_id", "posted_date", "amount_cents", "description"},
    "settings": {"key", "value"},
}


def _validate_source(conn: sqlite3.Connection) -> None:
    tables = {row["name"] for row in
              conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = sorted(set(REQUIRED_TABLES) - tables)
    if missing:
        raise MigrationError(
            "this does not look like an AI Finance Manager database (missing "
            + ", ".join(missing) + ")")
    for table, columns in REQUIRED_TABLES.items():
        present = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        absent = sorted(columns - present)
        if absent:
            raise MigrationError(
                f"the AI Finance Manager table {table!r} is missing "
                + ", ".join(absent))


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(" ", "T").split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


def _cents(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value) / 100.0, 2)


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in _rows(conn, "SELECT key, value FROM settings")}


SECRET_MAP = {
    "secret:llm:anthropic": "llm_anthropic",
    "secret:llm:openai": "llm_openai",
    "secret:llm:deepseek": "llm_deepseek",
    "secret:llm:openrouter": "llm_openrouter",
    "secret:feed:redbark": "feed_redbark",
    "secret:feed:banksync": "feed_banksync",
}


def _chat_calls(role: str, data: dict) -> list[dict]:
    if role == "tool":
        return [{"tool_call_id": data.get("tool_call_id", ""),
                 "name": data.get("name", ""),
                 "is_error": bool(data.get("is_error"))}]
    if role == "assistant":
        return list(data.get("tool_calls") or [])
    return []


def plan(afm_db: Path, *, allow_live: bool = False) -> tuple[
        StoreDocument, Settings, Secrets, MigrationReport]:
    """Read the AFM database and build the GridFinance entities without writing anything."""
    if not afm_db.exists():
        raise MigrationError(f"there is no AI Finance Manager database at {afm_db}")

    wal = Path(f"{afm_db}-wal")
    try:
        live_wal = wal.exists() and wal.stat().st_size > 0
    except OSError as exc:
        raise MigrationError(f"could not inspect {wal}: {exc}") from exc
    if live_wal and not allow_live:
        raise MigrationError(
            "AI Finance Manager's write-ahead log is not empty; close the app before "
            "migrating (or pass --allow-live to copy it anyway)")

    # Work from a disposable snapshot: opening a WAL database read-only still creates a
    # -shm file beside it, which would write into the source folder.
    tmpdir = tempfile.TemporaryDirectory()
    snapshot = Path(tmpdir.name) / "finance.db"
    try:
        shutil.copy2(afm_db, snapshot)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{afm_db}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, Path(f"{snapshot}{suffix}"))
    except OSError as exc:
        tmpdir.cleanup()
        raise MigrationError(f"could not read a snapshot of {afm_db}: {exc}") from exc

    conn = sqlite3.connect(snapshot)
    conn.row_factory = sqlite3.Row
    try:
        _validate_source(conn)
        report = MigrationReport()
        if live_wal:
            report.warnings.append(
                "the source has a non-empty write-ahead log and was copied while live; "
                "the snapshot may be missing the newest rows")
        document = StoreDocument()
        settings = Settings()
        secrets = Secrets()

        category_rows = _rows(conn, "SELECT id, name, parent_id FROM categories")
        categories = {int(row["id"]): str(row["name"]) for row in category_rows}
        if any(row["parent_id"] is not None for row in category_rows):
            report.warnings.append(
                "AFM's category hierarchy was flattened to leaf names; GridFinance "
                "categories are strings")

        account_map: dict[int, str] = {}
        import_map: dict[int, str] = {}
        transaction_map: dict[int, str] = {}

        for row in _rows(conn, "SELECT * FROM accounts ORDER BY id"):
            local_id = new_id("accounts")
            account_map[int(row["id"])] = local_id
            source = str(row["source"] or "afm")
            balance = _cents(row["current_balance_cents"])
            if balance is None:
                report.warnings.append(
                    f"account {row['id']} had no recorded balance; it was migrated as $0.00")
            document.accounts.append(Account(
                id=local_id,
                name=str(row["name"] or ""),
                kind=str(row["account_type"] or "transaction"),
                balance=balance or 0.0,
                currency=str(row["currency"] or "AUD").upper(),
                external_id=row["external_id"],
                provider=source,
                masked=str(row["number_masked"] or ""),
                balance_as_of=_date(row["balance_as_of"]),
            ))
        report.accounts = len(document.accounts)
        if _rows(conn, "SELECT 1 FROM accounts WHERE is_active = 0 LIMIT 1"):
            report.warnings.append(
                "archived AFM accounts were migrated as ordinary accounts, because "
                "GridFinance has no archive flag")

        for row in _rows(conn, "SELECT * FROM imports ORDER BY id"):
            local_id = new_id("imports")
            import_map[int(row["id"])] = local_id
            document.imports.append(ImportRecord(
                id=local_id,
                filename=str(row["filename"] or ""),
                file_hash=str(row["file_hash"] or ""),
                source_type=str(row["source_type"] or ""),
                account=account_map.get(int(row["account_id"]), "")
                         if row["account_id"] is not None else "",
                rows_total=int(row["rows_total"] or 0),
                rows_new=int(row["rows_new"] or 0),
                rows_duplicate=int(row["rows_duplicate"] or 0),
                status=str(row["status"] or "ok"),
                error=str(row["error"] or ""),
                imported_at=_datetime(row["imported_at"]),
            ))
        report.imports = len(document.imports)

        source_rows = _rows(conn, "SELECT * FROM transactions ORDER BY id")
        report.source_transactions = len(source_rows)
        report.afm_total = round(sum(
            int(row["amount_cents"] or 0) for row in source_rows) / 100.0, 2)
        seen: dict[tuple, int] = {}
        for row in source_rows:
            when = _date(row["posted_date"])
            local_account = account_map.get(int(row["account_id"]))
            if when is None:
                report.skipped.append(f"transaction {row['id']}: unreadable date")
                report.skipped_transactions += 1
                continue
            if local_account is None:
                report.skipped.append(f"transaction {row['id']}: unknown account")
                report.skipped_transactions += 1
                continue
            amount = _cents(row["amount_cents"]) or 0.0
            description = str(row["description"] or "")
            key = (when, round(amount, 2), imp.normalise_description(description),
                   local_account)
            index = seen.get(key, 0)
            seen[key] = index + 1
            category = categories.get(int(row["category_id"])) \
                if row["category_id"] is not None else None
            if row["category_id"] is not None and category is None:
                report.warnings.append(
                    f"transaction {row['id']}: unknown category "
                    f"{row['category_id']}; imported uncategorised")
            local_id = new_id("transactions")
            transaction_map[int(row["id"])] = local_id
            value_date = _date(row["value_date"])
            if row["value_date"] not in (None, "") and value_date is None:
                report.warnings.append(
                    f"transaction {row['id']}: unreadable value date "
                    f"{row['value_date']!r}; it was dropped")
            document.transactions.append(Transaction(
                id=local_id,
                date=when,
                amount=amount,
                description=description,
                account=local_account,
                category=category,
                tags=[],
                external_id=row["external_id"],
                fingerprint=imp.fingerprint(when, amount, description, local_account,
                                            "import", index=index),
                source="afm",
                imported_at=_datetime(row["created_at"]),
                merchant=row["merchant"],
                note=str(row["notes"] or ""),
                category_source=str(row["category_source"] or ""),
                category_confidence=row["category_confidence"],
                needs_review=_bool(row["needs_review"]),
                import_id=import_map.get(int(row["import_id"]))
                          if row["import_id"] is not None else None,
                status=str(row["status"] or "posted"),
                value_date=value_date,
                currency=str(row["currency"] or ""),
            ))
            report.migrated_total += amount
        report.transactions = len(document.transactions)

        bounded_rules = 0
        for row in _rows(conn, "SELECT * FROM rules ORDER BY id"):
            category = categories.get(int(row["category_id"]))
            if category is None:
                report.skipped.append(f"rule {row['id']}: unknown category")
                continue
            priority = int(row["priority"] or 0)
            amount_min = _cents(row["amount_min_cents"])
            amount_max = _cents(row["amount_max_cents"])
            if amount_min is not None or amount_max is not None:
                bounded_rules += 1
            document.rules.append(Rule(
                # A stable, ordered id keeps equal-priority rules in AFM's creation order;
                # GridFinance breaks priority ties by id.
                id=f"afm_rule_{int(row['id']):08d}",
                match=str(row["pattern"] or ""),
                category=category,
                tags=[],
                priority=AFM_PRIORITY_BASE - priority,
                name=str(row["name"] or row["pattern"] or ""),
                match_type=str(row["match_type"] or "contains"),
                amount_min=amount_min,
                amount_max=amount_max,
                active=_bool(row["is_active"]),
                hit_count=int(row["hit_count"] or 0),
            ))
        report.rules = len(document.rules)
        if bounded_rules:
            report.warnings.append(
                f"{bounded_rules} rule(s) with amount bounds were migrated; AFM compared "
                "the signed amount and GridFinance compares its magnitude, so a bound "
                "range may now match a different set of rows")

        for row in _rows(conn, "SELECT * FROM review_items ORDER BY id"):
            transaction_id = transaction_map.get(int(row["transaction_id"]))
            suggested = categories.get(int(row["suggested_category_id"])) \
                if row["suggested_category_id"] is not None else ""
            resolved = categories.get(int(row["resolved_category_id"])) \
                if row["resolved_category_id"] is not None else ""
            if row["suggested_category_id"] is not None and not suggested:
                report.warnings.append(
                    f"review {row['id']}: unknown suggested category "
                    f"{row['suggested_category_id']}")
            if row["resolved_category_id"] is not None and not resolved:
                report.warnings.append(
                    f"review {row['id']}: unknown resolved category "
                    f"{row['resolved_category_id']}")
            if transaction_id is None:
                report.skipped.append(f"review {row['id']}: unknown transaction")
                continue
            try:
                options = json.loads(row["options_json"] or "[]")
            except (TypeError, ValueError):
                options = []
            document.reviews.append(ReviewItem(
                id=new_id("reviews"),
                transaction_id=transaction_id,
                question=str(row["question"] or ""),
                reason=str(row["reason"] or "low_confidence"),
                options=list(options) if isinstance(options, list) else [],
                suggested_category=suggested or "",
                status=str(row["status"] or "open"),
                answer=str(row["answer"] or ""),
                resolved_category=resolved or "",
                created_at=_datetime(row["created_at"]),
                resolved_at=_datetime(row["resolved_at"]),
            ))
        report.reviews = len(document.reviews)

        for row in _rows(conn, "SELECT * FROM chat_messages ORDER BY id"):
            try:
                data = json.loads(row["tool_calls_json"] or "{}")
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            role = str(row["role"] or "")
            document.messages.append(ChatMessage(
                id=new_id("messages"),
                role=role,
                content=str(row["content"] or ""),
                tool_calls=_chat_calls(role, data),
                created_at=_datetime(row["created_at"]),
            ))
        report.messages = len(document.messages)

        stored = _settings(conn)
        settings.ai_provider = str(stored.get("llm.provider") or "")
        settings.ai_model = str(stored.get("llm.model") or "")
        settings.ai_effort = str(stored.get("llm.effort") or "")
        settings.ai_writes = stored.get("ai.writes", "1") == "1"
        settings.feed_provider = str(stored.get("feed.provider") or "")
        settings.enable_bank_sync = bool(settings.feed_provider)
        settings.onboarded = stored.get("onboarded", "") == "1"

        for stored_key, local_key in SECRET_MAP.items():
            raw = stored.get(stored_key)
            if not raw:
                continue
            value = decode_stored(raw)
            if value is None:
                report.warnings.append(f"{stored_key} could not be decrypted; it was skipped")
                continue
            secrets.set(local_key, value)

        report.warnings.append(
            "AI Finance Manager's PIN, review threshold and pending-row preference live "
            "outside its database; set them in GridFinance.")
        report.warnings.append(
            "API keys kept only in AFM's environment or .env are not in its database; "
            "enter them in GridFinance.")
        if _rows(conn, "SELECT 1 FROM budgets LIMIT 1"):
            report.warnings.append(
                "AFM budgets were not migrated; GridFinance derives budgets from the plan.")
        if _rows(conn, "SELECT 1 FROM recurring LIMIT 1"):
            report.warnings.append(
                "AFM recurring rows were not migrated; GridFinance derives them from history.")
        if stored.get("feed:last_sync"):
            report.warnings.append(
                "AFM's single global feed watermark was not migrated; each GridFinance "
                "account keeps its own and will re-read its lookback window once")
        if _rows(conn, "SELECT 1 FROM sync_state LIMIT 1"):
            report.warnings.append(
                "AFM's sync_state table was not migrated; GridFinance keeps watermarks on "
                "the account")

        return document, settings, secrets, report
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"could not read the AI Finance Manager database: {exc}") from exc
    except IndexError as exc:
        raise MigrationError(
            "an AI Finance Manager table is missing a column GridFinance needs "
            f"({exc}); nothing was read") from exc
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        raise MigrationError(f"malformed AI Finance Manager data: {exc}") from exc
    finally:
        conn.close()
        tmpdir.cleanup()


def _destination_has_data(data_dir: Path) -> bool:
    path = data_dir / dbm.DB_NAME
    if not path.exists():
        return False
    known = set(dbm.TABLES) | {dbm.SETTINGS_TABLE, dbm.CANVAS_TABLE, dbm.META_TABLE}
    try:
        inspection = sqlite3.connect(f"file:{quote(path.as_posix())}?mode=ro", uri=True)
        try:
            tables = {row[0] for row in inspection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = inspection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            inspection.close()
    except sqlite3.Error:
        return True
    if tables - known:
        return True                     # some other application's database
    if tables and not version:
        return True                     # tables GridFinance might use, but never stamped by it
    try:
        store = Store.load(data_dir, create=False)
    except Exception:
        return True
    try:
        if any("could not be read" in note for note in store.notes):
            return True
        if any(getattr(store.doc, table) for table in dbm.TABLES):
            return True
        default = Settings()
        return any(getattr(store.settings, field.name) != getattr(default, field.name)
                   for field in dataclasses.fields(Settings))
    finally:
        store.close()


def _backup(data_dir: Path, target: Path) -> Path | None:
    path = data_dir / dbm.DB_NAME
    if not path.exists():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = dbm.connect(path, create=False)
        destination = sqlite3.connect(target)
        try:
            connection.backup(destination)
        finally:
            destination.close()
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        target.unlink(missing_ok=True)
        raise MigrationError(
            f"could not back up the existing store ({exc}); nothing was changed") from exc
    return target


def migrate(afm_db: Path, data_dir: Path, *, force: bool = False,
            dry_run: bool = False, allow_live: bool = False) -> MigrationReport:
    source = Path(afm_db).resolve()
    destination = Path(data_dir).resolve()
    if not source.exists():
        raise MigrationError(f"there is no AI Finance Manager database at {source}")
    source_dir = source.parent
    if (destination == source_dir or destination.is_relative_to(source_dir)
            or source_dir.is_relative_to(destination)):
        raise MigrationError(
            "the destination must be outside the AI Finance Manager data tree")

    document, settings, secrets, report = plan(source, allow_live=allow_live)
    report.dry_run = dry_run
    report.destination = "" if dry_run else str(destination)

    has_data = _destination_has_data(destination)
    if dry_run:
        if report.skipped_transactions or report.skipped:
            report.problems.append(
                f"{len(report.skipped)} row(s) could not be mapped")
        if not report.balanced:
            report.problems.append(
                f"the source total (${report.afm_total:,.2f}) does not match what could "
                f"be read (${report.migrated_total:,.2f})")
        if has_data and not force:
            report.problems.append(
                f"{destination} already has a GridFinance store; --force would replace it")
        return report

    if report.skipped_transactions or report.skipped:
        raise MigrationError(
            f"{len(report.skipped)} row(s) could not be mapped, so nothing was written: "
            + "; ".join(report.skipped[:5]))
    if not report.balanced:
        raise MigrationError(
            f"the source total (${report.afm_total:,.2f}) does not match what could be "
            f"read (${report.migrated_total:,.2f}); nothing was written")
    if has_data and not force:
        raise MigrationError(
            f"{destination} already has a GridFinance store with data; "
            "pass --force to replace it (a backup is taken)")

    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationError(f"could not create {destination}: {exc}") from exc

    if force and destination.exists():
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
        base = destination / "backups" / f"afm-pre-migration-{stamp}"
        try:
            base.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MigrationError(f"could not create the backup folder: {exc}") from exc
        backup = _backup(destination, base.with_suffix(".db"))
        report.backup = str(backup) if backup else ""
        for name in ("secrets.json", "profile.md"):
            existing_file = destination / name
            if existing_file.exists():
                try:
                    shutil.copy2(existing_file,
                                 base.with_name(f"{base.name}-{name}"))
                except OSError as exc:
                    raise MigrationError(
                        f"could not back up {existing_file}: {exc}") from exc
        # Move the old files aside rather than deleting them. A rename onto a file
        # another window holds open fails on Windows before anything is lost, and a
        # later step that fails can put every file back.
        staged = base.with_name(f"{base.name}-replaced")
        moved: list[tuple[Path, Path]] = []
        try:
            staged.mkdir(parents=True, exist_ok=True)
            for name in (dbm.DB_NAME, f"{dbm.DB_NAME}-wal", f"{dbm.DB_NAME}-shm",
                         "secrets.json", "profile.md"):
                old = destination / name
                if not old.exists():
                    continue
                os.replace(old, staged / name)
                moved.append((old, staged / name))
        except OSError as exc:
            stuck = []
            for old, held in reversed(moved):
                try:
                    os.replace(held, old)
                except OSError:
                    stuck.append(str(old))
            if stuck:
                raise MigrationError(
                    f"could not replace the existing store ({exc}) and could not put "
                    f"back {', '.join(stuck)}; the copies in {base.parent} are intact") from exc
            raise MigrationError(
                f"could not replace the existing store ({exc}); close any window holding "
                "it and try again. Nothing has been changed.") from exc
        # The copies above are the durable record; the staged originals can go.
        shutil.rmtree(staged, ignore_errors=True)

    # Secrets and the profile are written before the store: a store that exists is what
    # makes a plain retry refuse, so it is the last thing to appear.
    existing = load_secrets(destination / "secrets.json")
    for key in SECRET_KEYS:
        value = getattr(secrets, key, None)
        if value:
            existing.set(key, value)
    try:
        save_secrets(existing, destination / "secrets.json")
    except OSError as exc:
        raise MigrationError(f"could not write secrets: {exc}") from exc

    profile = source.parent / "profile.md"
    target_profile = destination / "profile.md"
    if profile.exists() and not target_profile.exists():
        try:
            shutil.copy2(profile, target_profile)
        except OSError as exc:
            raise MigrationError(f"could not copy the profile: {exc}") from exc

    try:
        store = Store(doc=document, settings=settings, data_dir=destination)
        try:
            store.save()
        finally:
            store.close()
    except (StoreError, OSError, sqlite3.Error) as exc:
        raise MigrationError(f"could not write the GridFinance store: {exc}") from exc

    check = Store.load(destination, create=False)
    try:
        counts = {
            "accounts": len(check.doc.accounts),
            "transactions": len(check.doc.transactions),
            "rules": len(check.doc.rules),
            "reviews": len(check.doc.reviews),
            "imports": len(check.doc.imports),
            "messages": len(check.doc.messages),
        }
    finally:
        check.close()
    for name, expected in (("accounts", report.accounts), ("transactions", report.transactions),
                           ("rules", report.rules), ("reviews", report.reviews),
                           ("imports", report.imports), ("messages", report.messages)):
        if counts[name] != expected:
            raise MigrationError(
                f"{name}: wrote {counts[name]} but planned {expected}; inspect the destination")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate AI Finance Manager into GridFinance")
    parser.add_argument("--afm-db", type=Path,
                        default=Path(__file__).resolve().parent.parent.parent
                        / "AI Finance Manager" / "data" / "finance.db")
    parser.add_argument("--data-dir", type=Path, default=paths.DATA_DIR)
    parser.add_argument("--force", action="store_true",
                        help="replace an existing GridFinance store (backed up first)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would move; write nothing")
    parser.add_argument("--allow-live", action="store_true",
                        help="copy a non-empty write-ahead log from a running AFM")
    args = parser.parse_args(argv)

    try:
        report = migrate(args.afm_db, args.data_dir, force=args.force,
                         dry_run=args.dry_run, allow_live=args.allow_live)
    except MigrationError as exc:
        print(f"migration refused: {exc}")
        return 1
    print(report.describe())
    return 1 if report.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
