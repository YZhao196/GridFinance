"""What a hook is handed and what it hands back (§28).

Four rules, all of them load-bearing:

* **Read-only.** A hook reads the store; it never writes. The store's on-disk shape is a
  contract the UI does not touch.
* **Pure.** Same ``ctx``, same output. No clocks — ``today`` is injected. This is what
  makes output memo-cacheable and screenshots reproducible.
* **No runtime loading.** Hooks are supplied in code at build time. No plugins
  directory, no ``importlib``, no third-party script executed against a live ledger.
* **Absence is ``None``.** A hook with no data returns ``None``; the widget hides and
  the grid reflows. Never a zero that means "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Callable, Mapping

from finance_tool.store.store import StoreView


@dataclass(frozen=True)
class Row:
    """One line of a ledger or table a widget can render."""

    label: str
    value: float | None = None
    detail: str = ""
    sign: int | None = None
    tags: tuple[str, ...] = ()
    target: dict[str, Any] = field(default_factory=dict)
    # Enough to act on the row without re-deriving what it meant: an item id and the
    # occurrence date it came from, a transaction id, and so on.
    reference: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_sign(self) -> int:
        if self.sign is not None:
            return self.sign
        if self.value is None or self.value == 0:
            return 0
        return 1 if self.value > 0 else -1


@dataclass(frozen=True)
class WidgetData:
    """What a hook returns. Every field is optional, and every field means something."""

    value: float | None = None
    delta: float | None = None
    series: list[tuple[date, float]] | None = None
    rows: list[Row] | None = None
    sign: int | None = None
    note: str | None = None
    # Free-form extras for a widget that needs more than the common shape — a
    # secondary figure, a unit, a label override. Not a place to hide the main number.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_sign(self) -> int:
        """Which way is this pointing? Derived from the value unless set outright."""
        if self.sign is not None:
            return self.sign
        if self.value is None or self.value == 0:
            return 0
        return 1 if self.value > 0 else -1

    @property
    def has_series(self) -> bool:
        """One point is a fact, not a trend (§32) — two is the minimum to draw one."""
        return bool(self.series) and len(self.series) >= 2

    @property
    def is_empty(self) -> bool:
        """Nothing to show at all — what makes a widget hide and the grid reflow (§32).

        An explicitly empty ``rows`` list is *not* emptiness: it is a widget with a
        considered empty state to render, which §29 asks for by name (a budget report
        with no budgets) and §32 does not want papered over with a blank tile.
        """
        return (self.value is None and not self.series and self.rows is None
                and not self.note and not self.extra)

    def with_value(self, value: float | None) -> "WidgetData":
        return replace(self, value=value)

    def without_series(self) -> "WidgetData":
        return replace(self, series=None)


@dataclass(frozen=True)
class HookContext:
    """The period a widget is being asked about, and its siblings on the same canvas."""

    store: StoreView
    lens: str
    start: date
    end: date
    label: str
    today: date
    peers: Mapping[str, WidgetData] = field(default_factory=dict)

    @property
    def span(self) -> tuple[date, date]:
        return self.start, self.end

    def peer(self, widget_id: str) -> WidgetData | None:
        return self.peers.get(widget_id)


# A hook takes a context and its own config, and returns data or nothing at all.
Hook = Callable[[HookContext, dict[str, Any]], WidgetData | None]


def widget_data(
    value: float | None = None,
    *,
    delta: float | None = None,
    series: list[tuple[date, float]] | None = None,
    rows: list[Row] | None = None,
    sign: int | None = None,
    note: str | None = None,
    **extra: Any,
) -> WidgetData:
    """A small constructor, so a hook reads as data rather than as a dataclass call."""
    return WidgetData(value=value, delta=delta, series=series, rows=rows, sign=sign,
                      note=note, extra=dict(extra))


EMPTY: WidgetData | None = None
