"""Bank sync over SimpleFIN (§22).

SimpleFIN reaches Australian bank data through the Consumer Data Right via a bridge, so
a personal desktop app never has to be an Accredited Data Recipient. It also returns **no
category** — only a description — which is why §24's categorisation is load-bearing for
this feature rather than a nicety.

Three entry points, stdlib ``urllib`` only, no new dependency:

* :func:`claim` — base64-decode the pasted setup token, ``POST`` it, get an Access URL.
* :func:`fetch_accounts` — walk accounts and their transactions, epoch → ISO date.
* :func:`sync` — feed those rows through the import pipeline.

**Failure must never look like success.** A revoked connection, a lapsed subscription and
an expired consent all surface as an explicit error, because a silent "no new
transactions" is the failure mode that makes a user stop trusting the app.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from finance_tool.engine import importers as imp
from finance_tool.store.entities import Account, Rule
from finance_tool.store.secrets import Secrets, save_secrets
from finance_tool.store.store import StoreView, new_id

DEFAULT_TIMEOUT = 25.0
DEFAULT_LOOKBACK_DAYS = 90
# Banks post a transaction days after it happens, so a strictly incremental fetch misses
# anything that lands late. The window is pulled back by a few days and dedupe absorbs
# the overlap, which costs nothing and is the difference between a complete ledger and a
# quietly incomplete one.
OVERLAP_DAYS = 5


# ------------------------------------------------------------------------ failures


class SyncError(Exception):
    """Anything that stopped a sync from happening. Always shown, never swallowed."""

    def __init__(self, message: str, *, status: int | None = None, kind: str = "error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind

    @property
    def needs_reconnect(self) -> bool:
        """A consent that has to be redone: revoked, lapsed, or rejected (§22)."""
        return self.kind in {"revoked", "lapsed", "unauthorised"} or self.status in {401, 402, 403}

    @property
    def is_transient(self) -> bool:
        return self.status is not None and self.status >= 500

    def __str__(self) -> str:
        return self.message


class TokenAlreadyClaimed(SyncError):
    def __init__(self, message: str = "this setup token has already been claimed"):
        super().__init__(message, status=403, kind="revoked")


class ConnectionRevoked(SyncError):
    def __init__(self, message: str = "the bank connection has been revoked — reconnect required"):
        super().__init__(message, status=403, kind="revoked")


class SubscriptionLapsed(SyncError):
    def __init__(self, message: str = "the SimpleFIN bridge subscription has lapsed"):
        super().__init__(message, status=402, kind="lapsed")


# ----------------------------------------------------------------------- transport

@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise SyncError("the bridge returned something that is not JSON") from exc


class Transport(Protocol):
    """The seam tests stub. Everything above it is protocol logic; nothing below is."""

    def request(self, method: str, url: str, *, data: bytes | None = None,
                headers: dict[str, str] | None = None) -> Response: ...


class UrllibTransport:
    """The real one: stdlib only, no new dependency (§22)."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def request(self, method: str, url: str, *, data: bytes | None = None,
                headers: dict[str, str] | None = None) -> Response:
        request = Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as handle:
                return Response(status=handle.status, body=handle.read(),
                                headers=dict(handle.headers))
        except HTTPError as exc:
            # An HTTP error is a response with a status, and the status is the message.
            return Response(status=exc.code, body=exc.read() or b"", headers=dict(exc.headers or {}))
        except URLError as exc:
            raise SyncError(f"could not reach the bridge: {exc.reason}") from exc
        except OSError as exc:
            raise SyncError(f"could not reach the bridge: {exc}") from exc


def _raise_for_status(response: Response, context: str) -> None:
    if 200 <= response.status < 300:
        return
    if response.status == 403:
        raise ConnectionRevoked()
    if response.status == 402:
        raise SubscriptionLapsed()
    if response.status in (401, 404):
        raise SyncError(f"{context}: the bridge rejected the credential (HTTP {response.status})",
                        status=response.status, kind="unauthorised")
    if response.status >= 500:
        raise SyncError(f"{context}: the bridge is unwell (HTTP {response.status})",
                        status=response.status, kind="server")
    raise SyncError(f"{context}: HTTP {response.status}", status=response.status)


