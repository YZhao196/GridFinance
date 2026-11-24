"""A read-only, CDR-shaped local API (§24).

This is not a Consumer Data Right implementation: there is no OAuth, no mTLS and no
accreditation. It exposes the same *data shapes* over ``127.0.0.1`` so a local tool can
read the ledger. The database is opened read-only for every request, and an optional PIN
hash gates the whole surface.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from finance_tool import paths
from finance_tool.store import db as dbm
from finance_tool.store.secrets import load_secrets
from finance_tool.store.store import Store, StoreError

ACCOUNT_TYPES = {
    "transaction": "TRANS_AND_SAVINGS_ACCOUNTS",
    "savings": "SAVINGS",
    "credit_card": "CRED_AND_CHRG_CARDS",
    "loan": "RESIDENTIAL_MORTGAGES",
}
NOT_FOUND = "urn:au-cds:error:cds-all:Resource/NotFound"


def hash_pin(pin: str) -> str:
    return hashlib.sha256(str(pin).encode("utf-8")).hexdigest()


def _account_id(account) -> str:
    return account.external_id or f"acc_{account.id}"


def _account_payload(account) -> dict:
    payload = {
        "accountId": _account_id(account),
        "displayName": account.name,
        "accountType": ACCOUNT_TYPES.get(account.kind, "TRANS_AND_SAVINGS_ACCOUNTS"),
        "maskedNumber": account.masked or "",
        "currency": account.currency or "",
    "balance": {"currentBalance": account.balance},
    }
    return payload


def _transaction_payload(txn, account) -> dict:
    when = txn.date.isoformat() if txn.date else None
    payload = {
        "transactionId": txn.external_id or f"txn_{txn.id}",
        "accountId": _account_id(account),
        "isDetailAvailable": False,
        "type": "CREDIT" if txn.amount > 0 else "DEBIT",
        "status": (txn.status or "posted").upper(),
        "description": txn.description,
        "postingDateTime": f"{when}T00:00:00Z" if when else None,
        "valueDateTime": f"{(txn.value_date or txn.date).isoformat()}T00:00:00Z"
                         if (txn.value_date or txn.date) else None,
        "amount": txn.amount,
    }
    if txn.merchant:
        payload["reference"] = txn.merchant
    return payload


@dataclass
class Api:
    """The route logic, separated from the socket so it can be tested directly."""

    store_loader: Callable[[], Store] = field(
        default_factory=lambda: (lambda: Store.load(paths.DATA_DIR, create=False)))
    pin_hash: str | None = None

    def get(self, path: str, headers: dict[str, str]) -> tuple[int, dict]:
        parts = urlsplit(path)
        route = parts.path.rstrip("/") or "/"
        if route == "/health":
            return 200, {"status": "ok"}

        if not self._authorised(headers):
            return 401, {"errors": [{"code": "unauthorised", "title": "Unauthorised"}]}

        try:
            store = self.store_loader()
        except (StoreError, dbm.StoreDatabaseError, sqlite3.DatabaseError, OSError) as exc:
            return 503, {"errors": [{"code": "store-unavailable", "title": str(exc)}]}

        if store.db_path is None or not store.db_path.exists():
            store.close()
            return 503, {"errors": [{"code": "store-unavailable",
                                     "title": "there is no store to read"}]}
        if any("could not be read" in note for note in store.notes):
            store.close()
            return 503, {"errors": [{"code": "store-unavailable",
                                     "title": "the store could not be read"}]}
        try:
            return self._route(store, route, parse_qs(parts.query))
        except ValueError as exc:
            return 400, {"errors": [{"code": "bad-request", "title": str(exc)}]}
        except Exception as exc:  # a broken read must be a response, not a dropped socket
            return 500, {"errors": [{"code": "server-error", "title": str(exc)}]}
        finally:
            store.close()

    def _authorised(self, headers: dict[str, str]) -> bool:
        if not self.pin_hash:
            return True
        lower = {key.lower(): value for key, value in headers.items()}
        token = ""
        authorization = lower.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        token = token or lower.get("x-pin", "")
        return secrets.compare_digest(hash_pin(token), self.pin_hash)

    def _route(self, store: Store, route: str, query: dict[str, list[str]]):
        if route == "/cds-au/v1/discovery/status":
            return 200, _envelope({"status": "OK",
                                   "updateTime": f"{date.today().isoformat()}T00:00:00Z",
                                   "explanation": "Local GridFinance compatibility layer"},
                                  route)
        if route == "/cds-au/v1/banking/accounts":
            accounts = [_account_payload(account) for account in store.view().accounts]
            return 200, _envelope({"accounts": accounts}, route,
                                  {"totalRecords": len(accounts)})
        if route.startswith("/cds-au/v1/banking/accounts/"):
            return self._account_route(store, route, query)
        return 404, _error(NOT_FOUND, "Resource not found")

    def _account_route(self, store: Store, route: str, query: dict[str, list[str]]):
        rest = route[len("/cds-au/v1/banking/accounts/"):]
        pieces = rest.split("/")
        account_id = pieces[0]
        view = store.view()
        account = view.account_by_external(account_id) or next(
            (item for item in view.accounts
             if item.id == account_id or f"acc_{item.id}" == account_id), None)
        if account is None:
            return 404, _error(NOT_FOUND, "Account not found")

        if len(pieces) == 1:
            return 200, _envelope(_account_payload(account), route)
        if pieces[1] == "balance":
            return 200, _envelope({
                "accountId": _account_id(account),
                "currentBalance": account.balance,
                "availableBalance": account.balance,
                "currency": account.currency or "",
                "balanceAsOf": account.balance_as_of.isoformat()
                               if account.balance_as_of else None,
            }, route)
        if pieces[1] == "transactions":
            return self._transactions(store, account, route, query)
        return 404, _error(NOT_FOUND, "Resource not found")

    def _transactions(self, store: Store, account, route: str,
                      query: dict[str, list[str]]):
        start = _iso(query.get("from", [""])[0])
        end = _iso(query.get("to", [""])[0])
        try:
            page_size = max(1, min(int(query.get("page-size", ["100"])[0]), 1000))
            page = max(1, int(query.get("page", ["1"])[0]))
        except ValueError as exc:
            raise ValueError("page-size and page must be integers") from exc

        rows = [txn for txn in store.view().transactions if txn.account == account.id]
        if start:
            rows = [txn for txn in rows if txn.date and txn.date >= start]
        if end:
            rows = [txn for txn in rows if txn.date and txn.date <= end]
        rows.sort(key=lambda txn: (txn.date or date.min, txn.id), reverse=True)
        total = len(rows)
        window = rows[(page - 1) * page_size: page * page_size]
        meta = {"totalRecords": total,
                "totalPages": max(1, (total + page_size - 1) // page_size)}
        if page * page_size < total:
            params = {key: values[-1] for key, values in query.items()}
            params["page"] = str(page + 1)
            params["page-size"] = str(page_size)
            meta["next"] = f"{route}?{urlencode(params)}"
        return 200, _envelope({"transactions": [
            _transaction_payload(txn, account) for txn in window]}, route, meta)


def _envelope(data: Any, route: str, meta: dict | None = None) -> dict:
    return {"data": data, "links": {"self": route}, "meta": meta or {}}


def _error(code: str, title: str) -> dict:
    return {"errors": [{"code": code, "title": title}]}


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not an ISO date") from exc


class _Handler(BaseHTTPRequestHandler):
    api: Api

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's name
        status, payload = self.api.get(self.path, dict(self.headers))
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def make_server(api: Api, *, host: str = "127.0.0.1", port: int = 0):
    handler = type("BoundHandler", (_Handler,), {"api": api})
    return ThreadingHTTPServer((host, port), handler)


def serve(*, data_dir: Path | None = None, pin_hash: str | None = None,
          host: str = "127.0.0.1", port: int = 8000) -> None:
    directory = Path(data_dir) if data_dir is not None else paths.DATA_DIR
    api = Api(store_loader=lambda: Store.load(directory, create=False), pin_hash=pin_hash)
    server = make_server(api, host=host, port=port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    secrets = load_secrets()
    serve(pin_hash=secrets.app_pin_hash)


if __name__ == "__main__":
    main()
