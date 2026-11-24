"""The domain model (§9, §10).

Two conventions meet in this file and nowhere else:

* **Items store an unsigned amount**; direction comes from ``type``.
* **Transactions store a signed amount**; negative is money out.

The single meeting point is :func:`signed`, which is the only function allowed to know
both rules.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from finance_tool.store.codec import encode, decode_dataclass

ItemType = Literal["income", "expense"]

INCOME = "income"
EXPENSE = "expense"


def signed(node: Any) -> float:
    """An entity's amount carrying its own sign convention.

    Items are stored positive with direction in ``type``; transactions are already
    signed. Every consumer that needs a signed figure comes through here.
    """
    amount = float(getattr(node, "amount", 0.0) or 0.0)
    kind = getattr(node, "type", None)
    if kind == INCOME:
        return abs(amount)
    if kind == EXPENSE:
        return -abs(amount)
    return amount


@dataclass
class Item:
    """A plan item — the *definition* half of the split in §9."""

    id: str
    parent_id: str | None = None
    name: str = ""
    type: ItemType = EXPENSE
    amount: float = 0.0
    start: date | None = None
    end: date | None = None
    recurrence: dict[str, Any] | None = None
    due: str | None = None
    priority: int = 0
    tags: list[str] = field(default_factory=list)
    note: str = ""
    # The budget line this item funds. Transactions and rules already carry a category
    # (§10.2), so the plan side needs the same vocabulary for budget-vs-actual to have
    # a join key at all; without it the comparison has nothing to group by.
    category: str = ""
    subscription: bool = False
    shared: dict[str, Any] | None = None

    # Per-occurrence state (§9) — the only occurrence data ever stored.
    overrides: dict[date, dict[str, Any]] = field(default_factory=dict)
    cancelled: list[date] = field(default_factory=list)
    paid: list[date] = field(default_factory=list)
    # §16: paid state is per person per occurrence, so "has Sam paid me back" is a
    # question about a specific bill rather than about a subscription in general.
    shares_paid: dict[date, list[str]] = field(default_factory=dict)

    ideal: float | None = None
    cancel_by: date | None = None
    expanded: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_income(self) -> bool:
        return self.type == INCOME

    @property
    def is_expense(self) -> bool:
        return self.type == EXPENSE

    def is_cancelled(self, occ: date) -> bool:
        return occ in self.cancelled

    def is_paid(self, occ: date) -> bool:
        return occ in self.paid

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        return decode_dataclass(cls, data)


@dataclass
class Account:
    id: str
    name: str = ""
    kind: str = ""
    balance: float = 0.0
    currency: str = ""
    external_id: str | None = None
    # §22: the simplefin high-water mark, so a sync pulls the delta.
    high_water: int | None = None
    # Which feed produced this account (simplefin, redbark, banksync). External ids are
    # only unique inside a provider, so matching must be scoped by it.
    provider: str = ""
    # The masked number a statement prints ("****5678"), used to place an import when
    # the statement does not name its account.
    masked: str = ""
    # The date the balance belongs to. A statement that closes earlier must not overwrite
    # a newer balance just because it was imported later.
    balance_as_of: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return decode_dataclass(cls, data)


@dataclass
class Transaction:
    """A reconciled or imported bank line. Negative = money out (§10.2)."""

    id: str
    date: date | None = None
    amount: float = 0.0
    description: str = ""
    account: str = ""
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    external_id: str | None = None
    # §21: the fallback identity for rows with no external id, and only ever compared
    # against rows from the same source. Carries a disambiguator so two genuine
    # same-day, same-amount, same-merchant lines both survive an import.
    fingerprint: str | None = None
    source: str = ""
    imported_at: datetime | None = None
    # The categorisation rule that claimed this row, if any — lets rules be re-run
    # and reverted without guessing at provenance.
    rule_id: str | None = None
    # The merchant the statement named, when a parser could tell. Rules match against it.
    merchant: str | None = None
    note: str = ""
    # Where a category came from and how sure the source was: rule, memory, llm, bank,
    # user, detected. Absence of a source means the row predates provenance.
    category_source: str = ""
    category_confidence: float | None = None
    needs_review: bool = False
    import_id: str | None = None
    status: str = "posted"
    value_date: date | None = None
    # The line's own currency, when an import or another app recorded one. Empty means
    # "the account's currency"; analytics still works in the app's home currency.
    currency: str = ""

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        return decode_dataclass(cls, data)


@dataclass
class Rule:
    id: str
    match: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    priority: int = 0
    name: str = ""
    # contains | equals | regex | merchant. The importer's original rules were all
    # "contains"; the richer kinds arrived with the AI categoriser.
    match_type: str = "contains"
    amount_min: float | None = None
    amount_max: float | None = None
    active: bool = True
    hit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return decode_dataclass(cls, data)


@dataclass
class Goal:
    id: str
    name: str = ""
    target: float = 0.0
    saved: float = 0.0
    linked_def_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        return decode_dataclass(cls, data)


@dataclass
class TrackerItem:
    id: str
    name: str = ""
    quantity: float = 0.0
    per_use_amount: float = 0.0
    purchased_on: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackerItem":
        return decode_dataclass(cls, data)


@dataclass
class Person:
    id: str
    name: str = ""
    email: str = ""
    phone: str = ""

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Person":
        return decode_dataclass(cls, data)


@dataclass
class Snapshot:
    """A net-worth reading. Whether these are automatic is an open decision (§38)."""

    date: date
    value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return decode_dataclass(cls, data)


@dataclass
class ReviewItem:
    """One transaction waiting for a category decision (§24).

    Persisted rather than derived: a review queue is a promise the app made to the user
    — "this one could not be decided" — and it must survive a restart.
    """

    id: str
    transaction_id: str = ""
    question: str = ""
    reason: str = "low_confidence"
    options: list[str] = field(default_factory=list)
    suggested_category: str = ""
    suggested_confidence: float | None = None
    status: str = "open"          # open | resolved | skipped
    answer: str = ""
    resolved_category: str = ""
    created_at: datetime | None = None
    resolved_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewItem":
        return decode_dataclass(cls, data)


@dataclass
class ImportRecord:
    """One statement file that was read, successful or not (§19–§21).

    The import summary itself is a return value; this row is what the history surface
    reads after the fact.
    """

    id: str
    filename: str = ""
    file_hash: str = ""
    source_type: str = ""
    account: str = ""
    rows_total: int = 0
    rows_new: int = 0
    rows_duplicate: int = 0
    rows_skipped: int = 0
    status: str = "ok"
    error: str = ""
    imported_at: datetime | None = None
    span_start: date | None = None
    span_end: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportRecord":
        return decode_dataclass(cls, data)


@dataclass
class ChatMessage:
    """One turn in the assistant transcript, including the tools it called."""

    id: str
    role: str = ""                # user | assistant | tool
    content: str = ""             # raw markdown; the chat surface renders it
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # The provider's own content blocks, when it has them. Anthropic needs its thinking
    # blocks replayed verbatim on a later turn, so the transcript keeps them.
    raw: list[dict[str, Any]] = field(default_factory=list)
    # The model's reasoning, when the provider exposes it. Stored apart from the answer so
    # the UI can show or hide it without altering the reply.
    thinking: str = ""
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatMessage":
        return decode_dataclass(cls, data)


@dataclass
class PlacedWidget:
    """One tile's position and configuration on a canvas (§26)."""

    widget: str
    x: int = 0
    y: int = 0
    w: int = 4
    h: int = 2
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlacedWidget":
        return decode_dataclass(cls, data)


