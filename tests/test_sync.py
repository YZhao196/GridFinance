"""SimpleFIN sync against a stubbed HTTP layer (§35).

Network code with no tests is the largest risk in the plan, so nothing here reaches the
network: :class:`StubTransport` sits at the one seam the module defines, records every
request, and lets each test decide what the bridge says back.
"""

import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest

from finance_tool.engine import categorise as cat
from finance_tool.engine import sync as sy
from finance_tool.store.entities import Account, StoreDocument, Item, EXPENSE
from finance_tool.store.secrets import Secrets, load_secrets
from finance_tool.store.store import Store

ACCESS = "https://sam:hunter2@bridge.simplefin.org/simplefin"
CLAIM_URL = "https://bridge.simplefin.org/simplefin/create?token=abc"


# ------------------------------------------------------------------- the stub


@dataclass
class StubCall:
    method: str
    url: str
    data: bytes | None = None
    headers: dict = field(default_factory=dict)


class StubTransport:
    """A fake bridge. ``handler`` returns a :class:`Response` for each request."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[StubCall] = []

    def request(self, method, url, *, data=None, headers=None):
        call = StubCall(method=method, url=url, data=data, headers=dict(headers or {}))
        self.calls.append(call)
        return self.handler(call)

    @property
    def last(self) -> StubCall:
        return self.calls[-1]

    @property
    def paths(self) -> list[str]:
        return [call.url for call in self.calls]


def respond(status: int, body: bytes | str = b"", headers: dict | None = None):
    if isinstance(body, str):
        body = body.encode("utf-8")
    return lambda call: sy.Response(status=status, body=body, headers=headers or {})


def respond_json(payload, status: int = 200):
    return respond(status, json.dumps(payload))


def epoch(when: date) -> int:
    """The unix seconds SimpleFIN would report for a UTC date."""
    return int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp())


def txn(txn_id, when, amount, description, *, pending=False):
    return {"id": txn_id, "posted": epoch(when) if when else 0, "amount": f"{amount:.2f}",
            "description": description, "pending": pending,
            "transacted_at": epoch(when) if when else 0}


def account(account_id="acc-1", name="Everyday", currency="AUD", balance="2400.00",
            transactions=()):
    return {"id": account_id, "name": name, "currency": currency, "balance": balance,
            "transactions": list(transactions)}


def payload(*accounts):
    return {"accounts": list(accounts)}


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def store():
    store = Store(
        doc=StoreDocument(
            accounts=[Account(id="a_everyday", name="Everyday", kind="transaction",
                              balance=0.0, currency="AUD")],
        ),
        data_dir=None,
        clock=lambda: datetime(2026, 10, 7, 12, 0, 0),
    )
    store.settings.enable_bank_sync = True
    cat.ensure_seed_rules(store)
    return store


@pytest.fixture
def secrets():
    return Secrets(simplefin_access_url=ACCESS)


ONE_ACCOUNT = payload(account(transactions=[
    txn("t1", date(2026, 9, 1), -520.00, "RENT PAYMENT 8821"),
    txn("t2", date(2026, 9, 4), 3200.00, "SALARY ACME PTY"),
    txn("t3", date(2026, 9, 5), -79.00, "INTERNET PROVIDER 4471"),
    txn("t4", date(2026, 9, 22), -4.50, "CAFE LATTE 0091"),
    txn("t5", date(2026, 9, 22), -4.50, "CAFE LATTE 0091"),
]))


# ---------------------------------------------------------------------- the token


def test_the_setup_token_is_base64_of_a_claim_url():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    assert sy.decode_token(token) == CLAIM_URL


def test_whitespace_in_a_pasted_token_is_tolerated():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    wrapped = "\n".join([token[:20], token[20:]])
    assert sy.decode_token(wrapped) == CLAIM_URL


@pytest.mark.parametrize("token", ["", "!!!not base64!!!", "aGVsbG8="])
def test_a_token_that_is_not_a_url_is_refused(token):
    with pytest.raises(sy.SyncError):
        sy.decode_token(token)


def test_claim_posts_the_decoded_url_and_returns_the_access_url():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    stub = StubTransport(respond(200, ACCESS + "\n"))

    assert sy.claim(token, transport=stub) == ACCESS

    assert len(stub.calls) == 1
    call = stub.last
    assert call.method == "POST"
    assert call.url == CLAIM_URL
    assert call.data == b""
    assert call.headers["Content-Length"] == "0"


def test_a_claimed_token_is_a_clear_error_and_not_retried():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    stub = StubTransport(respond(403, "already claimed"))

    with pytest.raises(sy.TokenAlreadyClaimed) as caught:
        sy.claim(token, transport=stub)

    assert "already been claimed" in str(caught.value)
    assert caught.value.needs_reconnect is True
    assert len(stub.calls) == 1          # no retry: it can only fail the same way


def test_a_lapsed_bridge_subscription_is_named():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    stub = StubTransport(respond(402, ""))
    with pytest.raises(sy.SubscriptionLapsed):
        sy.claim(token, transport=stub)


def test_a_server_error_is_flagged_as_transient():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    stub = StubTransport(respond(503, ""))
    with pytest.raises(sy.SyncError) as caught:
        sy.claim(token, transport=stub)
    assert caught.value.is_transient is True
    assert caught.value.needs_reconnect is False


def test_a_claim_that_returns_junk_is_refused():
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    stub = StubTransport(respond(200, "not a url"))
    with pytest.raises(sy.SyncError, match="Access URL"):
        sy.claim(token, transport=stub)


def test_claiming_stores_the_credential_and_revoking_removes_it(tmp_path):
    path = tmp_path / "secrets.json"
    secrets = Secrets()
    token = base64.b64encode(CLAIM_URL.encode()).decode()
    stub = StubTransport(respond(200, ACCESS))

    sy.claim_and_store(secrets, token, transport=stub, path=path)
    assert load_secrets(path).simplefin_access_url == ACCESS

    sy.revoke(secrets, path=path)
    assert secrets.simplefin_access_url is None
    assert not path.exists()              # no residue (§13)


# ----------------------------------------------------------------- access URL auth


def test_the_access_url_is_split_into_a_clean_url_and_basic_auth():
    clean, authorization = sy.split_credentials(ACCESS)

    assert clean == "https://bridge.simplefin.org/simplefin"
    assert "sam:hunter2" not in clean
    assert authorization.startswith("Basic ")
    decoded = base64.b64decode(authorization.split()[1]).decode()
    assert decoded == "sam:hunter2"


def test_a_url_without_credentials_is_left_alone():
    clean, authorization = sy.split_credentials("https://example.org/x")
    assert clean == "https://example.org/x"
    assert authorization is None


def test_fetching_sends_the_credential_as_a_header_not_in_the_url():
    stub = StubTransport(respond_json(ONE_ACCOUNT))
    sy.fetch_accounts(ACCESS, transport=stub)

    call = stub.last
    assert "hunter2" not in call.url
    assert "sam" not in call.url
    assert call.headers["Authorization"].startswith("Basic ")


def test_fetching_passes_the_start_date_as_an_epoch():
    stub = StubTransport(respond_json(ONE_ACCOUNT))
    sy.fetch_accounts(ACCESS, since=date(2026, 9, 1), transport=stub)

    assert stub.last.url.endswith(f"/accounts?start-date={epoch(date(2026, 9, 1))}")


def test_fetching_without_a_start_date_asks_for_everything():
    stub = StubTransport(respond_json(ONE_ACCOUNT))
    sy.fetch_accounts(ACCESS, transport=stub)
    assert stub.last.url.endswith("/accounts")


# ----------------------------------------------------------------------- payloads


@pytest.mark.parametrize("value,expected", [
    (0, None),
    (None, None),
    ("", None),
    ("abc", None),
    (-5, None),
    (1_700_000_000, date(2023, 11, 14)),
])
def test_epochs_map_to_dates_and_nonsense_maps_to_nothing(value, expected):
    assert sy.epoch_to_date(value) == expected


def test_a_full_account_parses():
    result = sy.fetch_accounts(ACCESS, transport=StubTransport(respond_json(ONE_ACCOUNT)))

    assert len(result.accounts) == 1
    remote = result.accounts[0]
    assert remote.id == "acc-1"
    assert remote.name == "Everyday"
    assert remote.currency == "AUD"
    assert remote.balance == pytest.approx(2400.00)
    assert len(remote.transactions) == 5

    first = remote.transactions[0]
    assert first.posted == date(2026, 9, 1)
    assert first.amount == pytest.approx(-520.00)
    assert first.description == "RENT PAYMENT 8821"
    assert first.pending is False
    assert result.transactions == 5


def test_simplefin_amounts_keep_their_own_sign():
    result = sy.fetch_accounts(ACCESS, transport=StubTransport(respond_json(ONE_ACCOUNT)))
    amounts = [t.amount for t in result.accounts[0].transactions]
    assert amounts == [-520.00, 3200.00, -79.00, -4.50, -4.50]


def test_pending_transactions_are_left_alone_by_default():
    """A pending row's id changes when it posts, so importing it now would duplicate it."""
    data = payload(account(transactions=[
        txn("p1", date(2026, 9, 30), -10.00, "PENDING COFFEE", pending=True),
        txn("t1", date(2026, 9, 1), -520.00, "RENT PAYMENT"),
    ]))

    default = sy.fetch_accounts(ACCESS, transport=StubTransport(respond_json(data)))
    assert [t.id for t in default.accounts[0].transactions] == ["t1"]

    asked = sy.fetch_accounts(ACCESS, include_pending=True,
                              transport=StubTransport(respond_json(data)))
    assert [t.id for t in asked.accounts[0].transactions] == ["p1", "t1"]


