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
from datetime import date, datetime

from finance_tool.engine import categories as cats
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


def _normalise(text: str | None) -> str:
    return " ".join(str(text or "").split()).casefold()


def rule_matches(rule: Rule, description: str, merchant: str | None = None, *,
                 amount: float | None = None) -> bool:
    """Whether a rule claims a row.

    ``contains`` keeps the old behaviour — a substring, with keywords shorter than five
    characters required to stand on their own as words. ``equals`` compares the whole
    normalised text, ``regex`` searches it, and ``merchant`` compares only the merchant
    key so a rule does not accidentally match a reference number or a memo.

    Amount bounds compare magnitude: "$5–$200" means the size of the movement, not its
    sign. An invalid regex matches nothing rather than raising.
    """
    if not rule.active:
        return False
    if amount is not None:
        magnitude = abs(float(amount))
        if rule.amount_min is not None and magnitude < rule.amount_min:
            return False
        if rule.amount_max is not None and magnitude > rule.amount_max:
            return False

    pattern = (rule.match or "").strip()
    needle = pattern.casefold()
    if not needle:
        return False
    key = det.merchant_key(merchant if merchant is not None else description).casefold()
    kind = (rule.match_type or "contains").casefold()

    if kind == "merchant":
        return needle in key
    if kind == "regex":
        haystack = f"{description or ''} {merchant or ''}"
        try:
            return re.search(pattern, haystack, re.IGNORECASE) is not None
        except re.error:
            return False
    if kind == "equals":
        return _normalise(description) == _normalise(pattern) or key == needle

    haystack = (description or "").casefold()
    if len(needle) >= SHORT_KEYWORD:
        if needle in haystack:
            return True
    elif re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack):
        return True
    return needle == key


@dataclass(frozen=True)
class Decision:
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    rule_id: str | None = None
    matched: str | None = None
    source: str = ""
    confidence: float | None = None

    @property
    def categorised(self) -> bool:
        return bool(self.category)


UNCLEAR = Decision()


def categorise(
    description: str,
    rules: list[Rule],
    *,
    merchant: str | None = None,
    amount: float | None = None,
    count_hit: bool = False,
) -> Decision:
    """The first matching active rule wins — user rules first, then seeded defaults.

    ``count_hit`` is off by default because asking "would a rule match this?" is not the
    same as applying it; import and the classifier turn it on for rows they actually file.
    """
    carried_tags: list[str] = []
    for rule in order_rules(rules):
        if not rule_matches(rule, description, merchant, amount=amount):
            continue
        if rule.category:
            tags = list(dict.fromkeys(carried_tags + list(rule.tags)))
            if count_hit:
                rule.hit_count += 1
            return Decision(category=rule.category, tags=tags,
                            rule_id=rule.id, matched=rule.match, source="rule",
                            confidence=1.0)
        if rule.tags:
            # A rule with tags but no category is a note, not a decision: collect it and
            # keep looking. Letting it stop the search would shadow every rule below.
            carried_tags.extend(rule.tags)
            if count_hit:
                rule.hit_count += 1
    if carried_tags:
        return Decision(tags=list(dict.fromkeys(carried_tags)), source="rule")
    return UNCLEAR


def categorise_many(
    transactions: list[Transaction],
    rules: list[Rule],
) -> list[tuple[Transaction, Decision]]:
    return [(txn, categorise(txn.description, rules, merchant=txn.merchant,
                             amount=txn.amount)) for txn in transactions]


# --------------------------------------------------------------------- memory

MEMORY_MIN_OCCURRENCES = 2
MEMORY_AGREEMENT = 0.60
MEMORY_CONFIDENCE = 0.9


@dataclass(frozen=True)
class MemoryDecision:
    category: str
    confidence: float
    occurrences: int
    agreement: float


def memory_category(view: StoreView, description: str, merchant: str | None = None, *,
                    exclude_id: str | None = None) -> MemoryDecision | None:
    """What this merchant was called before, when the history is decisive.

    Two prior rows and 60% agreement is the bar. Below it the answer is "ask", not "guess":
    a merchant split across two categories is exactly the case a person should settle.
    """
    key = det.merchant_key(merchant if merchant is not None else description).casefold()
    if not key:
        return None
    counts: dict[str, int] = {}
    total = 0
    for txn in view.transactions:
        if txn.id == exclude_id or not txn.category or txn.date is None:
            continue
        # A provisional model guess is not evidence, a transfer is a movement of the
        # user's own money rather than a merchant's identity, and "Uncategorised" is
        # the absence of an answer, not one.
        if (txn.needs_review or cats.is_transfer(txn.category)
                or txn.category == cats.UNCATEGORISED):
            continue
        txn_key = det.merchant_key(
            txn.merchant if txn.merchant is not None else txn.description).casefold()
        if txn_key != key:
            continue
        counts[txn.category] = counts.get(txn.category, 0) + 1
        total += 1
    if not counts or total == 0:
        return None
    top, count = max(counts.items(), key=lambda pair: (pair[1], pair[0]))
    agreement = count / total
    if count < MEMORY_MIN_OCCURRENCES or agreement < MEMORY_AGREEMENT:
        return None
    return MemoryDecision(category=top, confidence=MEMORY_CONFIDENCE,
                          occurrences=count, agreement=agreement)


