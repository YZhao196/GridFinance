"""The derived-metric templates (§28).

A derived widget — ``x over y``, a delta, a share of total — is **not a new widget
class**. It is an ordinary catalogue widget handed a different hook. These functions
build that hook.

They are shorthand, not a closed grammar: anything outside the set is still just a Python
function, and there is deliberately no expression parser and no formula box (§5, §28).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from finance_tool.hooks.types import Hook, HookContext, WidgetData

Hookish = Hook | float | int | None


def _constant(value: float) -> Hook:
    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData:
        return WidgetData(value=float(value))

    return hook


def as_hook(source: Hookish) -> Hook:
    """A hook, or a literal treated as a constant one — so ``ratio(income, 12)`` works."""
    if source is None:
        return _constant(0.0)
    if callable(source):
        return source
    return _constant(float(source))


def _both(a: Hookish, b: Hookish, ctx: HookContext, config: dict[str, Any]
          ) -> tuple[WidgetData | None, WidgetData | None]:
    return as_hook(a)(ctx, config), as_hook(b)(ctx, config)


def _pair_series(left: list[tuple] | None,
                 right: list[tuple] | None) -> list[tuple]:
    """Point-wise pairing of two series, aligned on the dates they share.

    Aligned on dates rather than on position: two widgets built from different lenses can
    have different lengths, and zipping them positionally would silently compare October
    to August.
    """
    if not left or not right:
        return []
    right_by_date = dict(right)
    return [(when, value) for when, value in left if when in right_by_date]


def ratio(a: Hookish, b: Hookish, *, label: str | None = None) -> Hook:
    """``a`` over ``b``, guarded against a zero denominator.

    The scalar is the ratio of the *totals*, not the average of point-wise ratios: on a
    series that would let a quiet Sunday count as much as a rent day. The series, where
    both sides have one, is point-wise and skips any date whose denominator is zero.
    """
    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        left, right = _both(a, b, ctx, config)
        if left is None or right is None:
            return None

        value = None
        if left.value is not None and right.value:
            value = left.value / right.value

        series = None
        steps = dict(right.series or [])
        if left.series and steps:
            points = [(when, amount / steps[when])
                      for when, amount in _pair_series(left.series, right.series)
                      if steps.get(when)]
            series = points or None

        if value is None and series is None:
            return None
        return WidgetData(
            value=value, series=series,
            note=label or (left.note or None),
            extra={"numerator": left.value, "denominator": right.value},
        )

    return hook


def share_of(a: Hookish, b: Hookish) -> Hook:
    """``a`` as a percentage of ``b``."""
    inner = ratio(a, b)

    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        data = inner(ctx, config)
        if data is None or data.value is None:
            return None
        return replace(data, value=data.value * 100.0,
                       extra={**data.extra, "unit": "%"})

    return hook


def delta(a: Hookish, b: Hookish, *, label: str | None = None) -> Hook:
    """``a`` minus ``b``, with the sign carried into the label rather than the hue.

    A missing figure is missing on both sides: ``a - absent`` is ``None``, not ``a``,
    because a difference from an unknown baseline is not known either.
    """
    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        left, right = _both(a, b, ctx, config)
        if left is None or right is None:
            return None

        value = None
        if left.value is not None and right.value is not None:
            value = left.value - right.value

        steps = dict(right.series or {})
        series = None
        if left.series and right.series:
            points = [(when, amount - steps[when])
                      for when, amount in _pair_series(left.series, right.series)]
            series = points or None

        if value is None and series is None:
            return None
        return WidgetData(
            value=value, series=series, note=label,
            extra={"minuend": left.value, "subtrahend": right.value},
        )

    return hook


def cumulative(a: Hookish) -> Hook:
    """A running total across the period."""
    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        data = as_hook(a)(ctx, config)
        if data is None:
            return None
        if not data.series:
            return data           # nothing to accumulate; the single figure stands

        running = 0.0
        series: list[tuple] = []
        for when, value in data.series:
            running += value
            series.append((when, running))
        return replace(data, value=running, series=series, extra={**data.extra, "cumulative": True})

    return hook


def rolling_mean(a: Hookish, n: int) -> Hook:
    """An ``n``-period mean, growing in from the left rather than starting late.

    The first point is the mean of what exists so far, so a five-period mean on a
    two-point series returns two points — a shorter window, not a missing chart.
    """
    window = max(1, int(n))

    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        data = as_hook(a)(ctx, config)
        if data is None:
            return None
        if not data.series:
            return data

        series: list[tuple] = []
        for index in range(len(data.series)):
            chunk = data.series[max(0, index - window + 1): index + 1]
            series.append((data.series[index][0], sum(v for _, v in chunk) / len(chunk)))
        return replace(data, series=series,
                       extra={**data.extra, "window": window})

    return hook


def scale(a: Hookish, factor: float) -> Hook:
    """Multiply by a constant — the plainest derived metric there is."""
    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        data = as_hook(a)(ctx, config)
        if data is None:
            return None
        value = None if data.value is None else data.value * factor
        series = [(when, amount * factor) for when, amount in data.series] if data.series else None
        return replace(data, value=value, series=series)

    return hook


def combine(*hooks: Hookish, op: str = "sum", label: str | None = None) -> Hook:
    """Several hooks folded into one figure. ``sum`` or ``mean``."""
    def hook(ctx: HookContext, config: dict[str, Any]) -> WidgetData | None:
        values: list[float] = []
        for source in hooks:
            data = as_hook(source)(ctx, config)
            if data is not None and data.value is not None:
                values.append(data.value)
        if not values:
            return None
        total = sum(values) if op == "sum" else sum(values) / len(values)
        return WidgetData(value=total, note=label, extra={"parts": len(values)})

    return hook



TEMPLATES: dict[str, Callable[..., Hook]] = {
    "ratio": ratio,
    "share_of": share_of,
    "delta": delta,
    "cumulative": cumulative,
    "rolling_mean": rolling_mean,
    "scale": scale,
    "combine": combine,
}
