"""Redbark and BankSync against a stubbed transport, and the provider-selection seam."""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from finance_tool.engine import categorise as cat
from finance_tool.engine import feeds
from finance_tool.engine import sync as sy
from finance_tool.store.entities import Account, Rule, Settings, StoreDocument
from finance_tool.store.secrets import Secrets
from finance_tool.store.store import Store

SINCE = date(2026, 10, 1)
TODAY = date(2026, 11, 5)


# ------------------------------------------------------------------- the stub


@dataclass
class Call:
    method: str
    url: str
    data: bytes | None = None
    headers: dict = field(default_factory=dict)


class Stub:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[Call] = []

    def request(self, method, url, *, data=None, headers=None):
        call = Call(method, url, data, dict(headers or {}))
        self.calls.append(call)
        return self.handler(call)

    @property
    def urls(self) -> list[str]:
        return [call.url for call in self.calls]


def json_response(payload, status=200):
    return sy.Response(status=status, body=json.dumps(payload).encode("utf-8"))


def query(call: Call) -> dict[str, str]:
    return {key: values[-1] for key, values in
            parse_qs(urlsplit(call.url).query).items()}


def epoch(when: date) -> int:
    return int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------------- Redbark


REDBARK_ACCOUNT = {
    "id": "acc_1", "connectionId": "conn_1", "name": "Everyday",
    "type": "transaction", "currency": "AUD", "accountNumber": "12345678",
}
REDBARK_TRANSACTIONS = [
    {"id": "r1", "date": "2026-11-01", "amount": "57.80", "direction": "debit",
     "status": "posted", "description": "COLES 1234", "merchantName": "Coles",
     "category": "Groceries", "currency": "AUD"},
    {"id": "r2", "date": "2026-11-02", "amount": "2500.00", "direction": "credit",
     "status": "posted", "description": "SALARY ACME", "category": "Salary",
     "currency": "AUD"},
    {"id": "r3", "date": "2026-11-03", "amount": "4.50", "direction": "debit",
     "status": "pending", "description": "PENDING COFFEE", "category": None,
     "currency": "AUD"},
]


def redbark_handler(call: Call):
    if "/v1/accounts" in call.url:
        return json_response({"data": [REDBARK_ACCOUNT],
                              "pagination": {"hasMore": False}})
    if "/v1/balances" in call.url:
        return json_response({"data": [{
            "accountId": "acc_1", "currentBalance": "2400.00",
            "availableBalance": "2300.00"}]})
    if "/v1/transactions" in call.url:
        return json_response({"data": REDBARK_TRANSACTIONS,
                              "pagination": {"hasMore": False}})
    return json_response({}, 404)


def test_redbark_maps_accounts_balances_and_transactions():
    stub = Stub(redbark_handler)

    fetched = feeds.fetch_redbark("rb-key", since=SINCE, today=TODAY, transport=stub)

    assert fetched.provider == "redbark"
    account = fetched.accounts[0]
    assert account.name == "Everyday"
    assert account.balance == pytest.approx(2400.0)
    assert account.available == pytest.approx(2300.0)
    assert account.masked == "****5678"
    assert account.connection_id == "conn_1"
    assert [txn.amount for txn in account.transactions] == [-57.8, 2500.0]
    assert account.transactions[0].merchant == "Coles"
    assert account.transactions[0].category == "Groceries"
    assert fetched.skipped_pending == 1
    assert stub.calls[0].headers["Authorization"] == "Bearer rb-key"
    tx_calls = [call for call in stub.calls if "/v1/transactions" in call.url]
    assert query(tx_calls[0])["from"] == "2026-10-01"