# ------------------------------------------------------------------------- the token


def decode_token(token: str) -> str:
    """The setup token is base64 of a claim URL. Anything else is refused, not guessed."""
    cleaned = re.sub(r"\s+", "", token or "")
    if not cleaned:
        raise SyncError("no setup token was given")
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8").strip()
    except Exception as exc:
        raise SyncError("that does not look like a SimpleFIN setup token") from exc
    if not decoded.lower().startswith(("http://", "https://")):
        raise SyncError("the setup token did not decode to a URL")
    return decoded


def claim(token: str, *, transport: Transport | None = None) -> str:
    """Exchange a setup token for an Access URL.

    A ``403`` means the token was already claimed — one clear message and no retry, since
    retrying a claimed token can only fail the same way.
    """
    url = decode_token(token)
    client = transport or UrllibTransport()
    response = client.request("POST", url, data=b"",
                              headers={"Content-Length": "0", "Accept": "*/*"})
    if response.status == 403:
        raise TokenAlreadyClaimed()
    _raise_for_status(response, "claiming the setup token")

    access_url = response.text.strip()
    if not access_url.lower().startswith(("http://", "https://")):
        raise SyncError("the bridge did not return an Access URL")
    return access_url


def claim_and_store(
    secrets: Secrets,
    token: str,
    *,
    transport: Transport | None = None,
    path=None,
) -> str:
    """Claim a token and write the credential, which is the only thing secrets hold."""
    access_url = claim(token, transport=transport)
    secrets.set("simplefin_access_url", access_url)
    save_secrets(secrets, path)
    return access_url


def revoke(secrets: Secrets, *, path=None) -> None:
    """Revoking is a delete: a removed credential leaves no residue (§13)."""
    secrets.revoke("simplefin_access_url")
    save_secrets(secrets, path)


# ------------------------------------------------------------------ access URL auth


def split_credentials(url: str) -> tuple[str, str | None]:
    """An Access URL embeds Basic Auth, which is why it *is* the credential (§22)."""
    parts = urlsplit(url)
    if not parts.username:
        return url, None
    user = parts.username
    password = parts.password or ""
    credentials = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)), \
        f"Basic {credentials}"


def _auth_headers(access_url: str) -> tuple[str, dict[str, str]]:
    clean, authorization = split_credentials(access_url)
    headers = {"Accept": "application/json", "User-Agent": "finance-tool"}
    if authorization:
        headers["Authorization"] = authorization
    return clean.rstrip("/"), headers


# ------------------------------------------------------------------------ payloads


@dataclass(frozen=True)
class RemoteTransaction:
    id: str
    amount: float
    description: str
    posted: date | None = None
    posted_epoch: int = 0
    pending: bool = False
    transacted_at: int = 0


@dataclass(frozen=True)
class RemoteAccount:
    id: str
    name: str = ""
    currency: str = ""
    balance: float = 0.0
    transactions: list[RemoteTransaction] = field(default_factory=list)

    @property
    def latest_epoch(self) -> int:
        return max((t.posted_epoch for t in self.transactions), default=0)


@dataclass(frozen=True)
class FetchResult:
    accounts: list[RemoteAccount] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def transactions(self) -> int:
        return sum(len(a.transactions) for a in self.accounts)


