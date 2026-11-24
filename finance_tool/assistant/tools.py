"""The assistant's tools — read first, write behind a switch (§24)."""

from __future__ import annotations

import json
from datetime import date, datetime

from finance_tool.assistant.context import finance_context
from finance_tool.engine import analytics as an
from finance_tool.engine import categories as cats
from finance_tool.engine import categorise as cat
from finance_tool.engine import detect as det
from finance_tool.engine import exports
from finance_tool.engine import periods as per
from finance_tool.engine import review
from finance_tool.llm.base import ToolSpec
from finance_tool import query as sqlquery
from finance_tool.store.entities import Rule
from finance_tool.store.store import Store, StoreError, new_id

WRITE_TOOLS = {"set_category", "create_rule", "add_note", "append_to_profile",
               "write_summaries"}
MATCH_TYPES = {"contains", "equals", "regex", "merchant"}


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _date(value, default=None) -> date | None:
    if value in (None, ""):
        return default
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{value!r} is not an ISO date") from exc


def _category_names(view) -> list[str]:
    return sorted(set(cats.names()) | set(cat.categories_in_use(view)),
                  key=str.casefold)


def _transaction_payload(txn) -> dict:
    return {
        "id": txn.id,
        "date": txn.date.isoformat() if txn.date else None,
        "amount": txn.amount,
        "description": txn.description,
        "merchant": txn.merchant,
        "account": txn.account,
        "category": txn.category,
        "tags": list(txn.tags),
        "needs_review": txn.needs_review,
    }


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("get_finance_context",
             "Snapshot of accounts, balances, recent spending and outstanding review items.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("search_transactions",
             "Find transactions. Use this for any question about specific spending.",
             {"type": "object", "properties": {
                 "query": {"type": "string", "description": "Substring of description or merchant"},
                 "start": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive"},
                 "end": {"type": "string", "description": "ISO date YYYY-MM-DD, inclusive"},
                 "category": {"type": "string", "description": "Exact category name"},
                 "account": {"type": "string", "description": "Account id"},
                 "limit": {"type": "integer", "description": "Max rows, default 50"}},
              "additionalProperties": False}),
    ToolSpec("get_spending_summary",
             "Totals by category, merchant and month for a date range.",
             {"type": "object", "properties": {"start": {"type": "string"},
                                               "end": {"type": "string"}},
              "additionalProperties": False}),
    ToolSpec("list_uncategorised", "List transactions with no category yet.",
             {"type": "object", "properties": {"limit": {"type": "integer"}},
              "additionalProperties": False}),
    ToolSpec("list_review_items",
             "Questions waiting for the user about ambiguous transactions.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("set_category", "Assign a category to one transaction.",
             {"type": "object", "properties": {
                 "transaction_id": {"type": "string"},
                 "category": {"type": "string"}},
              "required": ["transaction_id", "category"],
              "additionalProperties": False}),
    ToolSpec("create_rule",
             "Create a rule so future matching transactions are categorised automatically.",
             {"type": "object", "properties": {
                 "pattern": {"type": "string", "description": "Text to match"},
                 "category": {"type": "string"},
                 "match_type": {"type": "string",
                                "enum": ["contains", "equals", "regex", "merchant"]}},
              "required": ["pattern", "category"], "additionalProperties": False}),
    ToolSpec("add_note", "Attach a note to a transaction.",
             {"type": "object", "properties": {
                 "transaction_id": {"type": "string"},
                 "note": {"type": "string"}},
              "required": ["transaction_id", "note"], "additionalProperties": False}),
    ToolSpec("write_summaries",
             "Write the markdown summary for each financial year that has transactions.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("read_profile", "Read the user's life/finance profile notes.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("append_to_profile",
             "Append a durable fact about the user's finances to their profile.",
             {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False}),
    ToolSpec("list_categories", "List available category names.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("list_recurring",
             "Detected subscriptions and recurring payments with cadence and typical amount.",
             {"type": "object", "properties": {}, "additionalProperties": False}),
    ToolSpec("upcoming_expenses",
             "Recurring payments due within the next N days (default 30).",
             {"type": "object", "properties": {"days": {"type": "integer"}},
              "additionalProperties": False}),
    ToolSpec("query_sql",
             "Run a read-only SQL SELECT against the ledger. Use this for anything the "
             "other tools cannot answer directly.\n\n" + sqlquery.SCHEMA_DOC,
             {"type": "object", "properties": {
                 "sql": {"type": "string", "description": "A single SELECT or WITH statement"},
                 "limit": {"type": "integer", "description": "Max rows, default 200"}},
              "required": ["sql"], "additionalProperties": False}),
]


def _profile_path(store: Store):
    return (store.data_dir / "profile.md") if store.data_dir is not None else None