def test_redbark_pages_accounts_by_offset():
    def handler(call):
        if "/v1/accounts" in call.url:
            if query(call).get("offset") == "0":
                return json_response({"data": [REDBARK_ACCOUNT],
                                      "pagination": {"hasMore": True, "limit": 1}})
            second = dict(REDBARK_ACCOUNT, id="acc_2", name="Savings",
                          accountNumber="87654321")
            return json_response({"data": [second], "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [], "pagination": {"hasMore": False}})

    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=Stub(handler))

    assert [account.id for account in fetched.accounts] == ["acc_1", "acc_2"]


def test_redbark_401_needs_a_reconnect():
    stub = Stub(lambda call: sy.Response(401, b""))
    with pytest.raises(sy.SyncError) as caught:
        feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=stub)
    assert caught.value.needs_reconnect is True


def test_redbark_rate_limit_is_not_a_reconnect():
    stub = Stub(lambda call: sy.Response(429, b""))
    with pytest.raises(sy.SyncError) as caught:
        feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=stub)
    assert caught.value.status == 429
    assert caught.value.needs_reconnect is False


def test_one_failing_account_does_not_hide_the_other():
    second = dict(REDBARK_ACCOUNT, id="acc_2", name="Savings")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT, second],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        if "/v1/transactions" in call.url:
            if query(call).get("accountId") == "acc_2":
                return sy.Response(500, b"")
            return json_response({"data": [REDBARK_TRANSACTIONS[0]],
                                  "pagination": {"hasMore": False}})
        return json_response({}, 404)

    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=Stub(handler))

    assert len(fetched.accounts) == 2
    assert len(fetched.errors) == 1 and "Savings" in fetched.errors[0]
    by_id = {account.id: account for account in fetched.accounts}
    assert len(by_id["acc_1"].transactions) == 1
    assert by_id["acc_2"].transactions == []


# ------------------------------------------------------------------- BankSync


BANKSYNC_ACCOUNT = {
    "id": "acc_1", "accountName": "ANZ Everyday", "accountType": "transaction",
    "currency": "AUD", "accountNumber": "12345678",
    "balance": "1234.56", "availableBalance": "1200.00",
}


def banksync_handler(call: Call):
    if call.url.endswith("/accounts"):
        return json_response({"data": [BANKSYNC_ACCOUNT]})
    if "/transactions" in call.url:
        if query(call).get("cursor") == "c2":
            return json_response({"data": [
                {"id": "t2", "date": "2026-11-02", "description": "SALARY",
                 "amount": "2500.00", "category": "Mystery"},
            ], "meta": {"hasMore": False}})
        return json_response({"data": [
            {"id": "t1", "date": "2026-11-01", "description": "COLES 1234",
             "amount": "-57.80", "merchantName": "Coles", "category": "Groceries"},
            {"id": "t3", "date": "2026-11-03", "description": "PENDING",
             "amount": "-4.50", "pending": True},
        ], "meta": {"hasMore": True, "cursor": "c2"}})
    return json_response({}, 404)


def test_banksync_maps_accounts_and_pages_by_cursor():
    stub = Stub(banksync_handler)

    fetched = feeds.fetch_banksync("bs-key", bank_id="bnk_anz", since=SINCE,
                                   today=TODAY, transport=stub)

    assert fetched.provider == "banksync"
    account = fetched.accounts[0]
    assert account.name == "ANZ Everyday"
    assert account.balance == pytest.approx(1234.56)
    assert [txn.id for txn in account.transactions] == ["t1", "t2"]
    assert fetched.skipped_pending == 1
    assert account.transactions[0].category == "Groceries"
    assert account.transactions[1].category == "Mystery"
    assert any("cursor=c2" in url for url in stub.urls)
    assert stub.calls[0].headers["X-API-Key"] == "bs-key"


def test_when_every_account_fails_the_sync_is_not_reported_as_empty():
    second = dict(REDBARK_ACCOUNT, id="acc_2", name="Savings")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT, second],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return sy.Response(500, b"")

    store = _store(feed_provider="redbark")
    result = sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                         transport=Stub(handler))

    assert result.ok is False
    assert result.imported == 0
    assert result.error is not None
    assert "Savings" in str(result.error) or "Everyday" in str(result.error)
    assert "failed" in result.describe().casefold()