def epoch_to_date(epoch: Any) -> date | None:
    """SimpleFIN timestamps are unix seconds; a pending row has ``0`` and no date."""
    try:
        seconds = int(epoch)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def parse_amount(value: Any) -> float | None:
    """SimpleFIN amounts arrive as strings and are already signed the app's way."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def parse_accounts(payload: dict[str, Any], *, include_pending: bool = False) -> FetchResult:
    """The ``/accounts`` document as typed data, skipping anything unreadable."""
    warnings: list[str] = []
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise SyncError("the bridge response had no accounts list")

    parsed: list[RemoteAccount] = []
    skipped = 0
    for entry in accounts:
        if not isinstance(entry, dict) or not entry.get("id"):
            skipped += 1
            continue
        transactions: list[RemoteTransaction] = []
        for row in entry.get("transactions") or []:
            if not isinstance(row, dict):
                skipped += 1
                continue
            amount = parse_amount(row.get("amount"))
            if amount is None:
                skipped += 1
                continue
            pending = bool(row.get("pending"))
            if pending and not include_pending:
                continue
            posted_epoch = row.get("posted") or row.get("transacted_at") or 0
            transactions.append(RemoteTransaction(
                id=str(row.get("id") or ""),
                amount=amount,
                description=" ".join(str(row.get("description") or "").split()),
                posted=epoch_to_date(posted_epoch),
                posted_epoch=int(posted_epoch) if str(posted_epoch).isdigit() else 0,
                pending=pending,
                transacted_at=int(row.get("transacted_at") or 0),
            ))

        balance = parse_amount(entry.get("balance")) or 0.0
        parsed.append(RemoteAccount(
            id=str(entry["id"]),
            name=str(entry.get("name") or entry.get("id")),
            currency=str(entry.get("currency") or ""),
            balance=balance,
            transactions=transactions,
        ))

    if skipped:
        warnings.append(f"{skipped} rows from the bridge could not be read")
    return FetchResult(accounts=parsed, warnings=warnings, raw=payload)


def fetch_accounts(
    access_url: str,
    *,
    since: date | None = None,
    include_pending: bool = False,
    transport: Transport | None = None,
) -> FetchResult:
    """Every account and its transactions, optionally from ``since`` forwards."""
    base, headers = _auth_headers(access_url)
    query = "/accounts"
    if since is not None:
        epoch = int(datetime(since.year, since.month, since.day,
                             tzinfo=timezone.utc).timestamp())
        query += f"?start-date={epoch}"
    client = transport or UrllibTransport()
    response = client.request("GET", base + query, headers=headers)
    _raise_for_status(response, "fetching accounts")
    return parse_accounts(response.json(), include_pending=include_pending)


# --------------------------------------------------------------------------- gating


def sync_enabled(view: StoreView, secrets: Secrets | None = None) -> bool:
    """Off by default, so file import remains the always-on path (§22)."""
    enabled = bool(view.settings.enable_bank_sync)
    if secrets is not None and not secrets.has("simplefin_access_url"):
        return False
    return enabled


# -------------------------------------------------------------------------- the sync


@dataclass(frozen=True)
class SyncResult:
    """A sync's outcome, in a shape that cannot be mistaken for "nothing new"."""

    ok: bool = False
    summary: imp.ImportSummary = field(default_factory=imp.ImportSummary)
    error: SyncError | None = None
    accounts: int = 0
    skipped_accounts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    marks: dict[str, int] = field(default_factory=dict)
    settled: int = 0

    @property
    def needs_reconnect(self) -> bool:
        return self.error is not None and self.error.needs_reconnect

    @property
    def imported(self) -> int:
        return self.summary.imported

    def describe(self) -> str:
        if not self.ok:
            return f"Sync failed: {self.error}" if self.error else "Sync failed"
        parts = [self.summary.describe()]
        if self.settled:
            parts.append(f"{self.settled} marked paid")
        if self.skipped_accounts:
            parts.append(f"{len(self.skipped_accounts)} account(s) skipped")
        return "; ".join(parts)


def _local_account(store, remote: RemoteAccount) -> Account:
    existing = store.view().account_by_external(remote.id)
    if existing is not None:
        existing.name = remote.name or existing.name
        existing.balance = remote.balance
        return existing
    return store.add("accounts", Account(
        id=new_id("accounts"), name=remote.name, kind="transaction",
        balance=remote.balance, currency=remote.currency, external_id=remote.id,
    ))


def _since_for(store, remote_id: str, *, today: date, lookback_days: int) -> date:
    account = store.view().account_by_external(remote_id)
    if account is None or not account.high_water:
        return today - timedelta(days=lookback_days)
    marked = epoch_to_date(account.high_water) or today
    return marked - timedelta(days=OVERLAP_DAYS)


