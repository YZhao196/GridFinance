"""Hook memoisation on an exact key (§28).

Because hooks are pure, a result is safe to keep — and because the store carries a
monotonically increasing ``version``, invalidation is **exact rather than time-based**.
There is no TTL here and there should never be one: a cached figure is wrong only if the
key is wrong, and the key includes the store version.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Hashable

from finance_tool.hooks.types import Hook, HookContext, WidgetData

DEFAULT_MAXSIZE = 512


def freeze(value: Any) -> Hashable:
    """A dict or list as something hashable, recursively. Order-insensitive for dicts."""
    if isinstance(value, dict):
        return tuple(sorted((str(key), freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(freeze(item) for item in value))
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def peers_key(ctx: HookContext) -> Hashable:
    """A stable fingerprint of the sibling data a hook may read.

    §28 states the key as ``(hook_id, config, lens, anchor, store.version)``, and that is
    right for a hook that only reads the store. A hook that also reads its peers — the
    donut selecting a month for the trend beside it — depends on them too, so they have
    to be in the key or the second of two identical recomputes returns the first one's
    answer. ``repr`` of a frozen dataclass is deterministic over its fields, which is
    enough to tell one peer state from another.
    """
    if not ctx.peers:
        return ()
    return tuple(sorted((widget_id, repr(data)) for widget_id, data in ctx.peers.items()))


def cache_key(
    hook_id: str,
    config: dict[str, Any] | None,
    ctx: HookContext,
) -> tuple:
    """Everything a hook's output depends on, and nothing else."""
    return (
        hook_id,
        freeze(config or {}),
        ctx.lens,
        ctx.start,
        ctx.end,
        ctx.label,
        ctx.today,
        ctx.store.version,
        ctx.store.currency,
        peers_key(ctx),
    )


class HookCache:
    """A bounded memo table. Insertion-ordered, so the oldest key is the one evicted."""

    def __init__(self, maxsize: int = DEFAULT_MAXSIZE):
        self.maxsize = max(1, maxsize)
        self._entries: dict[tuple, WidgetData | None] = {}
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = self.misses = 0

    def get(self, key: tuple) -> tuple[bool, WidgetData | None]:
        if key in self._entries:
            self.hits += 1
            return True, self._entries[key]
        self.misses += 1
        return False, None

    def put(self, key: tuple, value: WidgetData | None) -> None:
        if key in self._entries:
            self._entries[key] = value
            return
        if len(self._entries) >= self.maxsize:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = value

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return None if total == 0 else self.hits / total

    def call(
        self,
        hook_id: str,
        hook: Hook,
        ctx: HookContext,
        config: dict[str, Any] | None = None,
    ) -> WidgetData | None:
        """The hook's data, from the cache when the key matches and computed otherwise."""
        key = cache_key(hook_id, config, ctx)
        found, value = self.get(key)
        if found:
            return value
        value = hook(ctx, dict(config or {}))
        self.put(key, value)
        return value

    def invalidate_store(self) -> None:
        """Drop everything. A store write bumps the version, so the keys are already
        stale; this is only for when a caller wants the memory back."""
        self.clear()


def peers_of(canvas_widgets, data_by_id: dict[str, WidgetData]) -> dict[str, WidgetData]:
    """The sibling data a widget may read (§28's ``peers``), excluding itself."""
    return {widget_id: data for widget_id, data in data_by_id.items() if data is not None}


def with_peers(ctx: HookContext, peers: dict[str, WidgetData]) -> HookContext:
    return replace(ctx, peers=peers)