def test_banksync_409_names_the_reconnect_url():
    stub = Stub(lambda call: json_response(
        {"reconnectUrl": "https://banksync.example/reconnect"}, 409))
    with pytest.raises(sy.SyncError) as caught:
        feeds.fetch_banksync("k", bank_id="bnk_anz", since=SINCE, today=TODAY,
                             transport=stub)
    assert caught.value.needs_reconnect is True
    assert "reconnect" in str(caught.value)
    assert "https://banksync.example/reconnect" in str(caught.value)


# ------------------------------------------------------------------ run_sync


def _store(**overrides):
    return Store(doc=StoreDocument(),
                 settings=Settings(enable_bank_sync=True, **overrides),
                 data_dir=None)


def test_run_sync_uses_redbark_and_applies_the_provider_category():
    store = _store(feed_provider="redbark")
    secrets = Secrets(feed_redbark="rb-key")

    result = sy.run_sync(store, secrets, today=TODAY, transport=Stub(redbark_handler))

    assert result.ok is True
    assert result.provider == "redbark"
    assert result.imported == 2
    account = store.view().account_by_external("acc_1", "redbark")
    assert account is not None
    assert account.provider == "redbark"
    assert account.balance == pytest.approx(2400.0)
    assert account.high_water == epoch(date(2026, 11, 2))

    by_external = {txn.external_id: txn for txn in store.doc.transactions}
    assert by_external["r1"].category == "Groceries"
    assert by_external["r1"].category_source == "bank"
    assert by_external["r1"].merchant == "Coles"
    assert by_external["r1"].source == "redbark:acc_1"
    assert any("pending" in warning for warning in result.warnings)


def test_run_sync_is_idempotent_for_a_feed():
    store = _store(feed_provider="redbark")
    secrets = Secrets(feed_redbark="rb-key")
    stub = Stub(redbark_handler)

    sy.run_sync(store, secrets, today=TODAY, transport=stub)
    second = sy.run_sync(store, secrets, today=TODAY, transport=stub)

    assert second.imported == 0
    assert second.summary.duplicates == 2
    assert len(store.doc.transactions) == 2


def test_the_second_sync_asks_from_the_overlap():
    store = _store(feed_provider="redbark")
    secrets = Secrets(feed_redbark="rb-key")
    stub = Stub(redbark_handler)

    sy.run_sync(store, secrets, today=TODAY, transport=stub)
    sy.run_sync(store, secrets, today=TODAY, transport=stub)

    tx_calls = [call for call in stub.calls if "/v1/transactions" in call.url]
    assert query(tx_calls[-1])["from"] == "2026-10-28"      # 2 Nov - 5 days


def test_the_provider_is_inferred_from_the_key():
    store = _store()
    secrets = Secrets(feed_banksync="bs-key")

    result = sy.run_sync(store, secrets, today=TODAY, transport=Stub(banksync_handler))

    assert result.ok is True
    assert result.provider == "banksync"
    assert result.imported == 2
    by_external = {txn.external_id: txn for txn in store.doc.transactions}
    assert by_external["t1"].category == "Groceries"
    assert by_external["t2"].category is None       # "Mystery" is not one of ours
    assert by_external["t2"].category_source == ""


def test_a_stored_provider_wins_over_which_keys_exist():
    store = _store(feed_provider="banksync")
    secrets = Secrets(feed_redbark="rb-key", feed_banksync="bs-key")

    assert sy.provider_for(store.view(), secrets) == "banksync"
    store.settings.feed_provider = ""
    assert sy.provider_for(store.view(), secrets) == "redbark"


def test_a_rule_beats_a_provider_category():
    store = _store(feed_provider="redbark")
    store.add("rules", Rule(id="r_coles", match="COLES",
                            category="Dining & Takeaway", priority=10))
    secrets = Secrets(feed_redbark="rb-key")

    sy.run_sync(store, secrets, today=TODAY, transport=Stub(redbark_handler))

    coles = next(t for t in store.doc.transactions if t.external_id == "r1")
    assert coles.category == "Dining & Takeaway"
    assert coles.category_source == "rule"


