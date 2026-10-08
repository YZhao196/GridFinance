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

    def to_dict(self) -> dict[str, Any]:
        return encode(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        return decode_dataclass(cls, data)


def replace(obj: Any, **changes: Any) -> Any:
    """``dataclasses.replace`` re-exported for the engine's copy-on-derive habits."""
    return dataclasses.replace(obj, **changes)