def test_an_unreadable_amount_is_counted_not_crashed():
    data = payload(account(transactions=[
        {"id": "bad", "posted": epoch(date(2026, 9, 1)), "amount": "not money"},
        txn("t1", date(2026, 9, 1), -520.00, "RENT PAYMENT"),
    ]))
    result = sy.fetch_accounts(ACCESS, transport=StubTransport(respond_json(data)))

    assert len(result.accounts[0].transactions) == 1
    assert result.warnings == ["1 rows from the bridge could not be read"]


def test_a_response_with_no_accounts_list_is_an_error():
    stub = StubTransport(respond_json({"unexpected": True}))
    with pytest.raises(sy.SyncError, match="no accounts list"):
        sy.fetch_accounts(ACCESS, transport=stub)


def test_a_non_json_response_is_an_error():
    stub = StubTransport(respond(200, "<html>login</html>"))
    with pytest.raises(sy.SyncError, match="not JSON"):
        sy.fetch_accounts(ACCESS, transport=stub)


@pytest.mark.parametrize("status,expected", [
    (403, sy.ConnectionRevoked),
    (402, sy.SubscriptionLapsed),
    (401, sy.SyncError),
])
def test_failure_states_are_named_and_ask_for_a_reconnect(status, expected):
    stub = StubTransport(respond(status, ""))
    with pytest.raises(expected) as caught:
        sy.fetch_accounts(ACCESS, transport=stub)
    assert caught.value.needs_reconnect is True