def test_pending_rows_are_stored_when_requested():
    store = _store(feed_provider="redbark", feed_include_pending=True)
    secrets = Secrets(feed_redbark="rb-key")

    result = sy.run_sync(store, secrets, today=TODAY, transport=Stub(redbark_handler))

    assert result.imported == 3
    pending = next(t for t in store.doc.transactions if t.external_id == "r3")
    assert pending.status == "pending"
    assert not any("pending" in warning for warning in result.warnings)


def test_sync_enabled_recognises_feed_credentials():
    store = _store()
    assert sy.sync_enabled(store.view(), Secrets(feed_redbark="k")) is True
    assert sy.sync_enabled(store.view(), Secrets()) is False
    store.settings.enable_bank_sync = False
    assert sy.sync_enabled(store.view(), Secrets(feed_redbark="k")) is False


def test_redbark_retries_without_a_date_range_when_rejected():
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        if "from=" in call.url:
            return sy.Response(400, b"start date is not valid")
        return json_response({"data": [REDBARK_TRANSACTIONS[0]],
                              "pagination": {"hasMore": False}})

    stub = Stub(handler)
    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=stub)

    assert [txn.id for txn in fetched.accounts[0].transactions] == ["r1"]
    tx_calls = [call for call in stub.calls if "/v1/transactions" in call.url]
    assert len(tx_calls) == 2


def test_an_unrelated_400_does_not_trigger_the_range_retry():
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return sy.Response(400, b"invalid account")

    stub = Stub(handler)
    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=stub)

    assert fetched.errors and "invalid account" in fetched.errors[0]
    tx_calls = [call for call in stub.calls if "/v1/transactions" in call.url]
    assert len(tx_calls) == 1


def test_redbark_accepts_an_object_shaped_page():
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": REDBARK_ACCOUNT})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": REDBARK_TRANSACTIONS[0]})

    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY,
                                  transport=Stub(handler))

    assert len(fetched.accounts) == 1
    assert [txn.id for txn in fetched.accounts[0].transactions] == ["r1"]


def test_truncated_redbark_pagination_is_an_error_not_partial_silence(monkeypatch):
    monkeypatch.setattr(feeds, "MAX_PAGES", 2)

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [REDBARK_TRANSACTIONS[0]],
                              "pagination": {"hasMore": True, "limit": 1}})

    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY,
                                  transport=Stub(handler))

    assert fetched.accounts[0].transactions == []
    assert any("pages" in error for error in fetched.errors)


def test_banksync_more_pages_without_a_cursor_is_an_error():
    def handler(call):
        if call.url.endswith("/accounts"):
            return json_response({"data": [BANKSYNC_ACCOUNT]})
        return json_response({"data": [], "meta": {"hasMore": True}})

    fetched = feeds.fetch_banksync("k", bank_id="bnk_anz", since=SINCE,
                                   today=TODAY, transport=Stub(handler))

    assert fetched.accounts[0].transactions == []
    assert fetched.errors and "cursor" in fetched.errors[0]


@pytest.mark.parametrize("status,reconnect,transient", [
    (401, True, False),
    (429, False, False),
    (500, False, True),
])
def test_banksync_statuses_are_named(status, reconnect, transient):
    stub = Stub(lambda call: sy.Response(status, b""))
    with pytest.raises(sy.SyncError) as caught:
        feeds.fetch_banksync("k", bank_id="bnk_anz", since=SINCE, today=TODAY,
                             transport=stub)
    assert caught.value.needs_reconnect is reconnect
    assert caught.value.is_transient is transient


