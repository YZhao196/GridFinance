"""The category vocabulary and what each category *means* (§24).

Categories stay strings on items, transactions and rules — that model predates this module
and the 35 hooks are written against it. What was missing is the one thing a string cannot
carry by itself: whether a category is income, spending, or a movement of the user's own
money. A transfer counted as spending inflates every total, and a credit-card payment
counted as spending counts the same money twice.

The taxonomy is also the list the assistant and the categoriser offer, so a new category
name has one home rather than five.
"""

from __future__ import annotations

from typing import Iterable

INCOME = "income"
EXPENSE = "expense"
TRANSFER = "transfer"

TRANSFERS = "Transfers"
CREDIT_CARD_PAYMENT = "Credit Card Payment"
UNCATEGORISED = "Uncategorised"

# (name, kind, parent). Parent is None for a top-level category. Order is the order the
# categoriser offers them, so it is deliberate: income first, then living costs, then the
# buckets that are not spending.
CATALOGUE: tuple[tuple[str, str, str | None], ...] = (
    ("Income", INCOME, None),
    ("Salary", INCOME, "Income"),
    ("Interest", INCOME, "Income"),
    ("Refunds & Reimbursements", INCOME, "Income"),
    ("Other Income", INCOME, "Income"),
    ("Housing", EXPENSE, None),
    ("Rent & Mortgage", EXPENSE, "Housing"),
    ("Utilities", EXPENSE, "Housing"),
    ("Internet & Phone", EXPENSE, "Housing"),
    ("Home & Maintenance", EXPENSE, "Housing"),
    ("Groceries", EXPENSE, None),
    ("Dining & Takeaway", EXPENSE, None),
    ("Transport", EXPENSE, None),
    ("Public Transport", EXPENSE, "Transport"),
    ("Fuel", EXPENSE, "Transport"),
    ("Parking & Tolls", EXPENSE, "Transport"),
    ("Rideshare & Taxi", EXPENSE, "Transport"),
    ("Shopping", EXPENSE, None),
    ("Clothing", EXPENSE, "Shopping"),
    ("Electronics", EXPENSE, "Shopping"),
    ("Health & Fitness", EXPENSE, None),
    ("Pharmacy", EXPENSE, "Health & Fitness"),
    ("Medical", EXPENSE, "Health & Fitness"),
    ("Gym & Sport", EXPENSE, "Health & Fitness"),
    ("Insurance", EXPENSE, None),
    ("Subscriptions", EXPENSE, None),
    ("Entertainment", EXPENSE, None),
    ("Travel", EXPENSE, None),
    ("Education", EXPENSE, None),
    ("Fees & Charges", EXPENSE, None),
    ("Charity & Gifts", EXPENSE, None),
    (TRANSFERS, TRANSFER, None),
    (CREDIT_CARD_PAYMENT, TRANSFER, None),
    ("Cash Withdrawal", EXPENSE, None),
    (UNCATEGORISED, EXPENSE, None),
)

KINDS: dict[str, str] = {name: kind for name, kind, _parent in CATALOGUE}
PARENTS: dict[str, str | None] = {name: parent for name, _kind, parent in CATALOGUE}

# A user may type any category. An unknown one is treated as spending, because that is
# the safe direction: counting it keeps a total honest, while silently dropping it hides
# money the user actually spent.
DEFAULT_KIND = EXPENSE


def kind_of(category: str | None) -> str:
    if not category:
        return DEFAULT_KIND
    return KINDS.get(category, DEFAULT_KIND)


def parent_of(category: str | None) -> str | None:
    return PARENTS.get(category or "")


def children_of(category: str) -> tuple[str, ...]:
    return tuple(name for name, _kind, parent in CATALOGUE if parent == category)


def is_transfer(category: str | None) -> bool:
    return kind_of(category) == TRANSFER


def is_income(category: str | None) -> bool:
    return kind_of(category) == INCOME


def names(kind: str | None = None) -> tuple[str, ...]:
    if kind is None:
        return tuple(name for name, _kind, _parent in CATALOGUE)
    return tuple(name for name, kind_, _parent in CATALOGUE if kind_ == kind)


def top_level() -> tuple[str, ...]:
    return tuple(name for name, _kind, parent in CATALOGUE if parent is None)


def parent_map() -> dict[str, str | None]:
    """A copy, so a caller building a tree cannot mutate the catalogue."""
    return dict(PARENTS)


def unique(names: Iterable[str]) -> list[str]:
    """Preserve order, drop duplicates, ignore blanks."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out