def sync(
    store,
    *,
    access_url: str,
    today: date,
    imported_at: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    transport: Transport | None = None,
    rules: list[Rule] | None = None,
    include_pending: bool = False,
    since: date | None = None,
) -> SyncResult:
    """Pull the delta for every account and run it through the import pipeline.

    Incremental by a per-account high-water mark, pulled back by a few days so a
    late-posted transaction is not missed; the overlap is re-fetched and deduped, which
    is cheaper than a gap.

    ``today`` is required rather than defaulted to the clock: it decides the lookback
    window, so a caller who forgot it would silently reach for real time and make the run
    unreproducible (§15).
    """
    if not store.settings.enable_bank_sync:
        return SyncResult(ok=False, error=SyncError(
            "bank sync is turned off in Settings", status=None, kind="disabled"))
    if not access_url:
        return SyncResult(ok=False, error=SyncError(
            "no bank connection is configured — reconnect required",
            status=401, kind="unauthorised"))

    currency = store.settings.currency
    start = since
    try:
        if start is None:
            marks = [
                _since_for(store, a.external_id, today=today, lookback_days=lookback_days)
                for a in store.view().accounts if a.external_id
            ]
            start = min(marks) if marks else today - timedelta(days=lookback_days)
        fetched = fetch_accounts(access_url, since=start, transport=transport,
                                 include_pending=include_pending)
    except SyncError as exc:
        return SyncResult(ok=False, error=exc)

    summaries: list[imp.ImportSummary] = []
    skipped: list[str] = []
    warnings = list(fetched.warnings)
    marks_out: dict[str, int] = {}

    for remote in fetched.accounts:
        local = _local_account(store, remote)

        if remote.currency and remote.currency != currency:
            # §22: flagged, never summed. The account and its balance are recorded so it
            # is visible, and not one transaction from it is imported.
            skipped.append(f"{remote.name} ({remote.currency})")
            warnings.append(
                f"{remote.name} is in {remote.currency} and the app works in {currency}; "
                "its transactions were not imported")
            continue

        if not local.currency:
            local.currency = currency

        usable = [t for t in remote.transactions if t.posted is not None]
        undated = len(remote.transactions) - len(usable)
        if undated:
            # Counted and said out loud. A transaction the bridge sent with a date this
            # app cannot read must not disappear into "no new transactions".
            warnings.append(
                f"{undated} transaction(s) from {remote.name} had no readable date and "
                "were not imported")
        rows = [
            imp.RawTxn(date=t.posted, amount=t.amount, description=t.description,
                       external_id=t.id or None)
            for t in usable
        ]
        summary = imp.import_rows(
            store, rows, source=f"simplefin:{remote.id}", account=local.id,
            rules=rules, now=imported_at,
        )
        summaries.append(summary)

        high_water = remote.latest_epoch or (local.high_water or 0)
        local.high_water = high_water
        marks_out[remote.id] = high_water

    if not fetched.accounts:
        warnings.append("the bridge returned no accounts")

    return SyncResult(
        ok=True,
        summary=imp.rolled_up_import_summaries(summaries),
        accounts=len(fetched.accounts),
        skipped_accounts=skipped,
        warnings=warnings,
        marks=marks_out,
    )


def run_sync(
    store,
    secrets: Secrets,
    *,
    today: date,
    imported_at: datetime | None = None,
    transport: Transport | None = None,
    rules: list[Rule] | None = None,
) -> SyncResult:
    """What the UI calls: read the credential, check the switch, sync."""
    if not store.settings.enable_bank_sync:
        return SyncResult(ok=False, error=SyncError(
            "bank sync is turned off in Settings", kind="disabled"))
    access_url = secrets.simplefin_access_url
    if not access_url:
        return SyncResult(ok=False, error=SyncError(
            "no bank connection is configured — reconnect required",
            status=401, kind="unauthorised"))
    return sync(store, access_url=access_url, today=today, imported_at=imported_at,
                transport=transport, rules=rules)


def high_water_date(store, remote_id: str) -> date | None:
    account = store.view().account_by_external(remote_id)
    return epoch_to_date(account.high_water) if account and account.high_water else None


def is_simplefin_url(text: str) -> bool:
    """A cheap sanity check for a pasted Access URL, for the settings form."""
    if not text:
        return False
    parts = urlsplit(text)
    return bool(parts.scheme in {"http", "https"} and parts.hostname)
