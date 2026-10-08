"""Categorisation: rules, seeded defaults and suggestions (§24).

Two layers, checked in order: the user's rules, then the seeded defaults. The seeded
defaults are **ordinary rules** shipped into the store — editable and deletable like any
other — so there is no hidden layer that behaves differently from a rule the user wrote.
They simply start at a negative priority, which is the whole mechanism: any rule the user
adds (priority 0 by default) outranks all of them.

The real feature is the suggestion. Grouping uncategorised transactions by merchant key
— the same grouping detection uses — turns "categorise 54 transactions" into "accept
these nine rules", and accepting one writes an ordinary rule and re-runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from finance_tool.engine import detect as det
from finance_tool.store.entities import Rule, Transaction
from finance_tool.store.store import StoreView, new_id

SEED_PREFIX = "seed_"

# Ordered most specific first; the order becomes the seeded priority, so "cafe" can win
# over a broader "food" that also matches.
SEED_KEYWORDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("cafe", "Cafes", ()),
    ("coffee", "Cafes", ()),
    ("espresso", "Cafes", ()),
    ("bakery", "Cafes", ()),
    ("restaurant", "Dining", ()),
    ("pizza", "Dining", ()),
    ("burger", "Dining", ()),
    ("sushi", "Dining", ()),
    ("thai", "Dining", ()),
    ("takeaway", "Dining", ()),
    ("supermarket", "Groceries", ()),
    ("woolworths", "Groceries", ()),
    ("coles", "Groceries", ()),
    ("aldi", "Groceries", ()),
    ("iga", "Groceries", ()),
    ("fuel", "Transport", ()),
    ("petrol", "Transport", ()),
    ("caltex", "Transport", ()),
    ("ampol", "Transport", ()),
    ("bp", "Transport", ()),
    ("shell", "Transport", ()),
    ("uber", "Transport", ()),
    ("taxi", "Transport", ()),
    ("opal", "Transport", ()),
    ("myki", "Transport", ()),
    ("parking", "Transport", ()),
    ("pharmacy", "Health", ()),
    ("chemist", "Health", ()),
    ("gym", "Health", ()),
    ("fitness", "Health", ()),
    ("dentist", "Health", ()),
    ("doctor", "Health", ()),
    ("netflix", "Subscriptions", ("streaming",)),
    ("spotify", "Subscriptions", ("streaming",)),
    ("stan", "Subscriptions", ("streaming",)),
    ("binge", "Subscriptions", ("streaming",)),
    ("disney", "Subscriptions", ("streaming",)),
    ("electricity", "Utilities", ()),
    ("internet", "Utilities", ()),
    ("water", "Utilities", ()),
    ("gas", "Utilities", ()),
    ("telstra", "Utilities", ()),
    ("optus", "Utilities", ()),
    ("vodafone", "Utilities", ()),
    ("insurance", "Insurance", ()),
    ("rent", "Rent", ()),
    ("amazon", "Shopping", ()),
    ("kmart", "Shopping", ()),
    ("target", "Shopping", ()),
    ("big w", "Shopping", ()),
    ("bunnings", "Shopping", ()),
    ("salary", "Income", ()),
    ("payroll", "Income", ()),
    ("wages", "Income", ()),
    ("interest", "Income", ()),
    ("refund", "Income", ()),
    ("atm", "Cash", ()),
    ("withdrawal", "Cash", ()),
    ("transfer", "Transfers", ()),
]


def seed_rules() -> list[Rule]:
    """The shipped defaults, as plain rules at negative priority.

    Negative priority is the entire mechanism that makes a user rule win: the user's own
    rules sit at 0 or above, so nothing here can shadow one they wrote.
    """
    return [
        Rule(id=f"{SEED_PREFIX}{index:02d}", match=keyword, category=category,
             tags=list(tags), priority=-(index + 1))
        for index, (keyword, category, tags) in enumerate(SEED_KEYWORDS)
    ]


def is_seed(rule: Rule) -> bool:
    return rule.id.startswith(SEED_PREFIX)


def order_rules(rules: list[Rule]) -> list[Rule]:
    """Highest priority first, then stable by id so two equal rules never swap places."""
    return sorted(rules, key=lambda rule: (-rule.priority, rule.id))


# ------------------------------------------------------------------------ matching


SHORT_KEYWORD = 5


def rule_matches(rule: Rule, description: str, merchant: str | None = None) -> bool:
    """A rule matches a description as a substring, case-insensitively.

    It also matches the merchant key, so a rule written against the normalised identity
    ("NETFLIX") keeps working however the bank pads the line ("NETFLIX.COM 4482").

    A keyword shorter than five characters must stand on its own as a word: "bp" is a
    fuel brand, but as a bare substring it also matches "subprime", and a rule that
    silently re-categorises unrelated spending is the kind of quiet wrongness this app
    exists to avoid.
    """
    needle = (rule.match or "").strip().casefold()
    if not needle:
        return False
    haystack = (description or "").casefold()
    if len(needle) >= SHORT_KEYWORD:
        if needle in haystack:
            return True
    elif re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
        return True
    return merchant is not None and needle == merchant.casefold()


@dataclass(frozen=True)
class Decision:
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    rule_id: str | None = None
    matched: str | None = None

    @property
    def categorised(self) -> bool:
        return bool(self.category)


UNCLEAR = Decision()


def categorise(
    description: str,
    rules: list[Rule],
    *,
    merchant: str | None = None,
) -> Decision:
    """The first rule that matches wins — user rules first, then the seeded defaults."""
    key = merchant if merchant is not None else det.merchant_key(description)
    for rule in order_rules(rules):
        if rule_matches(rule, description, key):
            return Decision(category=rule.category or None, tags=list(rule.tags),
                            rule_id=rule.id, matched=rule.match)
    return UNCLEAR


def categorise_many(
    transactions: list[Transaction],
    rules: list[Rule],
) -> list[tuple[Transaction, Decision]]:
    return [(txn, categorise(txn.description, rules)) for txn in transactions]


def uncategorised(transactions: list[Transaction]) -> list[Transaction]:
    return [txn for txn in transactions if not txn.category]


# ------------------------------------------------------------------ suggestions


@dataclass(frozen=True)
class RuleSuggestion:
    """A proposed rule, with the evidence that justifies it."""

    merchant: str
    occurrences: int
    total: float
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    sample: str = ""
    transaction_ids: list[str] = field(default_factory=list)

    @property
    def average(self) -> float:
        return self.total / self.occurrences if self.occurrences else 0.0

    @property
    def label(self) -> str:
        """"CALTEX FUEL ×14 → Transport", or "→ ?" when nothing suggests a category."""
        return f"{self.merchant} ×{self.occurrences} → {self.category or '?'}"

    def to_rule(self) -> Rule:
        return Rule(id="", match=self.merchant, category=self.category or "",
                    tags=list(self.tags), priority=10)


def suggest_rules(
    view: StoreView,
    *,
    min_occurrences: int = 2,
    limit: int = 12,
) -> list[RuleSuggestion]:
    """Group uncategorised spend by merchant and propose a rule per group.

    Grouped by merchant key, the same grouping §17 uses for detection, so "CALTEX FUEL
    ×14" is one proposal rather than fourteen chores. The category comes from the seeded
    keyword table where it recognises the merchant, and is left blank where it does not —
    a blank proposal is still worth offering, because writing the rule is the tedious
    half of the job.
    """
    groups = det.group_by_merchant(uncategorised(list(view.transactions)))
    seeds = seed_rules()

    found: list[RuleSuggestion] = []
    for merchant, txns in groups.items():
        if len(txns) < min_occurrences:
            continue
        decision = categorise(txns[0].description, seeds, merchant=merchant)
        found.append(RuleSuggestion(
            merchant=merchant,
            occurrences=len(txns),
            total=sum(abs(t.amount) for t in txns),
            category=decision.category,
            tags=list(decision.tags),
            sample=txns[0].description,
            transaction_ids=[t.id for t in txns],
        ))
    found.sort(key=lambda s: (-s.total, s.merchant))
    return found[:limit] if limit else found


def suggestions_summary(view: StoreView, *, min_occurrences: int = 2) -> str:
    """The one-liner the uncategorised widget shows (§29)."""
    count = len(uncategorised(list(view.transactions)))
    if count == 0:
        return "Everything categorised"
    proposals = len(suggest_rules(view, min_occurrences=min_occurrences, limit=0))
    noun = "rule" if proposals == 1 else "rules"
    return f"{count} uncategorised · review {proposals} suggested {noun}"


# ------------------------------------------------------------------- re-running


def recategorise_all(store, *, rules: list[Rule] | None = None, overwrite: bool = True,
                     protect_manual: bool = True) -> int:
    """Re-run the rules over every transaction. Returns how many changed.

    ``overwrite`` defaults on because that is what "re-run categorisation" means: a rule
    the user has just edited should take effect on the rows it already claimed.

    ``protect_manual`` defaults on as well, and it is the more important of the two. A
    transaction with a category but no ``rule_id`` was categorised by hand, and a hand-edit
    outranks a rule — so re-running must not be a way to lose one. Without this, pressing
    "re-run categorisation" silently reset every hand-set category to whatever a rule
    said, or to nothing at all.
    """
    chosen = list(store.doc.rules if rules is None else rules)
    changed = 0
    for txn in store.doc.transactions:
        if txn.category and not overwrite:
            continue
        if protect_manual and txn.category and txn.rule_id is None:
            continue
        decision = categorise(txn.description, chosen)
        if (txn.category, txn.tags, txn.rule_id) != (decision.category, decision.tags,
                                                     decision.rule_id):
            txn.category = decision.category
            txn.tags = list(decision.tags)
            txn.rule_id = decision.rule_id
            changed += 1
    if changed:
        store.version += 1
    return changed


def accept_suggestion(store, suggestion: RuleSuggestion, *, category: str | None = None) -> Rule:
    """Write a suggestion as a real rule and re-run categorisation (§24)."""
    rule = Rule(
        id=new_id("rules"),
        match=suggestion.merchant,
        category=category if category is not None else (suggestion.category or ""),
        tags=list(suggestion.tags),
        priority=10,
    )
    store.doc.rules.append(rule)
    recategorise_all(store, rules=store.doc.rules, overwrite=False)
    store.version += 1
    return rule


def ensure_seed_rules(store) -> int:
    """Install the seeded defaults into a store that has none. Returns how many were added.

    Called once on first run. Afterwards they are the user's own rules to edit or delete —
    which is what stops the seeded layer being a second, invisible rules engine.
    """
    if any(is_seed(rule) for rule in store.doc.rules):
        return 0
    existing_matches = {rule.match.casefold() for rule in store.doc.rules}
    added = [rule for rule in seed_rules() if rule.match.casefold() not in existing_matches]
    store.doc.rules.extend(added)
    if added:
        store.version += 1
    return len(added)


def categories_in_use(view: StoreView) -> list[str]:
    """Every category the store mentions, for a category picker."""
    names = {txn.category for txn in view.transactions if txn.category}
    names |= {item.category for item in view.items if item.category}
    names |= {rule.category for rule in view.rules if rule.category}
    return sorted(names, key=str.casefold)


def coverage_by_category(view: StoreView, start: date | None = None,
                         end: date | None = None) -> dict[str, float]:
    """Spend per category, including the uncategorised bucket, largest first."""
    bucket: dict[str, float] = {}
    for txn in view.transactions:
        if txn.amount >= 0 or txn.date is None:
            continue
        if start is not None and txn.date < start:
            continue
        if end is not None and txn.date > end:
            continue
        key = txn.category or "Uncategorised"
        bucket[key] = bucket.get(key, 0.0) + abs(txn.amount)
    return dict(sorted(bucket.items(), key=lambda pair: -pair[1]))