def test_a_legacy_simplefin_account_is_not_hijacked_by_another_provider():
    legacy = Account(id="a_old", name="Old Everyday", external_id="shared",
                     provider="", balance=10.0)
    store = Store(doc=StoreDocument(accounts=[legacy]),
                  settings=Settings(enable_bank_sync=True, feed_provider="redbark"),
                  data_dir=None)
    account = dict(REDBARK_ACCOUNT, id="shared")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [account], "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [], "pagination": {"hasMore": False}})

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY, transport=Stub(handler))

    assert store.view().account("a_old").provider == ""
    created = store.view().account_by_external("shared", "redbark")
    assert created is not None and created.id != "a_old"
    assert store.view().account_by_external("shared", "simplefin").id == "a_old"


def test_a_feed_balance_records_when_it_was_current():
    store = _store(feed_provider="redbark")
    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(redbark_handler))

    account = store.view().account_by_external("acc_1", "redbark")
    assert account.balance_as_of == TODAY


def test_moving_the_watermark_alone_counts_as_a_change():
    store = _store(feed_provider="redbark")
    secrets = Secrets(feed_redbark="k")
    sy.run_sync(store, secrets, today=TODAY, transport=Stub(redbark_handler))
    account = store.view().account_by_external("acc_1", "redbark")
    account.high_water = None
    before = store.version

    sy.run_sync(store, secrets, today=TODAY, transport=Stub(redbark_handler))

    assert account.high_water == epoch(date(2026, 11, 2))
    assert store.version > before


def test_a_user_renamed_account_is_not_overwritten_by_the_feed():
    store = _store(feed_provider="redbark")
    store.add("accounts", Account(id="a_1", name="My Everyday", kind="transaction",
                                  external_id="acc_1", provider="redbark"))

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(redbark_handler))

    assert store.view().account("a_1").name == "My Everyday"


def test_a_seed_rule_beats_the_provider_category():
    store = _store(feed_provider="redbark")
    cat.ensure_seed_rules(store)

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(redbark_handler))

    coles = next(t for t in store.doc.transactions if t.external_id == "r1")
    assert coles.category_source == "rule"


def test_an_unknown_provider_is_named():
    store = _store()
    result = sy.run_sync(store, Secrets(), provider="mystery", today=TODAY,
                         transport=Stub(redbark_handler))
    assert result.ok is False
    assert "unknown feed provider" in str(result.error)


def test_a_failed_account_beside_a_healthy_one_is_a_partial_success():
    second = dict(REDBARK_ACCOUNT, id="acc_2", name="US", currency="USD")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT, second],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        if query(call).get("accountId") == "acc_1":
            return sy.Response(500, b"")
        return json_response({"data": [REDBARK_TRANSACTIONS[0]],
                              "pagination": {"hasMore": False}})

    store = _store(feed_provider="redbark")
    result = sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                         transport=Stub(handler))

    assert result.ok is True
    assert result.failed_accounts == 1
    assert result.skipped_accounts == ["US (USD)"]
    assert "failed" in result.describe().casefold()


def test_redbark_offset_advances_by_rows_returned_not_by_the_echoed_limit():
    offsets: list[str] = []

    def handler(call):
        if "/v1/accounts" in call.url:
            offsets.append(query(call).get("offset", ""))
            if query(call).get("offset") == "0":
                return json_response({"data": [REDBARK_ACCOUNT],
                                      "pagination": {"hasMore": True, "limit": 200}})
            second = dict(REDBARK_ACCOUNT, id="acc_2", name="Savings")
            return json_response({"data": [second], "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [], "pagination": {"hasMore": False}})

    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=Stub(handler))

    assert len(fetched.accounts) == 2
    assert offsets == ["0", "1"]


def test_banksync_accounts_paginate_by_cursor():
    second = dict(BANKSYNC_ACCOUNT, id="acc_2", accountName="Savings")

    def handler(call):
        if "/accounts" in call.url and "/transactions" not in call.url:
            if query(call).get("cursor") == "c2":
                return json_response({"data": [second], "meta": {"hasMore": False}})
            return json_response({"data": [BANKSYNC_ACCOUNT],
                                  "meta": {"hasMore": True, "cursor": "c2"}})
        return json_response({"data": []})

    fetched = feeds.fetch_banksync("k", bank_id="bnk_anz", since=SINCE,
                                   today=TODAY, transport=Stub(handler))

    assert [account.id for account in fetched.accounts] == ["acc_1", "acc_2"]


def test_malformed_pagination_is_treated_as_the_last_page():
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT], "pagination": "nonsense"})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [REDBARK_TRANSACTIONS[0]],
                              "pagination": ["also", "nonsense"]})

    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY,
                                  transport=Stub(handler))

    assert len(fetched.accounts) == 1
    assert [txn.id for txn in fetched.accounts[0].transactions] == ["r1"]


