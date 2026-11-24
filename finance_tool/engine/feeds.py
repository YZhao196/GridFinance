"""Redbark and BankSync live feeds (§22).

Both speak JSON over HTTPS and both are written against :class:`~finance_tool.engine.sync.
Transport`, so the network seam and the test stub are the same one SimpleFIN already uses.
No new dependency.

The providers differ in shape, not in purpose: Redbark pages by offset and needs a
``connectionId`` per transaction call; BankSync pages by cursor and keys on a bank id.
Both return accounts and rows in the same :class:`FetchResult`, so the shared sync half
does not know which one answered.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode

from finance_tool.engine import importers as imp
from finance_tool.engine.sync import (
    FetchResult,
    RemoteAccount,
    RemoteTransaction,
    Response,
    SyncError,
    Transport,
    UrllibTransport,
)

REDBARK_BASE = "https://api.redbark.com"
BANKSYNC_BASE = "https://api.banksync.io"

PAGE_SIZE = 200
MAX_PAGES = 50
PENDING_STATUSES = {"pending", "authorised", "authorized"}


# ------------------------------------------------------------------------ helpers


def _first(row: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def _masked(raw: Any) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) >= 4:
        return f"****{digits[-4:]}"
    return str(raw or "")


def _account_type(raw: Any) -> str:
    text = str(raw or "").upper()
    if any(token in text for token in ("CRED", "CARD")):
        return "credit_card"
    if "SAV" in text:
        return "savings"
    if any(token in text for token in ("LOAN", "MORTGAGE")):
        return "loan"
    return "transaction"


def _amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return imp.parse_amount(str(value))


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    return imp.parse_date(text[:10])


def _epoch(when: date | None) -> int:
    if when is None:
        return 0
    return int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp())


def _rows(payload: Any) -> list[dict]:
    """A page's rows, accepting the list and single-object shapes the APIs use."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _document(payload: Any, provider: str) -> dict:
    if not isinstance(payload, dict):
        raise SyncError(f"{provider} returned an unexpected response shape")
    return payload


def _range_rejected(exc: SyncError) -> bool:
    """Whether a 400 is specifically about the date range, not anything else."""
    if exc.status != 400:
        return False
    message = str(exc).lower()
    about_dates = any(token in message for token in ("date", "range", "period"))
    about_the_range = any(token in message for token in
                          ("from", "to", "start", "end", "range", "period",
                           "before", "after", "must"))
    return about_dates and about_the_range


def _meta(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "pending"}
    return bool(value)


def _url(base: str, path: str, params: dict | None = None) -> str:
    url = base.rstrip("/") + path
    if params:
        query = urlencode({key: value for key, value in params.items()
                           if value is not None and value != ""})
        if query:
            url += "?" + query
    return url


def _check(response: Response, provider: str, context: str) -> None:
    status = response.status
    if 200 <= status < 300:
        return
    if provider == "BankSync" and status == 409:
        reconnect = "the BankSync app"
        try:
            payload = response.json()
        except SyncError:
            payload = {}
        if isinstance(payload, dict):
            reconnect = payload.get("reconnectUrl") or reconnect
        raise SyncError(
            f"the bank connection needs re-authorising — reconnect at {reconnect}",
            status=409, kind="revoked")
    if status in (401, 403):
        raise SyncError(f"{provider} rejected the API key (HTTP {status})",
                        status=status, kind="unauthorised")
    if status == 402:
        raise SyncError(f"{provider}'s subscription has lapsed (HTTP 402)",
                        status=status, kind="lapsed")
    if status == 429:
        raise SyncError(f"{provider} rate limit hit (HTTP 429); try again shortly",
                        status=status, kind="rate_limited")
    if status >= 500:
        raise SyncError(f"{provider} is unwell (HTTP {status})",
                        status=status, kind="server")
    detail = response.text[:300]
    raise SyncError(f"{provider} {context}: HTTP {status}{f' — {detail}' if detail else ''}",
                    status=status)


# ---------------------------------------------------------------------- Redbark


