"""The canonical fixture store (§36).

Every engine test hand-computes its expected values against *this* document, so the
fixture is deliberately small enough to hold in your head and varied enough to exercise
every branch the engine has:

* all five recurrence kinds, including a monthly-on-the-31st (month-end clamping)
* a cancelled occurrence and an overridden occurrence (per-occurrence state)
* a shared item with two people and an even split
* a category node with two children (nesting)
* a goal linked to a savings item, and an unlinked one
* income and expenses on both sides of ``TODAY``
* an account in a foreign currency that must never be summed
* two genuine same-day, same-amount, same-merchant transactions (§21)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from finance_tool.store.entities import (
    Account,
    Goal,
    Item,
    Person,
    Rule,
    Settings,
    Snapshot,
    StoreDocument,
    TrackerItem,
    Transaction,
    EXPENSE,
    INCOME,
)
from finance_tool.store.store import Store

TODAY = date(2026, 10, 7)

# The calendar is anchored so the fixture reads clearly:
#   September 2026 starts on a Tuesday; Mondays are the 7th, 14th, 21st, 28th.
SEP = date(2026, 9, 1)
OCT = date(2026, 10, 1)


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 9, 0, 0)


def build_document() -> StoreDocument:
    people = [
        Person(id="p_sam", name="Sam", email="sam@example.com"),
        Person(id="p_alex", name="Alex", phone="0400 000 000"),
    ]

    accounts = [
        Account(id="a_everyday", name="Everyday", kind="transaction",
                balance=2400.00, currency="AUD", external_id="sf_everyday"),
        Account(id="a_savings", name="Savings", kind="savings",
                balance=8000.00, currency="AUD", external_id="sf_savings"),
        # Foreign currency: flagged, never summed into net worth (§22).
        Account(id="a_usd", name="US Checking", kind="transaction",
                balance=500.00, currency="USD"),
    ]

    items = [
        # -- income ----------------------------------------------------------
        Item(id="d_salary", name="Salary", type=INCOME, amount=3200.00,
             category="Income",
             start=date(2026, 1, 15), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=15"},
             priority=0, tags=["work"], created_at=_at(date(2026, 1, 1))),
        Item(id="d_freelance", name="Freelance", type=INCOME, amount=450.00,
             category="Income",
             start=date(2026, 9, 4), recurrence={"rrule": "FREQ=WEEKLY;INTERVAL=2"},
             priority=5, tags=["work"], created_at=_at(date(2026, 1, 1))),

        # -- recurring expenses ----------------------------------------------
        Item(id="d_rent", name="Rent", type=EXPENSE, amount=520.00,
             category="Rent",
             start=date(2026, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
             due="1", priority=0, tags=["housing"],
             overrides={date(2026, 11, 1): {"amount": 545.00}},
             paid=[date(2026, 9, 1), date(2026, 10, 1)],
             created_at=_at(date(2026, 1, 1))),
        Item(id="d_net", name="Internet", type=EXPENSE, amount=79.00,
             category="Utilities",
             start=date(2026, 2, 5), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=5"},
             due="5", subscription=True, tags=["home"],
             created_at=_at(date(2026, 1, 1))),
        Item(id="d_spotify", name="Spotify", type=EXPENSE, amount=12.99,
             category="Subscriptions",
             start=date(2026, 3, 12), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=12"},
             due="12", subscription=True, tags=["fun"],
             shared={"members": ["p_sam", "p_alex"], "split": "even"},
             created_at=_at(date(2026, 1, 1))),
        # Monthly on the 31st: must clamp, never skip, never roll over (§11).
        Item(id="d_gym", name="Gym", type=EXPENSE, amount=24.00,
             category="Health",
             start=date(2026, 1, 31), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=31"},
             due="31", subscription=True, tags=["health"],
             created_at=_at(date(2026, 1, 1))),
        Item(id="d_insurance", name="Car insurance", type=EXPENSE, amount=640.00,
             category="Car",
             start=date(2026, 3, 20), recurrence={"rrule": "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"},
             due="20", tags=["car"], created_at=_at(date(2026, 1, 1))),

        # -- weekly, with an end ---------------------------------------------
        Item(id="d_coffee", name="Coffee", type=EXPENSE, amount=4.50,
             category="Food",
             start=date(2026, 9, 7), end=date(2026, 12, 31),
             recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR"},
             tags=["food"], created_at=_at(date(2026, 1, 1))),

        # -- a cancellation, and a one-off -----------------------------------
        Item(id="d_magazine", name="Magazine", type=EXPENSE, amount=15.00,
             category="Subscriptions",
             start=date(2026, 1, 20), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=20"},
             due="20", subscription=True, tags=["fun"],
             cancelled=[date(2026, 10, 20)],
             created_at=_at(date(2026, 1, 1))),
        Item(id="d_concert", name="Concert ticket", type=EXPENSE, amount=180.00,
             category="Fun",
             start=date(2026, 11, 14), recurrence=None,
             created_at=_at(date(2026, 1, 1))),

        # -- a savings transfer a goal links to ------------------------------
        Item(id="d_save", name="Savings transfer", type=EXPENSE, amount=400.00,
             category="Savings",
             start=date(2026, 1, 2), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=2"},
             due="2", tags=["saving"], created_at=_at(date(2026, 1, 1))),

        # -- nesting: a category node with two children ----------------------
        Item(id="d_utilities", name="Utilities", type=EXPENSE, amount=0.0,
             start=date(2026, 1, 1), recurrence=None, tags=["home"],
             created_at=_at(date(2026, 1, 1))),
        Item(id="d_elec", name="Electricity", type=EXPENSE, amount=145.00,
             category="Utilities",
             parent_id="d_utilities", start=date(2026, 1, 18),
             recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=18"}, due="18", tags=["home"],
             created_at=_at(date(2026, 1, 1))),
        Item(id="d_water", name="Water", type=EXPENSE, amount=60.00,
             category="Utilities",
             parent_id="d_utilities", start=date(2026, 1, 25),
             recurrence={"rrule": "FREQ=MONTHLY;INTERVAL=3"},
             due="25", tags=["home"], created_at=_at(date(2026, 1, 1))),
    ]

    rules = [
        Rule(id="r_rent", match="RENT", category="Rent", priority=10),
        Rule(id="r_net", match="INTERNET", category="Utilities", priority=10),
        Rule(id="r_salary", match="SALARY", category="Income", priority=10),
    ]

    goals = [
        Goal(id="g_japan", name="Japan trip", target=6000.00, saved=1500.00,
             linked_def_ids=["d_save"]),
        Goal(id="g_buffer", name="Buffer", target=2000.00, saved=250.00),
    ]

    tracker = [
        # 30 uses at $0.60 = $18.00, bought 36 days before TODAY.
        TrackerItem(id="k_beans", name="Coffee beans", quantity=30.0,
                    per_use_amount=0.60, purchased_on=TODAY - timedelta(days=36)),
    ]

    transactions = [
        Transaction(id="t_01", date=date(2026, 9, 1), amount=-520.00,
                    description="RENT PAYMENT", account="a_everyday", category="Rent",
                    source="csv:september.csv", external_id=None, rule_id="r_rent"),
        Transaction(id="t_02", date=date(2026, 9, 5), amount=-79.00,
                    description="INTERNET PROVIDER", account="a_everyday",
                    category="Utilities", source="csv:september.csv", rule_id="r_net"),
        Transaction(id="t_03", date=date(2026, 9, 12), amount=-12.99,
                    description="SPOTIFY", account="a_everyday", category="Subscriptions",
                    source="csv:september.csv"),
        Transaction(id="t_04", date=date(2026, 9, 15), amount=3200.00,
                    description="SALARY ACME PTY", account="a_everyday", category="Income",
                    source="csv:september.csv", rule_id="r_salary"),
        Transaction(id="t_05", date=date(2026, 9, 18), amount=-145.00,
                    description="ELECTRICITY CO", account="a_everyday", category="Utilities",
                    source="csv:september.csv"),
        Transaction(id="t_06", date=date(2026, 9, 20), amount=-15.00,
                    description="MAGAZINE SUB", account="a_everyday",
                    category="Subscriptions", source="csv:september.csv"),
        # §21: two genuine same-day, same-amount coffees. Both must survive.
        Transaction(id="t_07", date=date(2026, 9, 22), amount=-4.50,
                    description="CAFE LATTE", account="a_everyday",
                    source="csv:september.csv", fingerprint="fp_cafe_0"),
        Transaction(id="t_08", date=date(2026, 9, 22), amount=-4.50,
                    description="CAFE LATTE", account="a_everyday",
                    source="csv:september.csv", fingerprint="fp_cafe_1"),
        Transaction(id="t_09", date=date(2026, 10, 1), amount=-520.00,
                    description="RENT PAYMENT", account="a_everyday", category="Rent",
                    source="csv:october.csv", rule_id="r_rent"),
        Transaction(id="t_10", date=date(2026, 10, 5), amount=-79.00,
                    description="INTERNET PROVIDER", account="a_everyday",
                    category="Utilities", source="csv:october.csv", rule_id="r_net"),
        Transaction(id="t_11", date=date(2026, 10, 6), amount=-4.50,
                    description="CAFE LATTE", account="a_everyday",
                    source="csv:october.csv"),
    ]

    snapshots = [
        Snapshot(date=date(2026, 7, 31), value=9100.00),
        Snapshot(date=date(2026, 8, 31), value=9950.00),
        Snapshot(date=date(2026, 9, 30), value=10400.00),
    ]

    return StoreDocument(
        items=items, accounts=accounts, transactions=transactions, rules=rules,
        goals=goals, tracker=tracker, people=people, snapshots=snapshots,
    )


def build_settings() -> Settings:
    return Settings(currency="AUD", lens="month", anchor=OCT, animate=False)


def build_store(data_dir: Path | None = None) -> Store:
    """A Store holding the fixture, with a fixed clock so timestamps are deterministic."""
    store = Store(
        doc=build_document(),
        settings=build_settings(),
        data_dir=data_dir,
        clock=lambda: datetime(2026, 10, 7, 12, 0, 0),
    )
    store.version = 1
    return store


def seed_store_dir(path: Path, doc=None, settings=None) -> None:
    """Write a real store database at ``path``, the way the app would.

    Tests that used to hand-write `store.json` use this instead, so they exercise the same
    path the app does rather than a shape only tests know about.
    """
    store = Store(doc=doc if doc is not None else build_document(),
                  settings=settings if settings is not None else build_settings(),
                  data_dir=path,
                  clock=lambda: datetime(2026, 10, 7, 12, 0, 0))
    store.save()
    store.close()


def corrupt_store_dir(path: Path) -> Path:
    """Make the database unreadable — what a truncated or clobbered file looks like."""
    path.mkdir(parents=True, exist_ok=True)
    target = path / "store.db"
    target.write_bytes(b"this is not a database, whatever it says")
    return target


def item_named(store: Store, name: str) -> Item:
    for item in store.doc.items:
        if item.name == name:
            return item
    raise KeyError(name)


def view_of(store: Store):
    return store.view()
