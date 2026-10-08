from datetime import date, datetime

import pytest

from finance_tool.store.codec import decode, decode_dataclass, encode
from finance_tool.store.entities import (
    Account,
    Canvas,
    Item,
    PlacedWidget,
    StoreDocument,
    Transaction,
    EXPENSE,
    INCOME,
)


def test_item_round_trips_through_json():
    item = Item(
        id="d_rent",
        name="Rent",
        type=EXPENSE,
        amount=520.0,
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        recurrence={"rrule": "FREQ=MONTHLY;BYMONTHDAY=1"},
        overrides={date(2026, 11, 1): {"amount": 545.0}},
        cancelled=[date(2026, 10, 20)],
        paid=[date(2026, 9, 1)],
        shares_paid={date(2026, 9, 1): ["p_sam"]},
        created_at=datetime(2026, 1, 1, 9, 0, 0),
    )

    restored = Item.from_dict(encode(item))

    assert restored == item
    assert isinstance(restored.start, date)
    assert isinstance(restored.overrides, dict)
    assert list(restored.overrides) == [date(2026, 11, 1)]
    assert restored.overrides[date(2026, 11, 1)] == {"amount": 545.0}
    assert restored.cancelled == [date(2026, 10, 20)]
    assert isinstance(restored.created_at, datetime)


def test_none_stays_none_for_optional_dates():
    item = Item(id="d_one", name="One-off", start=date(2026, 11, 14), recurrence=None)
    restored = Item.from_dict(encode(item))
    assert restored.end is None
    assert restored.recurrence is None
    assert restored.cancel_by is None


def test_missing_keys_fall_back_to_field_defaults():
    """A field added in a later release must not break an older document."""
    restored = Item.from_dict({"id": "d_x", "name": "X"})
    assert restored.type == EXPENSE
    assert restored.amount == 0.0
    assert restored.tags == []
    assert restored.expanded is False
    assert restored.overrides == {}


def test_unknown_keys_are_ignored():
    restored = Item.from_dict({"id": "d_x", "name": "X", "future_field": {"a": 1}})
    assert restored.id == "d_x"
    assert not hasattr(restored, "future_field")


def test_nested_dataclasses_round_trip():
    document = StoreDocument(
        items=[Item(id="d_a", name="A", type=INCOME, amount=1.0, start=date(2026, 1, 1))],
        accounts=[Account(id="a_1", name="Everyday", balance=10.0, currency="AUD")],
        transactions=[Transaction(id="t_1", date=date(2026, 9, 1), amount=-4.5,
                                  description="CAFE", tags=["food"])],
    )

    restored = StoreDocument.from_dict(encode(document))

    assert restored == document
    assert isinstance(restored.items[0], Item)
    assert isinstance(restored.transactions[0].date, date)


def test_canvas_widgets_round_trip_with_config():
    canvas = Canvas(id="c_1", name="Bills", widgets=[
        PlacedWidget(widget="hero_pl", x=0, y=0, w=6, h=3, config={"period": "month"}),
    ])

    restored = Canvas.from_dict(encode(canvas))

    assert restored == canvas
    assert restored.widgets[0].config == {"period": "month"}


def test_encode_rejects_unknown_types_rather_than_guessing():
    with pytest.raises(TypeError):
        encode({"bad": object()})


def test_decode_rejects_a_non_object_for_a_dataclass():
    with pytest.raises(TypeError):
        decode_dataclass(Item, ["not", "an", "object"])


def test_list_and_dict_primitives_decode():
    assert decode(["2026-01-01", "2026-02-01"], list[date]) == [
        date(2026, 1, 1), date(2026, 2, 1)]
    assert decode({"a": 1.5}, dict[str, float]) == {"a": 1.5}
    assert decode(None, date | None) is None
    assert decode("2026-03-01", date | None) == date(2026, 3, 1)