def _map_redbark_account(row: dict) -> RemoteAccount | None:
    account_id = _first(row, "id", "accountId", "account_id")
    if not account_id:
        return None
    connection = _first(row, "connectionId", "connection_id", "connection")
    if isinstance(connection, dict):
        connection = _first(connection, "id")
    return RemoteAccount(
        id=str(account_id),
        name=str(_first(row, "name", "displayName", "accountName",
                        default="Redbark account")),
        account_type=_account_type(_first(row, "type", "accountType", "kind")),
        currency=str(_first(row, "currency", default="AUD") or "AUD").upper(),
        masked=_masked(_first(row, "accountNumber", "maskedNumber", "number")),
        balance=None,
        connection_id=str(connection) if connection else None,
    )


def _map_redbark_transaction(row: dict, account_id: str) -> RemoteTransaction | None:
    txn_id = _first(row, "id", "transactionId")
    raw_date = _first(row, "date", "postDate", "postedAt", "valueDate")
    if not txn_id or not raw_date:
        return None
    amount = _amount(_first(row, "amount", "value"))
    if amount is None:
        return None

    direction = str(_first(row, "direction", default="") or "").lower()
    if direction == "debit" and amount > 0:
        amount = -amount
    elif direction == "credit" and amount < 0:
        amount = -amount

    status = str(_first(row, "status", default="posted") or "posted").lower()
    description = str(
        _first(row, "description", "extendedDescription", "narrative", default="") or "")
    merchant = _first(row, "merchantName", "payee", "payer", "merchant")
    category = _first(row, "customCategory", "category", "categoryName")
    posted = _date(raw_date)
    return RemoteTransaction(
        id=str(txn_id),
        amount=amount,
        description=description,
        posted=posted,
        posted_epoch=_epoch(posted),
        pending=status in PENDING_STATUSES,
        merchant=str(merchant) if merchant else None,
        category=str(category) if category else None,
    )


class _Redbark:
    def __init__(self, api_key: str, base_url: str, transport: Transport):
        self.key = api_key
        self.base = base_url
        self.transport = transport

    def _get(self, path: str, params: dict | None = None) -> Any:
        response = self.transport.request(
            "GET", _url(self.base, path, params),
            headers={"Authorization": f"Bearer {self.key}",
                     "Accept": "application/json",
                     "User-Agent": "gridfinance"})
        _check(response, "Redbark", path)
        return _document(response.json(), "Redbark")

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        offset = 0
        more = False
        for _ in range(MAX_PAGES):
            payload = self._get(path, {**(params or {}), "limit": PAGE_SIZE,
                                       "offset": offset})
            page_rows = _rows(payload)
            out.extend(page_rows)
            pagination = _meta(payload, "pagination")
            more = _truthy(pagination.get("hasMore"))
            if not more:
                break
            if not page_rows:
                raise SyncError(
                    f"Redbark reported another page but returned no rows at offset "
                    f"{offset}", kind="truncated")
            # Advance by the rows actually returned. A server that caps a page while
            # echoing a larger limit would otherwise skip the rows it held back.
            offset += len(page_rows)
        if more:
            raise SyncError(
                f"Redbark returned more than {MAX_PAGES} pages; some rows were not read",
                kind="truncated")
        return out

    def balances(self, account_ids: list[str]) -> dict[str, tuple[float | None, float | None]]:
        if not account_ids:
            return {}
        payload = self._get("/v1/balances", {"accountIds": ",".join(account_ids)})
        out: dict[str, tuple[float | None, float | None]] = {}
        for row in _rows(payload):
            if not isinstance(row, dict):
                continue
            account_id = _first(row, "accountId", "account_id", "id")
            if not account_id:
                continue
            out[str(account_id)] = (
                _amount(_first(row, "currentBalance", "current_balance", "balance")),
                _amount(_first(row, "availableBalance", "available_balance")),
            )
        return out

    def accounts(self) -> list[RemoteAccount]:
        mapped = [account for account in
                  (_map_redbark_account(row) for row in self._paged("/v1/accounts"))
                  if account is not None]
        if mapped:
            balances = self.balances([account.id for account in mapped])
            mapped = [
                replace(account, balance=balances[account.id][0],
                        available=balances[account.id][1])
                if account.id in balances else account
                for account in mapped
            ]
        return mapped

    def transactions(self, account: RemoteAccount, since: date, today: date,
                     include_pending: bool) -> tuple[list[RemoteTransaction], int, int]:
        if not account.connection_id:
            raise SyncError(f"{account.name}: Redbark did not report a connection id")

        rows: list[dict] = []
        ranged = True
        offset = 0
        more = False
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "connectionId": account.connection_id,
                "accountId": account.id,
                "limit": PAGE_SIZE,
                "offset": offset,
            }
            if ranged:
                params["from"] = since.isoformat()
                params["to"] = today.isoformat()
            try:
                payload = self._get("/v1/transactions", params)
            except SyncError as exc:
                # Older Redbark keys reject a date range. Drop it and filter locally
                # rather than losing the account.
                if ranged and _range_rejected(exc):
                    ranged, offset, rows = False, 0, []
                    continue
                raise
            page_rows = _rows(payload)
            rows.extend(page_rows)
            pagination = _meta(payload, "pagination")
            more = _truthy(pagination.get("hasMore"))
            if not more:
                break
            if not page_rows:
                raise SyncError(
                    f"{account.name}: Redbark reported another page but returned no rows "
                    f"at offset {offset}", kind="truncated")
            offset += len(page_rows)
        if more:
            raise SyncError(
                f"{account.name}: Redbark returned more than {MAX_PAGES} pages; some "
                "rows were not read", kind="truncated")

        mapped: list[RemoteTransaction] = []
        unreadable = skipped_pending = 0
        for row in rows:
            txn = _map_redbark_transaction(row, account.id)
            if txn is None:
                unreadable += 1
                continue
            if not ranged and txn.posted is not None and txn.posted < since:
                continue
            if txn.pending and not include_pending:
                skipped_pending += 1
                continue
            mapped.append(txn)
        return mapped, skipped_pending, unreadable


