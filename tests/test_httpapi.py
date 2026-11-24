"""The read-only CDR-shaped API and its PIN gate (§24)."""

import json
import threading
import urllib.request
from datetime import date

import pytest

from finance_tool.httpapi import Api, hash_pin, make_server
from finance_tool.store.entities import Account, Settings, StoreDocument, Transaction
from finance_tool.store.store import Store


def _seed(tmp_path):
    store = Store(data_dir=tmp_path, doc=StoreDocument(
        accounts=[Account(id="a_1", name="Everyday", kind="transaction",
                          balance=100.5, currency="AUD", external_id="ext-1",
                          masked="****5678", balance_as_of=date(2026, 10, 31)),
                  Account(id="a_2", name="Savings", kind="savings",
                          balance=0.0, currency="AUD")],
        transactions=[
            Transaction(id="t1", date=date(2026, 10, 1), amount=-10.0,
                        description="COLES", account="a_1",
                        external_id="x1", merchant="Coles", status="posted",
                        value_date=date(2026, 10, 2)),
            Transaction(id="t2", date=date(2026, 10, 2), amount=20.0,
                        description="SALARY", account="a_1", status="pending"),
            Transaction(id="t3", date=date(2026, 9, 1), amount=-5.0,
                        description="OLD", account="a_1"),
        ],
    ), settings=Settings())
    store.save()
    store.close()


def _api(tmp_path, pin_hash=None):
    return Api(store_loader=lambda: Store.load(tmp_path, create=False),
               pin_hash=pin_hash)


def test_accounts_use_the_cdr_shape(tmp_path):
    _seed(tmp_path)

    status, payload = _api(tmp_path).get("/cds-au/v1/banking/accounts", {})

    assert status == 200
    account = payload["data"]["accounts"][0]
    assert account["accountId"] == "ext-1"
    assert account["displayName"] == "Everyday"
    assert account["accountType"] == "TRANS_AND_SAVINGS_ACCOUNTS"
    assert account["maskedNumber"] == "****5678"
    assert account["balance"] == {"currentBalance": 100.5}
    assert payload["meta"]["totalRecords"] == 2


def test_an_account_without_an_external_id_is_fetchable_by_its_advertised_id(tmp_path):
    _seed(tmp_path)
    api = _api(tmp_path)

    status, listing = api.get("/cds-au/v1/banking/accounts", {})
    advertised = listing["data"]["accounts"][1]["accountId"]
    assert advertised == "acc_a_2"

    status, payload = api.get(f"/cds-au/v1/banking/accounts/{advertised}", {})
    assert status == 200
    assert payload["data"]["balance"] == {"currentBalance": 0.0}

    status, balance = api.get(f"/cds-au/v1/banking/accounts/{advertised}/balance", {})
    assert status == 200 and balance["data"]["currentBalance"] == 0.0


def test_a_single_account_and_its_balance(tmp_path):
    _seed(tmp_path)
    api = _api(tmp_path)

    status, payload = api.get("/cds-au/v1/banking/accounts/ext-1", {})
    assert status == 200 and payload["data"]["accountId"] == "ext-1"

    status, payload = api.get("/cds-au/v1/banking/accounts/ext-1/balance", {})
    assert status == 200
    assert payload["data"]["currentBalance"] == 100.5
    assert payload["data"]["balanceAsOf"] == "2026-10-31"


def test_transactions_page_with_iso_dates(tmp_path):
    _seed(tmp_path)
    api = _api(tmp_path)

    status, payload = api.get(
        "/cds-au/v1/banking/accounts/ext-1/transactions"
        "?from=2026-10-01&to=2026-10-31&page-size=1&page=1", {})

    assert status == 200
    assert payload["meta"] == {
        "totalRecords": 2, "totalPages": 2,
        "next": "/cds-au/v1/banking/accounts/ext-1/transactions"
                "?from=2026-10-01&to=2026-10-31&page-size=1&page=2"}
    txn = payload["data"]["transactions"][0]
    assert txn["transactionId"] == "txn_t2"       # newest first, no external id
    assert txn["type"] == "CREDIT"
    assert txn["status"] == "PENDING"
    assert txn["amount"] == 20.0
    assert txn["postingDateTime"] == "2026-10-02T00:00:00Z"

    status, second = api.get(
        "/cds-au/v1/banking/accounts/ext-1/transactions"
        "?from=2026-10-01&to=2026-10-31&page-size=1&page=2", {})
    assert status == 200
    assert second["data"]["transactions"][0]["transactionId"] == "x1"
    assert second["data"]["transactions"][0]["valueDateTime"] == "2026-10-02T00:00:00Z"


def test_unknown_resources_and_bad_queries(tmp_path):
    _seed(tmp_path)
    api = _api(tmp_path)

    assert api.get("/cds-au/v1/banking/accounts/nope", {})[0] == 404
    assert api.get("/cds-au/v1/nothing", {})[0] == 404
    assert api.get("/cds-au/v1/banking/accounts/ext-1/transactions?from=bad",
                   {})[0] == 400
    assert api.get("/cds-au/v1/banking/accounts/ext-1/transactions?page-size=x",
                   {})[0] == 400


def test_a_missing_store_is_service_unavailable_not_empty(tmp_path):
    api = Api(store_loader=lambda: Store.load(tmp_path / "missing", create=False))

    status, payload = api.get("/cds-au/v1/banking/accounts", {})

    assert status == 503
    assert payload["errors"][0]["code"] == "store-unavailable"


def test_the_pin_gate(tmp_path):
    _seed(tmp_path)
    api = _api(tmp_path, pin_hash=hash_pin("1234"))

    assert api.get("/health", {})[0] == 200
    assert api.get("/cds-au/v1/banking/accounts", {})[0] == 401
    assert api.get("/cds-au/v1/banking/accounts",
                   {"Authorization": "Bearer wrong"})[0] == 401
    assert api.get("/cds-au/v1/banking/accounts",
                   {"Authorization": "Bearer 1234"})[0] == 200
    assert api.get("/cds-au/v1/banking/accounts", {"X-PIN": "1234"})[0] == 200


def test_discovery_status(tmp_path):
    _seed(tmp_path)
    status, payload = _api(tmp_path).get("/cds-au/v1/discovery/status", {})
    assert status == 200
    assert payload["data"]["status"] == "OK"


def test_the_server_answers_over_http(tmp_path):
    _seed(tmp_path)
    api = _api(tmp_path)
    server = make_server(api, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/cds-au/v1/banking/accounts") as response:
            payload = json.loads(response.read())
        assert payload["data"]["accounts"][0]["accountId"] == "ext-1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
