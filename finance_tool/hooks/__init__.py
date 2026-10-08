"""The data hook layer (§28).

Every widget takes **one callable input** — its *data hook* — and calls it to get its
own numbers. That hook is the app's extension point, and it is why a figure the catalogue
does not ship is still a figure the app can show.

This package sits between the engine and the UI and imports no Qt, like everything below
it. See :mod:`finance_tool.hooks.types` for the contracts a hook holds itself to.
"""

from finance_tool.hooks.cache import HookCache, freeze
from finance_tool.hooks.templates import (
    Hook,
    cumulative,
    delta,
    ratio,
    rolling_mean,
    share_of,
)
from finance_tool.hooks.types import HookContext, Row, WidgetData

__all__ = [
    "HookCache",
    "freeze",
    "Hook",
    "cumulative",
    "delta",
    "ratio",
    "rolling_mean",
    "share_of",
    "HookContext",
    "Row",
    "WidgetData",
]