# ------------------------------------------------------------------------- the sync


def sync_once(store, data, **kwargs):
    stub = StubTransport(respond_json(data))
    return sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7),
                   transport=stub, **kwargs), stub


def test_sync_imports_a_matching_currency_account(store):
    result, _ = sync_once(store, ONE_ACCOUNT)

    assert result.ok is True
    assert result.accounts == 1
    assert result.imported == 5
    assert result.summary.duplicates == 0
    assert len(store.doc.transactions) == 5


def test_sync_creates_the_local_account_and_remembers_its_balance(store):
    sync_once(store, ONE_ACCOUNT)

    created = store.view().account_by_external("acc-1")
    assert created is not None
    assert created.name == "Everyday"
    assert created.balance == pytest.approx(2400.00)
    assert created.currency == "AUD"
    assert created.high_water == epoch(date(2026, 9, 22))


def test_sync_is_idempotent(store):
    """Phase 3's gate: running it twice imports nothing the second time."""
    first, _ = sync_once(store, ONE_ACCOUNT)
    version_after_first = store.version
    second, _ = sync_once(store, ONE_ACCOUNT)

    assert first.imported == 5
    assert second.imported == 0
    assert second.summary.duplicates == 5
    assert len(store.doc.transactions) == 5
    assert store.version == version_after_first