def _close_reviews(store, transaction_id: str, category: str | None,
                   now: datetime | None = None) -> None:
    """Settle any open question about a row a rule or a re-run just decided.

    Kept here as a direct walk of the review list rather than an import of
    :mod:`finance_tool.engine.review`, which imports this module: the alternative is a
    cycle for one loop.
    """
    moment = now or datetime.now()
    changed = 0
    for item in store.doc.reviews:
        if item.transaction_id != transaction_id or item.status != "open":
            continue
        item.status = "resolved"
        item.resolved_category = category or ""
        item.resolved_at = moment
        changed += 1
    if changed:
        store.version += 1


def apply_rule_to_history(store, rule: Rule, *, now: datetime | None = None) -> int:
    """Retro-apply one rule to rows no rule or person has already claimed."""
    if not rule.category:
        # A rule with no category is a grouping note, not a decision.
        return 0
    changed = 0
    auto_sources = {"llm", "memory", "bank"}
    for txn in store.doc.transactions:
        # A category with no source is a hand-set category (legacy or edited) and is as
        # protected here as it is in recategorise_all.
        if txn.category and txn.category_source not in auto_sources:
            continue
        if not rule_matches(rule, txn.description, txn.merchant, amount=txn.amount):
            continue
        txn.category = rule.category or None
        txn.tags = list(rule.tags)
        txn.rule_id = rule.id
        txn.category_source = "rule"
        txn.category_confidence = 1.0
        txn.needs_review = False
        _close_reviews(store, txn.id, rule.category, now)
        rule.hit_count += 1
        changed += 1
    if changed:
        store.version += 1
    return changed


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
    # A regular merchant the ledger does not otherwise recognise is a subscription even
    # when no keyword in the seed table names it. The proposal is still only a proposal.
    recurring = {charge.merchant
                 for charge in det.detect_subscriptions(view, min_occurrences=3)}

    found: list[RuleSuggestion] = []
    for merchant, txns in groups.items():
        if len(txns) < min_occurrences:
            continue
        decision = categorise(txns[0].description, seeds, merchant=merchant)
        category = decision.category
        if category is None and merchant in recurring:
            category = "Subscriptions"
        found.append(RuleSuggestion(
            merchant=merchant,
            occurrences=len(txns),
            total=sum(abs(t.amount) for t in txns),
            category=category,
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
                     protect_manual: bool = True, now: datetime | None = None) -> int:
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
        if (protect_manual and txn.category and txn.rule_id is None
                and txn.category_source not in {"llm", "memory", "bank"}):
            # A person's category is protected. An automatic one — the model, memory,
            # a bank hint — is exactly what re-running the rules is for.
            continue
        decision = categorise(txn.description, chosen, merchant=txn.merchant,
                              amount=txn.amount)
        if decision.category is None and decision.source == "rule":
            # A tag-only match annotates; it does not answer the category question, so
            # it must not clear one a category rule already gave.
            if txn.tags != decision.tags:
                txn.tags = list(decision.tags)
                changed += 1
            continue
        if (txn.category, txn.tags, txn.rule_id) != (decision.category, decision.tags,
                                                     decision.rule_id):
            txn.category = decision.category
            txn.tags = list(decision.tags)
            txn.rule_id = decision.rule_id
            txn.category_source = "rule" if decision.rule_id else ""
            txn.category_confidence = 1.0 if decision.rule_id else None
            if decision.rule_id:
                for rule in chosen:
                    if rule.id == decision.rule_id:
                        rule.hit_count += 1
                        break
            if decision.category:
                _close_reviews(store, txn.id, decision.category, now)
                txn.needs_review = False
            changed += 1
    if changed:
        store.version += 1
    return changed


def accept_suggestion(store, suggestion: RuleSuggestion, *, category: str | None = None) -> Rule:
    """Write a suggestion as a real rule and re-run categorisation (§24)."""
    chosen = category if category is not None else (suggestion.category or "")
    if not str(chosen).strip():
        raise ValueError("choose a category before accepting this suggestion")
    rule = Rule(
        id=new_id("rules"),
        match=suggestion.merchant,
        category=str(chosen),
        tags=list(suggestion.tags),
        priority=10,
        name=suggestion.merchant,
        match_type="contains",
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
        if cats.is_transfer(txn.category):
            continue
        if start is not None and txn.date < start:
            continue
        if end is not None and txn.date > end:
            continue
        key = txn.category or "Uncategorised"
        bucket[key] = bucket.get(key, 0.0) + abs(txn.amount)
    return dict(sorted(bucket.items(), key=lambda pair: -pair[1]))
