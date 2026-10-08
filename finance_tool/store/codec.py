"""JSON codec driven by the dataclasses' own type hints.

Adding a field to an entity is a one-line change: the codec reads the annotation and
does the right thing with ``date``/``datetime``, ``X | None``, ``list[...]`` and
``dict[...]`` (including date-keyed dicts, which the occurrence state needs).
"""

from __future__ import annotations

import dataclasses
import types
import typing
from datetime import date, datetime
from typing import Any, TypeVar, get_args, get_origin

T = TypeVar("T")

_PRIMITIVES = (str, int, float, bool)


def encode(value: Any) -> Any:
    """Dataclass tree -> JSON-safe values."""
    if value is None or isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {_key(k): encode(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    raise TypeError(f"cannot encode {type(value).__name__}")


def decode(value: Any, tp: Any) -> Any:
    """JSON value -> the Python type ``tp`` describes."""
    if value is None:
        return None
    if tp is Any or tp is None:
        return value

    origin = get_origin(tp)

    if origin is typing.Union or isinstance(tp, types.UnionType):
        for arg in get_args(tp):
            if arg is type(None):
                continue
            try:
                return decode(value, arg)
            except (TypeError, ValueError):
                continue
        return value

    if tp is bool:
        # SQLite has no boolean: a bool column comes back as 0 or 1, and an un-coerced
        # `1` renders as "1" where the sheet means "yes" — and is falsy nowhere it should
        # be truthy. `bool` is a subclass of int, but this compares identity, so an
        # `int` field is unaffected.
        return bool(value)
    if tp is date:
        return date.fromisoformat(value)
    if tp is datetime:
        return datetime.fromisoformat(value)

    if origin in (list, tuple, set):
        args = get_args(tp)
        item_tp = args[0] if args else Any
        return [decode(v, item_tp) for v in value]

    if origin is dict or origin is typing.Mapping:
        args = get_args(tp)
        key_tp, val_tp = args if len(args) == 2 else (str, Any)
        return {decode(k, key_tp): decode(v, val_tp) for k, v in value.items()}

    if dataclasses.is_dataclass(tp):
        return decode_dataclass(tp, value)

    return value


def decode_dataclass(cls: type[T], data: dict[str, Any]) -> T:
    """Build ``cls`` from ``data``, letting absent keys fall back to field defaults."""
    if not isinstance(data, dict):
        raise TypeError(f"expected an object for {cls.__name__}, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    kwargs = {f.name: decode(data[f.name], hints.get(f.name, Any))
              for f in dataclasses.fields(cls) if f.name in data}
    return cls(**kwargs)


def _key(value: Any) -> str:
    """JSON object keys are strings; dates become their ISO form."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