def test_sync_brings_in_both_same_day_coffees(store):
    sync_once(store, ONE_ACCOUNT)
    coffees = [t for t in store.view().transactions if t.description == "CAFE LATTE 0091"]
    assert len(coffees) == 2
    assert sorted(t.external_id for t in coffees) == ["t4", "t5"]


def test_sync_categorises_on_the_way_in(store):
    sync_once(store, ONE_ACCOUNT)

    by_description = {t.description: t.category for t in store.view().transactions}
    assert by_description["RENT PAYMENT 8821"] == "Rent"
    assert by_description["SALARY ACME PTY"] == "Income"
    assert by_description["INTERNET PROVIDER 4471"] == "Utilities"
    assert by_description["CAFE LATTE 0091"] == "Cafes"


def test_sync_uses_a_stable_source_per_remote_account(store):
    sync_once(store, ONE_ACCOUNT)
    assert {t.source for t in store.view().transactions} == {"simplefin:acc-1"}


def test_the_second_sync_reaches_back_for_late_transactions(store):
    sync_once(store, ONE_ACCOUNT)
    marked = sy.high_water_date(store, "acc-1")
    assert marked == date(2026, 9, 22)

    second, stub = sync_once(store, ONE_ACCOUNT)
    expected_since = date(2026, 9, 22) - timedelta(days=sy.OVERLAP_DAYS)
    assert f"start-date={epoch(expected_since)}" in stub.last.url


def test_a_foreign_currency_account_is_flagged_and_never_imported(store):
    data = payload(
        account(account_id="acc-usd", name="US Checking", currency="USD",
                balance="500.00", transactions=[
                    txn("u1", date(2026, 9, 2), -20.00, "FOREIGN COFFEE")]),
        account(account_id="acc-1", transactions=[
            txn("t1", date(2026, 9, 1), -520.00, "RENT PAYMENT")]),
    )
    result, _ = sync_once(store, data)

    assert result.imported == 1
    assert result.skipped_accounts == ["US Checking (USD)"]
    assert any("USD" in warning for warning in result.warnings)
    # The account exists so it is visible and flagged; none of its rows came across.
    assert store.view().account_by_external("acc-usd") is not None
    assert [t.description for t in store.view().transactions] == ["RENT PAYMENT"]
    assert store.view().foreign_accounts()


def test_sync_updates_the_balance_of_a_known_account(store):
    store.view().account("a_everyday").external_id = "acc-1"
    sync_once(store, payload(account(balance="9999.99", transactions=[])))
    assert store.view().account("a_everyday").balance == pytest.approx(9999.99)


def test_sync_does_nothing_when_the_switch_is_off(store):
    store.settings.enable_bank_sync = False
    stub = StubTransport(respond_json(ONE_ACCOUNT))

    result = sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7), transport=stub)

    assert result.ok is False
    assert "turned off" in str(result.error)
    assert stub.calls == []               # the network was never touched
    assert store.doc.transactions == []


def test_sync_without_a_credential_asks_for_a_reconnect(store):
    result = sy.sync(store, access_url="", today=date(2026, 10, 7),
                     transport=StubTransport(respond_json(ONE_ACCOUNT)))

    assert result.ok is False
    assert result.needs_reconnect is True


def test_a_revoked_connection_fails_loudly_and_imports_nothing(store):
    stub = StubTransport(respond(403, ""))
    result = sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7), transport=stub)

    assert result.ok is False
    assert result.needs_reconnect is True
    assert "reconnect" in result.describe()
    assert store.doc.transactions == []