def test_banksync_string_false_is_not_pending():
    def handler(call):
        if call.url.endswith("/accounts"):
            return json_response({"data": [BANKSYNC_ACCOUNT]})
        return json_response({"data": [
            {"id": "t1", "date": "2026-11-01", "description": "COLES",
             "amount": "-57.80", "pending": "false"},
        ], "meta": {"hasMore": False}})

    fetched = feeds.fetch_banksync("k", bank_id="bnk_anz", since=SINCE,
                                   today=TODAY, transport=Stub(handler))

    assert [txn.id for txn in fetched.accounts[0].transactions] == ["t1"]
    assert fetched.skipped_pending == 0


def test_creating_an_account_without_a_balance_says_so():
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [], "pagination": {"hasMore": False}})

    store = _store(feed_provider="redbark")
    result = sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                         transport=Stub(handler))

    account = store.view().account_by_external("acc_1", "redbark")
    assert account.balance == 0.0
    assert account.balance_as_of is None
    assert any("did not report a balance" in warning for warning in result.warnings)


def test_a_partial_account_failure_is_visible_in_describe():
    second = dict(REDBARK_ACCOUNT, id="acc_2", name="Savings")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT, second],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        if "/v1/transactions" in call.url:
            if query(call).get("accountId") == "acc_2":
                return sy.Response(500, b"")
            return json_response({"data": [REDBARK_TRANSACTIONS[0]],
                                  "pagination": {"hasMore": False}})
        return json_response({}, 404)

    store = _store(feed_provider="redbark")
    result = sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                         transport=Stub(handler))

    assert result.ok is True
    assert result.failed_accounts == 1
    assert "failed" in result.describe().casefold()


def test_a_manual_account_is_adopted_by_name():
    manual = Account(id="a_manual", name="Everyday", kind="transaction",
                     currency="AUD", balance=100.0)
    store = Store(doc=StoreDocument(accounts=[manual]),
                  settings=Settings(enable_bank_sync=True, feed_provider="redbark"),
                  data_dir=None)

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(redbark_handler))

    accounts = store.view().accounts
    assert len(accounts) == 1
    assert accounts[0].id == "a_manual"
    assert accounts[0].external_id == "acc_1"
    assert accounts[0].provider == "redbark"


def test_foreign_accounts_ignores_currency_case():
    store = Store(doc=StoreDocument(accounts=[
        Account(id="a_1", name="Everyday", currency="aud")]),
        settings=Settings(currency="AUD"), data_dir=None)

    assert store.view().foreign_accounts() == []


def test_a_manual_account_in_another_currency_is_not_adopted():
    manual = Account(id="a_manual", name="Everyday", kind="transaction",
                     currency="AUD", balance=100.0)
    store = Store(doc=StoreDocument(accounts=[manual]),
                  settings=Settings(enable_bank_sync=True, feed_provider="redbark"),
                  data_dir=None)
    usd = dict(REDBARK_ACCOUNT, currency="USD")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [usd], "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [], "pagination": {"hasMore": False}})

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY, transport=Stub(handler))

    assert store.view().account("a_manual").external_id is None
    assert len(store.view().accounts) == 2
    foreign = store.view().foreign_accounts()
    assert [account.currency for account in foreign] == ["USD"]