def fetch_redbark(
    api_key: str,
    *,
    since: date,
    today: date,
    include_pending: bool = False,
    base_url: str = REDBARK_BASE,
    transport: Transport | None = None,
) -> FetchResult:
    """Every Redbark account and its transactions since ``since``."""
    if not api_key:
        raise SyncError("no Redbark API key is configured — reconnect required",
                        status=401, kind="unauthorised")
    client = _Redbark(api_key, base_url, transport or UrllibTransport())
    accounts = client.accounts()

    results: list[RemoteAccount] = []
    errors: list[str] = []
    failed_ids: list[str] = []
    warnings: list[str] = []
    skipped_pending = 0
    for account in accounts:
        try:
            rows, skipped, unreadable = client.transactions(
                account, since, today, include_pending)
        except SyncError as exc:
            errors.append(f"{account.name}: {exc}")
            failed_ids.append(account.id)
            results.append(replace(account, transactions=[]))
            continue
        results.append(replace(account, transactions=rows))
        skipped_pending += skipped
        if unreadable:
            warnings.append(
                f"{unreadable} transaction(s) from {account.name} could not be read")

    return FetchResult(accounts=results, warnings=warnings, errors=errors,
                       failed_ids=failed_ids, skipped_pending=skipped_pending,
                       provider="redbark")


# --------------------------------------------------------------------- BankSync


def _map_banksync_account(row: dict) -> RemoteAccount | None:
    account_id = _first(row, "id", "accountId", "account_id")
    if not account_id:
        return None
    balance = _amount(row.get("balance")) if row.get("balance") is not None else None
    available = (_amount(row.get("availableBalance"))
                 if row.get("availableBalance") is not None else None)
    return RemoteAccount(
        id=str(account_id),
        name=str(_first(row, "accountName", "name", "accountType",
                        default="BankSync account")),
        account_type=_account_type(_first(row, "accountType", "type")),
        currency=str(row.get("currency") or "AUD").upper(),
        masked=_masked(row.get("accountNumber")),
        balance=balance,
        available=available,
    )


def _map_banksync_transaction(row: dict, account_id: str) -> RemoteTransaction | None:
    txn_id = _first(row, "id", "transactionId")
    posted = _date(row.get("date") or row.get("postedAt"))
    if not txn_id or posted is None:
        return None
    amount = _amount(row.get("amount"))
    if amount is None:
        return None
    return RemoteTransaction(
        id=str(txn_id),
        amount=amount,
        description=str(row.get("description") or ""),
        posted=posted,
        posted_epoch=_epoch(posted),
        pending=_truthy(row.get("pending")),
        merchant=row.get("merchantName") or None,
        category=row.get("category") or None,
    )