def test_a_failed_sync_cannot_be_mistaken_for_having_nothing_new(store):
    """§22's worst failure mode, asserted directly."""
    failed = sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7),
                     transport=StubTransport(respond(402, "")))
    empty = sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7),
                    transport=StubTransport(respond_json(payload(account(transactions=[])))))

    assert failed.imported == empty.imported == 0
    assert failed.ok is False and empty.ok is True
    assert failed.error is not None and empty.error is None
    assert "failed" in failed.describe().casefold()
    assert "failed" not in empty.describe().casefold()


def test_a_bridge_error_surfaces_its_status(store):
    stub = StubTransport(respond(503, ""))
    result = sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7), transport=stub)

    assert result.ok is False
    assert result.error.status == 503
    assert result.error.is_transient is True
    assert result.needs_reconnect is False


def test_no_accounts_at_all_is_a_warning_not_a_silence(store):
    result, _ = sync_once(store, payload())
    assert result.ok is True
    assert result.accounts == 0
    assert result.warnings == ["the bridge returned no accounts"]


def test_a_sync_that_finds_nothing_new_says_so_plainly(store):
    result, _ = sync_once(store, payload(account(transactions=[])))
    assert result.ok is True
    assert result.describe() == "0 read, 0 new"


def test_run_sync_reads_the_credential_from_secrets(store, secrets):
    stub = StubTransport(respond_json(ONE_ACCOUNT))
    result = sy.run_sync(store, secrets, today=date(2026, 10, 7), transport=stub)

    assert result.ok is True
    assert result.imported == 5
    assert stub.last.headers["Authorization"].startswith("Basic ")


def test_run_sync_refuses_when_the_switch_is_off(store, secrets):
    store.settings.enable_bank_sync = False
    result = sy.run_sync(store, secrets, today=date(2026, 10, 7),
                         transport=StubTransport(respond_json(ONE_ACCOUNT)))
    assert result.ok is False
    assert "turned off" in str(result.error)


def test_run_sync_refuses_without_a_stored_credential(store):
    result = sy.run_sync(store, Secrets(), today=date(2026, 10, 7),
                         transport=StubTransport(respond_json(ONE_ACCOUNT)))
    assert result.ok is False
    assert result.needs_reconnect is True


def test_sync_enabled_needs_both_the_switch_and_a_credential(store, secrets):
    assert sy.sync_enabled(store.view(), secrets) is True
    store.settings.enable_bank_sync = False
    assert sy.sync_enabled(store.view(), secrets) is False
    store.settings.enable_bank_sync = True
    assert sy.sync_enabled(store.view(), Secrets()) is False
    assert sy.sync_enabled(store.view()) is True     # the switch alone, when unasked


def test_a_sync_can_be_anchored_to_an_explicit_start(store):
    stub = StubTransport(respond_json(ONE_ACCOUNT))
    sy.sync(store, access_url=ACCESS, today=date(2026, 10, 7),
            since=date(2026, 8, 1), transport=stub)
    assert f"start-date={epoch(date(2026, 8, 1))}" in stub.last.url


def test_two_accounts_roll_into_one_summary(store):
    data = payload(
        account(account_id="acc-1", transactions=[
            txn("t1", date(2026, 9, 1), -520.00, "RENT PAYMENT")]),
        account(account_id="acc-2", name="Savings", transactions=[
            txn("t2", date(2026, 9, 2), 100.00, "transfer from everyday")]),
    )
    result, _ = sync_once(store, data)

    assert result.accounts == 2
    assert result.imported == 2
    assert result.summary.categorised == 2
    assert set(result.marks) == {"acc-1", "acc-2"}


def test_sync_never_touches_the_plan(store):
    store.add("items", Item(id="d_rent", name="Rent", type=EXPENSE, amount=520.0,
                            start=date(2026, 1, 1),
                            recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"}))
    before = store.view().item("d_rent").amount
    sync_once(store, ONE_ACCOUNT)
    assert store.view().item("d_rent").amount == before


def test_high_water_is_none_for_an_unknown_account(store):
    assert sy.high_water_date(store, "nope") is None


def test_is_simplefin_url():
    assert sy.is_simplefin_url(ACCESS) is True
    assert sy.is_simplefin_url("bridge.simplefin.org") is False
    assert sy.is_simplefin_url("") is False
    assert sy.is_simplefin_url("not a url") is False