def test_a_foreign_feed_balance_is_not_written_into_a_local_account():
    account = Account(id="a_1", name="Everyday", external_id="acc_1",
                      provider="redbark", currency="AUD", balance=100.0)
    store = Store(doc=StoreDocument(accounts=[account]),
                  settings=Settings(enable_bank_sync=True, feed_provider="redbark"),
                  data_dir=None)
    usd = dict(REDBARK_ACCOUNT, currency="USD")

    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [usd], "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": [{
                "accountId": "acc_1", "currentBalance": "500.00"}]})
        return json_response({"data": [], "pagination": {"hasMore": False}})

    result = sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                         transport=Stub(handler))

    assert store.view().account("a_1").balance == 100.0
    assert result.skipped_accounts == ["Everyday (USD)"]
    assert store.view().foreign_accounts() == []


def test_a_manual_account_with_a_different_masked_number_is_not_adopted():
    manual = Account(id="a_manual", name="Everyday", kind="transaction",
                     currency="AUD", masked="****9999")
    store = Store(doc=StoreDocument(accounts=[manual]),
                  settings=Settings(enable_bank_sync=True, feed_provider="redbark"),
                  data_dir=None)

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(redbark_handler))

    assert len(store.view().accounts) == 2
    assert store.view().account("a_manual").external_id is None


def test_a_feed_does_not_overwrite_a_balance_dated_later():
    account = Account(id="a_1", name="Everyday", external_id="acc_1",
                      provider="redbark", currency="AUD", balance=9999.0,
                      balance_as_of=date(2026, 11, 30))
    store = Store(doc=StoreDocument(accounts=[account]),
                  settings=Settings(enable_bank_sync=True, feed_provider="redbark"),
                  data_dir=None)

    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(redbark_handler))

    assert store.view().account("a_1").balance == 9999.0


def test_an_invalid_date_format_does_not_trigger_the_range_retry():
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return sy.Response(400, b"Invalid date format")

    stub = Stub(handler)
    fetched = feeds.fetch_redbark("k", since=SINCE, today=TODAY, transport=stub)

    assert fetched.errors
    tx_calls = [call for call in stub.calls if "/v1/transactions" in call.url]
    assert len(tx_calls) == 1


def test_a_noisy_feed_description_still_matches_the_raw_csv_row(tmp_path):
    def handler(call):
        if "/v1/accounts" in call.url:
            return json_response({"data": [REDBARK_ACCOUNT],
                                  "pagination": {"hasMore": False}})
        if "/v1/balances" in call.url:
            return json_response({"data": []})
        return json_response({"data": [{
            "id": "r1", "date": "2026-11-01", "amount": "57.80",
            "direction": "debit", "description": "COLES 123456",
            "category": "Groceries"}], "pagination": {"hasMore": False}})

    from finance_tool.engine import intake

    store = _store(feed_provider="redbark")
    sy.run_sync(store, Secrets(feed_redbark="k"), today=TODAY,
                transport=Stub(handler))
    stored = store.doc.transactions[0]
    assert stored.description == "COLES"
    assert stored.external_id == "r1"

    csv_path = tmp_path / "oct.csv"
    csv_path.write_text("Date,Amount,Description\n01/11/2026,-57.80,COLES 123456\n",
                        encoding="utf-8")
    outcome = intake.import_path(store, csv_path)

    assert outcome.summary.imported == 0
    assert outcome.summary.duplicates == 1


def test_account_lookup_is_scoped_by_provider():
    document = StoreDocument(accounts=[
        Account(id="a_red", name="Red", external_id="shared", provider="redbark"),
        Account(id="a_bank", name="Bank", external_id="shared", provider="banksync"),
    ])
    view = Store(doc=document, settings=Settings(), data_dir=None).view()

    assert view.account_by_external("shared", "redbark").id == "a_red"
    assert view.account_by_external("shared", "banksync").id == "a_bank"
    assert view.account_by_external("shared").id == "a_red"   # unscoped: first match