def execute_tool(store: Store, name: str, args: dict | None, *,
                 today: date, now: datetime | None = None) -> str:
    args = args or {}
    if name in WRITE_TOOLS and not store.settings.ai_writes:
        return _json({"error": "The assistant is read-only. Turn on 'Allow ledger "
                               "edits' in Settings to let it categorise or annotate."})

    view = store.view()
    moment = now or datetime.now()

    try:
        if name == "get_finance_context":
            return finance_context(view, today=today)

        if name == "search_transactions":
            start = _date(args.get("start"))
            end = _date(args.get("end"))
            query = (args.get("query") or "").strip().casefold()
            category = args.get("category")
            account = args.get("account")
            limit = max(1, min(int(args.get("limit") or 50), 200))
            found = []
            for txn in view.transactions:
                if txn.date is None:
                    continue
                if start and txn.date < start:
                    continue
                if end and txn.date > end:
                    continue
                if category and txn.category != category:
                    continue
                if account and txn.account != account:
                    continue
                text = f"{txn.description} {txn.merchant or ''}".casefold()
                if query and query not in text:
                    continue
                found.append(txn)
            found.sort(key=lambda txn: (txn.date, txn.id), reverse=True)
            return _json({"total": len(found),
                          "items": [_transaction_payload(txn) for txn in found[:limit]]})

        if name == "get_spending_summary":
            end = _date(args.get("end"), today)
            start = _date(args.get("start"), per.add_months(per.month_start(end), -2))
            by_category = [{"category": label, "spend": value}
                           for label, value in an.spend_by_category(view, (start, end))]
            by_merchant = [{"merchant": merchant.title(), "spend": value, "count": count}
                           for merchant, value, count in an.top_merchants(view, (start, end),
                                                                          limit=10)]
            by_month = []
            for point in an.history(view, start, end, lens=per.MONTH):
                by_month.append({"month": point.label, "income": point.actual.income,
                                 "expenses": point.actual.expenses,
                                 "net": point.actual.net})
            return _json({"start": start.isoformat(), "end": end.isoformat(),
                          "by_category": by_category, "by_merchant": by_merchant,
                          "by_month": by_month})

        if name == "list_uncategorised":
            limit = max(1, min(int(args.get("limit") or 40), 200))
            missing = cat.uncategorised(list(view.transactions))
            return _json({"total": len(missing),
                          "items": [_transaction_payload(txn) for txn in missing[:limit]]})

        if name == "list_review_items":
            items = []
            for item in review.open_items(view):
                txn = view.transaction(item.transaction_id)
                items.append({"id": item.id, "transaction_id": item.transaction_id,
                              "question": item.question, "reason": item.reason,
                              "suggested": item.suggested_category,
                              "amount": txn.amount if txn else None,
                              "description": txn.description if txn else ""})
            return _json(items)

        if name == "list_categories":
            return _json(_category_names(view))

        if name == "set_category":
            category = str(args.get("category") or "").strip()
            if category not in _category_names(view):
                return _json({"error": f"unknown category {category!r}"})
            txn = review.resolve_transaction(store, str(args["transaction_id"]),
                                             category, source="ai", now=moment)
            if txn is None:
                return _json({"error": f"no transaction {args.get('transaction_id')!r}"})
            return _json({"ok": True, "transaction_id": txn.id, "category": category})

        if name == "create_rule":
            category = str(args.get("category") or "").strip()
            if category not in _category_names(view):
                return _json({"error": f"unknown category {category!r}"})
            pattern = str(args.get("pattern") or "").strip()
            if not pattern:
                return _json({"error": "a pattern is required"})
            match_type = str(args.get("match_type") or "contains").lower()
            if match_type not in MATCH_TYPES:
                return _json({"error": f"unknown match type {match_type!r}"})
            rule = Rule(id=new_id("rules"), match=pattern, category=category,
                        priority=10, name=f"AI: {pattern} -> {category}",
                        match_type=match_type)
            store.doc.rules.append(rule)
            store.version += 1
            changed = cat.apply_rule_to_history(store, rule, now=moment)
            return _json({"ok": True, "rule_id": rule.id,
                          "retroactively_categorised": changed})

        if name == "add_note":
            txn = view.transaction(str(args["transaction_id"]))
            if txn is None:
                return _json({"error": f"no transaction {args.get('transaction_id')!r}"})
            note = str(args.get("note") or "")
            if txn.note != note:
                txn.note = note
                store.version += 1
            return _json({"ok": True})

        if name == "write_summaries":
            written = exports.export_all(store, today=today)
            return _json({"ok": True, "files": [str(path) for path in written]})

        if name == "read_profile":
            target = _profile_path(store)
            if target is None:
                return "(no profile: this store has no data directory)"
            return target.read_text(encoding="utf-8") if target.exists() else "(empty)"

        if name == "append_to_profile":
            text = str(args.get("text") or "").strip()
            if not text:
                return _json({"error": "nothing to append"})
            target = _profile_path(store)
            if target is None:
                return _json({"error": "this store has no data directory for a profile"})
            path = exports.ensure_profile(target)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n- {text}\n")
            return _json({"ok": True})

        if name == "list_recurring":
            groups = det.recurring_payments(view)
            return _json([{
                "merchant": group.merchant.title(),
                "occurrences": group.occurrences,
                "interval_days": group.interval_days,
                "average_amount": group.average_amount,
                "monthly_estimate": group.monthly_estimate,
                "tracked": group.tracked,
            } for group in groups])

        if name == "upcoming_expenses":
            days = max(0, int(args.get("days") or 30))
            renewals = det.upcoming_renewals(view, today, days=days)
            return _json([{
                "item_id": occurrence.item_id,
                "name": occurrence.name,
                "due": occurrence.due.isoformat(),
                "date": occurrence.date.isoformat(),
                "amount": occurrence.amount,
                "subscription": occurrence.subscription,
            } for occurrence in renewals])

        if name == "query_sql":
            if store.db_path is None:
                return _json({"error": "this store has no database file to query"})
            result = sqlquery.run_query(str(args.get("sql") or ""), path=store.db_path,
                                        limit=int(args.get("limit") or 200))
            return _json(result)

    except (KeyError, TypeError, ValueError, StoreError) as exc:
        return _json({"error": str(exc)})

    return _json({"error": f"unknown tool {name!r}"})