@dataclass
class Canvas:
    id: str
    name: str = ""
    widgets: list[PlacedWidget] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Canvas":
        return decode_dataclass(cls, data)


@dataclass
class StoreDocument:
    """The whole domain, serialised as ``store.json`` (§8)."""

    schema_version: int = 1
    items: list[Item] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    tracker: list[TrackerItem] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    messages: list[ChatMessage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StoreDocument":
        return decode_dataclass(cls, data)


@dataclass
class Settings:
    """Preferences: currency, lens, canvases, flags (§8, §26)."""

    schema_version: int = 1
    currency: str = "AUD"
    lens: str = "month"
    anchor: date | None = None
    canvases: list[Canvas] = field(default_factory=list)
    canvas_active: str | None = None
    enable_bank_sync: bool = False
    animate: bool = True
    # Colour is a real state, but a user may still want it quieter; kept honest by
    # still pairing every colour with a sign or a word (§33).
    currency_locked: bool = False
    # Assistant and feed preferences. Credentials never live here; they are in
    # secrets.json, and these fields only name which provider a credential belongs to.
    ai_provider: str = ""
    ai_model: str = ""
    ai_effort: str = ""
    ai_base_url: str = ""
    ai_writes: bool = True
    review_threshold: float = 0.6
    feed_provider: str = ""
    feed_include_pending: bool = False
    feed_banksync_bank_id: str = "bnk_anz"
    inbox_poll_minutes: int = 15
    feed_poll_minutes: int = 60
    onboarded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        return decode_dataclass(cls, data)


def replace(obj: Any, **changes: Any) -> Any:
    """``dataclasses.replace`` re-exported for the engine's copy-on-derive habits."""
    return dataclasses.replace(obj, **changes)
