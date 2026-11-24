"""The snapshot the assistant sees before it answers (§24)."""

from __future__ import annotations

from datetime import date, timedelta

from finance_tool.engine import analytics as an
from finance_tool.engine import categorise as cat
from finance_tool.engine import detect as det
from finance_tool.engine import periods as per
from finance_tool.engine import review
from finance_tool.store.store import StoreView


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def finance_context(view: StoreView, *, today: date) -> str:
    """Plain text, compact: the model reads this, so density beats prettiness."""
    lines = ["Accounts:"]
    if view.accounts:
        for account in view.accounts:
            currency = account.currency or view.currency
            lines.append(f"- {account.name} [{account.id}] "
                         f"({account.kind or 'account'}, {currency}): "
                         f"{_money(account.balance)}")
    else:
        lines.append("- none")

    lines.append(f"Net worth: {_money(an.net_worth(view))}")

    window_start = per.add_months(per.month_start(today), -5)
    lines.append("Last six months (actual):")
    for point in an.history(view, window_start, today, lens=per.MONTH):
        actual = point.actual
        lines.append(f"- {point.label}: in {_money(actual.income)}, "
                     f"out {_money(actual.expenses)}, net {_money(actual.net)}")

    recent = (today - timedelta(days=30), today)
    categories = an.spend_by_category(view, recent)
    if categories:
        lines.append("Spending by category, last 30 days:")
        lines.extend(f"- {label}: {_money(value)}" for label, value in categories[:12])
    merchants = an.top_merchants(view, recent, limit=10)
    if merchants:
        lines.append("Top merchants, last 30 days:")
        lines.extend(f"- {name.title()}: {_money(value)} ({count})"
                     for name, value, count in merchants)

    renewals = det.upcoming_renewals(view, today, days=30)
    if renewals:
        lines.append("Due in the next 30 days:")
        for occurrence in renewals[:12]:
            lines.append(f"- {occurrence.due.isoformat()} {occurrence.name}: "
                         f"{_money(occurrence.amount)}")

    uncategorised = len(cat.uncategorised(list(view.transactions)))
    queued = review.open_count(view)
    lines.append(f"Uncategorised transactions: {uncategorised}; open review questions: {queued}")
    lines.append(f"Categories in use: {', '.join(cat.categories_in_use(view)) or 'none'}")
    return "\n".join(lines)