class _BankSync:
    def __init__(self, api_key: str, bank_id: str, base_url: str, transport: Transport):
        self.key = api_key
        self.bank = bank_id
        self.base = base_url
        self.transport = transport

    def _get(self, path: str, params: dict | None = None) -> Any:
        response = self.transport.request(
            "GET", _url(self.base, path, params),
            headers={"X-API-Key": self.key, "Accept": "application/json",
                     "User-Agent": "gridfinance"})
        _check(response, "BankSync", path)
        return _document(response.json(), "BankSync")

    def accounts(self) -> list[RemoteAccount]:
        rows: list[dict] = []
        cursor: str | None = None
        more = False
        for _ in range(MAX_PAGES):
            params = {"cursor": cursor} if cursor else None
            payload = self._get(f"/v1/banks/{self.bank}/accounts", params)
            page_rows = _rows(payload)
            rows.extend(page_rows)
            meta = _meta(payload, "meta")
            more = _truthy(meta.get("hasMore"))
            cursor = meta.get("cursor")
            if not more:
                break
            if not cursor:
                raise SyncError("BankSync reported more account pages but gave no cursor",
                                kind="truncated")
        if more:
            raise SyncError(
                f"BankSync returned more than {MAX_PAGES} account pages; some accounts "
                "were not read", kind="truncated")
        return [account for account in
                (_map_banksync_account(row) for row in rows)
                if account is not None]

    def transactions(self, account: RemoteAccount, since: date, today: date,
                     include_pending: bool) -> tuple[list[RemoteTransaction], int, int]:
        out: list[RemoteTransaction] = []
        cursor: str | None = None
        unreadable = skipped_pending = 0
        more = False
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"from": since.isoformat(), "to": today.isoformat()}
            if cursor:
                params["cursor"] = cursor
            payload = self._get(
                f"/v1/banks/{self.bank}/accounts/{account.id}/transactions", params)
            for row in _rows(payload):
                txn = _map_banksync_transaction(row, account.id)
                if txn is None:
                    unreadable += 1
                    continue
                if txn.pending and not include_pending:
                    skipped_pending += 1
                    continue
                out.append(txn)
            meta = _meta(payload, "meta")
            more = _truthy(meta.get("hasMore"))
            cursor = meta.get("cursor")
            if not more:
                break
            if not cursor:
                raise SyncError(
                    f"{account.name}: BankSync reported more pages but gave no cursor",
                    kind="truncated")
        if more:
            raise SyncError(
                f"{account.name}: BankSync returned more than {MAX_PAGES} pages; some "
                "rows were not read", kind="truncated")
        return out, skipped_pending, unreadable


def fetch_banksync(
    api_key: str,
    *,
    bank_id: str,
    since: date,
    today: date,
    include_pending: bool = False,
    base_url: str = BANKSYNC_BASE,
    transport: Transport | None = None,
) -> FetchResult:
    """Every BankSync account for one bank and its transactions since ``since``."""
    if not api_key:
        raise SyncError("no BankSync API key is configured — reconnect required",
                        status=401, kind="unauthorised")
    client = _BankSync(api_key, bank_id or "bnk_anz", base_url,
                       transport or UrllibTransport())
    accounts = client.accounts()

    results: list[RemoteAccount] = []
    errors: list[str] = []
    failed_ids: list[str] = []
    warnings: list[str] = []
    skipped_pending = 0
    for account in accounts:
        try:
            rows, skipped, unreadable = client.transactions(
                account, since, today, include_pending)
        except SyncError as exc:
            errors.append(f"{account.name}: {exc}")
            failed_ids.append(account.id)
            results.append(replace(account, transactions=[]))
            continue
        results.append(replace(account, transactions=rows))
        skipped_pending += skipped
        if unreadable:
            warnings.append(
                f"{unreadable} transaction(s) from {account.name} could not be read")

    return FetchResult(accounts=results, warnings=warnings, errors=errors,
                       failed_ids=failed_ids, skipped_pending=skipped_pending,
                       provider="banksync")
