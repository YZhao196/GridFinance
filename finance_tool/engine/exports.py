"""Readable summaries the user can keep (§32).

The database is the source of truth; these files are a copy of it in a form a person can
read, diff or hand to someone else. Australian financial years run July to June.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from finance_tool import paths
from finance_tool.engine import analytics as an
from finance_tool.engine import categories as cats
from finance_tool.engine import detect as det
from finance_tool.engine import periods as per
from finance_tool.store.store import Store, StoreError, StoreView

TOP_MERCHANTS = 30


def financial_year(when: date) -> str:
    start = when.year if when.month >= 7 else when.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def fy_bounds(fy: str) -> tuple[date, date]:
    try:
        start_year = int(str(fy).split("-")[0])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"not a financial year: {fy!r}") from exc
    return date(start_year, 7, 1), date(start_year + 1, 6, 30)


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def summary_markdown(view: StoreView, fy: str) -> str:
    start, end = fy_bounds(fy)
    inside = [txn for txn in view.transactions
              if txn.date is not None and start <= txn.date <= end]

    lines = [
        f"# Financial year {fy}",
        "",
        f"Period {start.isoformat()} to {end.isoformat()}. {len(inside)} transactions.",
        "",
        "## Monthly",
        "",
        "| Month | Spent | Received | Net |",
        "|---|---:|---:|---:|",
    ]
    for point in an.history(view, start, end, lens=per.MONTH):
        actual = point.actual
        lines.append(f"| {point.label} | {_money(actual.expenses)} | "
                     f"{_money(actual.income)} | {_money(actual.net)} |")

    lines += ["", "## Spending by category", "",
              "| Category | Spent |", "|---|---:|"]
    for label, spend in an.spend_by_category(view, (start, end)):
        lines.append(f"| {label} | {_money(spend)} |")

    lines += ["", "## Top merchants", "",
              "| Merchant | Spent | Transactions |", "|---|---:|---:|"]
    for merchant, total, count in an.top_merchants(view, (start, end),
                                                   limit=TOP_MERCHANTS):
        lines.append(f"| {merchant.title()} | {_money(total)} | {count} |")

    return "\n".join(lines) + "\n"


def export_year(store: Store, fy: str, *, out_dir: Path | None = None) -> Path:
    directory = _exports_dir(store, out_dir) / f"FY{fy}"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "summary.md"
    target.write_text(summary_markdown(store.view(), fy), encoding="utf-8")
    return target


def export_all(store: Store, *, today: date, out_dir: Path | None = None) -> list[Path]:
    years = {financial_year(txn.date) for txn in store.doc.transactions if txn.date}
    if not years:
        years.add(financial_year(today))
    directory = _exports_dir(store, out_dir)
    return [export_year(store, fy, out_dir=directory) for fy in sorted(years)]


def _exports_dir(store: Store, out_dir: Path | None) -> Path:
    """Where summaries go. A scratch store has no 'beside the app' to write to."""
    if out_dir is not None:
        return Path(out_dir)
    if store.data_dir is None:
        raise StoreError(
            "this store has no directory, so it cannot export; pass out_dir")
    return store.data_dir / "exports"


PROFILE_TEMPLATE = """# About me

_The assistant reads this file before answering. Keep it short and factual._

## Work and income

-

## Household

-

## Goals

-

## Recurring commitments

-

## Preferences

-
"""


def ensure_profile(path: Path | None = None) -> Path:
    """Create the assistant's profile notes once; never overwrite what is there."""
    target = Path(path) if path is not None else paths.PROFILE_PATH
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(PROFILE_TEMPLATE, encoding="utf-8")
    return target
