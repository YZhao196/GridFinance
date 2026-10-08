"""A small in-memory dataset for ``--demo`` and ``--shot`` (§34).

Not a feature and never written to disk unless you ask it to be: a screenshot harness
with no data photographs an empty grid, and an empty grid is not a visual regression.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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


def demo_document(today: date = TODAY) -> StoreDocument:
    month_start = date(today.year, today.month, 1)
    items = [
        Item(id="d_salary", name="Salary", type=INCOME, amount=3200.00,
             start=date(today.year - 1, 1, 15), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=15"},
             category="Income", tags=["work"]),
        Item(id="d_side", name="Freelance", type=INCOME, amount=450.00,
             start=month_start - timedelta(days=30),
             recurrence={"rrule": "FREQ=WEEKLY;INTERVAL=2"},
             category="Income", tags=["work"]),
        Item(id="d_rent", name="Rent", type=EXPENSE, amount=520.00,
             start=date(today.year - 1, 1, 1), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
             due="1", category="Rent", tags=["housing"],
             paid=[month_start], overrides={},
             note="Rises in November"),
        Item(id="d_net", name="Internet", type=EXPENSE, amount=79.00,
             start=date(today.year - 1, 2, 5), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=5"},
             due="5", category="Utilities", subscription=True, tags=["home"]),
        Item(id="d_power", name="Electricity", type=EXPENSE, amount=145.00,
             start=date(today.year - 1, 1, 18), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=18"},
             due="18", category="Utilities", tags=["home"]),
        Item(id="d_spotify", name="Spotify", type=EXPENSE, amount=12.99,
             start=date(today.year - 1, 3, 12), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=12"},
             due="12", category="Subscriptions", subscription=True,
             shared={"members": ["p_sam", "p_alex"], "split": "even"}),
        Item(id="d_gym", name="Gym", type=EXPENSE, amount=24.00,
             start=date(today.year - 1, 1, 31), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=31"},
             due="31", category="Health", subscription=True),
        Item(id="d_coffee", name="Coffee", type=EXPENSE, amount=4.50,
             start=month_start - timedelta(days=40),
             end=date(today.year, 12, 31),
             recurrence={"rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR"}, category="Cafes"),
        Item(id="d_save", name="Savings transfer", type=EXPENSE, amount=400.00,
             start=date(today.year - 1, 1, 2), recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=2"},
             due="2", category="Savings", tags=["saving"],
             paid=[month_start]),
        Item(id="d_fuel", name="Fuel", type=EXPENSE, amount=72.00,
             start=month_start - timedelta(days=60),
             recurrence={"rrule": "FREQ=WEEKLY;INTERVAL=2"},
             category="Transport"),
    ]

    accounts = [
        Account(id="a_everyday", name="Everyday", kind="transaction", balance=2418.60,
                currency="AUD", external_id="demo-everyday"),
        Account(id="a_savings", name="Savings", kind="savings", balance=8412.00,
                currency="AUD", external_id="demo-savings"),
    ]

    rules = [
        Rule(id="r_rent", match="RENT", category="Rent", priority=10),
        Rule(id="r_net", match="INTERNET", category="Utilities", priority=10),
        Rule(id="r_salary", match="SALARY", category="Income", priority=10),
    ]

    people = [Person(id="p_sam", name="Sam"), Person(id="p_alex", name="Alex")]

    transactions = [
        Transaction(id="t_1", date=month_start, amount=-520.00,
                    description="RENT PAYMENT 8821", account="a_everyday",
                    category="Rent", source="demo", rule_id="r_rent"),
        Transaction(id="t_2", date=month_start + timedelta(days=2), amount=-400.00,
                    description="TRANSFER TO SAVINGS", account="a_everyday",
                    category="Savings", source="demo"),
        Transaction(id="t_3", date=month_start + timedelta(days=4), amount=-79.00,
                    description="INTERNET PROVIDER 4471", account="a_everyday",
                    category="Utilities", source="demo", rule_id="r_net"),
        Transaction(id="t_4", date=month_start + timedelta(days=1), amount=-6.40,
                    description="CAFE LATTE 0091", account="a_everyday", source="demo"),
        Transaction(id="t_5", date=month_start + timedelta(days=3), amount=-4.90,
                    description="CAFE LATTE 0091", account="a_everyday", source="demo"),
        Transaction(id="t_6", date=month_start + timedelta(days=5), amount=-112.80,
                    description="WOOLWORTHS 1234", account="a_everyday",
                    category="Groceries", source="demo"),
    ]

    for index in range(6):
        transactions.append(Transaction(
            id=f"t_hist{index}",
            date=date(today.year, today.month, 1) - timedelta(days=30 * (index + 1)),
            amount=3200.00,
            description="SALARY ACME PTY", account="a_everyday", category="Income",
            source="demo", rule_id="r_salary"))

    snapshots = [
        Snapshot(date=date(today.year, today.month - offset, 28)
                 if today.month - offset > 0
                 else date(today.year - 1, 12 + today.month - offset, 28),
                 value=9100.00 + offset * 320.0)
        for offset in range(5, 0, -1)
    ]

    return StoreDocument(
        items=items, accounts=accounts, transactions=transactions, rules=rules,
        goals=[Goal(id="g_japan", name="Japan trip", target=6000.00, saved=1500.00,
                    linked_def_ids=["d_save"]),
               Goal(id="g_buffer", name="Buffer", target=2000.00, saved=250.00)],
        tracker=[TrackerItem(id="k_beans", name="Coffee beans", quantity=30.0,
                             per_use_amount=0.60,
                             purchased_on=today - timedelta(days=36))],
        people=people, snapshots=snapshots,
    )


def demo_store(today: date = TODAY, data_dir=None) -> Store:
    """A store with the demo data.

    ``data_dir`` is taken here rather than assigned afterwards: the database is opened when
    the store is constructed, so pointing it at a directory later would leave it writing to
    memory while `save()` appeared to succeed.
    """
    return Store(
        doc=demo_document(today),
        settings=Settings(currency="AUD", lens="month", anchor=date(today.year, today.month, 1),
                          animate=False),
        data_dir=data_dir,
        clock=lambda: datetime.combine(today, datetime.min.time()),
    )
