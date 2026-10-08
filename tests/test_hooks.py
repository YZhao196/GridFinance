from dataclasses import replace
from datetime import date

import pytest

from finance_tool.hooks import cache as hc
from finance_tool.hooks import templates as ht
from finance_tool.hooks.types import HookContext, Row, WidgetData, widget_data

from fixtures import TODAY, build_store

OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def ctx(store):
    return HookContext(store=store.view(), lens="month", start=OCTOBER[0], end=OCTOBER[1],
                       label="October 2026", today=TODAY)


def series(*pairs):
    return [(date(2026, 10, day), value) for day, value in pairs]


# ---------------------------------------------------------------------- WidgetData


def test_the_sign_is_derived_unless_it_is_set():
    assert WidgetData(value=10).effective_sign == 1
    assert WidgetData(value=-10).effective_sign == -1
    assert WidgetData(value=0).effective_sign == 0
    assert WidgetData(value=None).effective_sign == 0
    # An explicit sign overrides the value — for a figure where up is bad.
    assert WidgetData(value=10, sign=-1).effective_sign == -1


def test_one_point_is_a_fact_and_two_is_a_trend():
    assert WidgetData(series=series((1, 5))).has_series is False
    assert WidgetData(series=series((1, 5), (2, 6))).has_series is True
    assert WidgetData(series=None).has_series is False
    assert WidgetData(series=[]).has_series is False


def test_emptiness():
    assert WidgetData().is_empty is True
    assert WidgetData(value=0).is_empty is False
    assert WidgetData(note="+12 uncategorised").is_empty is False
    assert WidgetData(rows=[]).is_empty is False


def test_row_sign_follows_the_value_by_default():
    assert Row(label="x", value=5.0).effective_sign == 1
    assert Row(label="x", value=-5.0).effective_sign == -1
    assert Row(label="x", value=None).effective_sign == 0
    assert Row(label="x", value=5.0, sign=-1).effective_sign == -1


def test_widget_data_helpers_do_not_mutate():
    original = widget_data(10.0, series=series((1, 1)), note="hi")
    assert original.with_value(20.0).value == 20.0
    assert original.value == 10.0
    assert original.without_series().series is None
    assert original.series


def test_the_context_exposes_its_span_and_its_peers(ctx):
    peer = WidgetData(value=99.0)
    ctx = replace(ctx, peers={"hero": peer})

    assert ctx.span == OCTOBER
    assert ctx.peer("hero") is peer
    assert ctx.peer("nobody") is None


# ----------------------------------------------------------------------- templates


def constant(value):
    return lambda ctx, config: WidgetData(value=value)


def test_ratio_divides_and_reports_its_parts(ctx):
    hook = ht.ratio(constant(4550.0), constant(1299.49))
    data = hook(ctx, {})

    assert data.value == pytest.approx(4550.0 / 1299.49)
    assert data.extra["numerator"] == 4550.0
    assert data.extra["denominator"] == 1299.49


def test_ratio_is_guarded_against_a_zero_denominator(ctx):
    assert ht.ratio(constant(10.0), constant(0.0))(ctx, {}) is None
    assert ht.ratio(constant(10.0), constant(None))(ctx, {}) is None


def test_ratio_of_nothing_is_nothing(ctx):
    missing = lambda ctx, config: None
    assert ht.ratio(missing, constant(10.0))(ctx, {}) is None
    assert ht.ratio(constant(10.0), missing)(ctx, {}) is None


def test_ratio_accepts_a_plain_number_as_a_constant(ctx):
    assert ht.ratio(constant(12.0), 12)(ctx, {}).value == pytest.approx(1.0)


def test_ratio_pairs_series_on_dates_not_on_position(ctx):
    """Two widgets built from different lenses must not be zipped positionally."""
    left = lambda ctx, config: WidgetData(series=series((1, 10), (2, 20), (3, 30)))
    right = lambda ctx, config: WidgetData(series=series((2, 2), (3, 3)))
    data = ht.ratio(left, right)(ctx, {})

    assert data.series == [(date(2026, 10, 2), pytest.approx(10.0)),
                           (date(2026, 10, 3), pytest.approx(10.0))]


def test_ratio_ignores_a_zero_denominator_point_in_the_series(ctx):
    left = lambda ctx, config: WidgetData(series=series((1, 10), (2, 20)))
    right = lambda ctx, config: WidgetData(value=2.0, series=series((1, 0), (2, 2)))
    data = ht.ratio(left, right)(ctx, {})
    assert data.series == [(date(2026, 10, 2), pytest.approx(10.0))]


def test_share_of_returns_a_percentage(ctx):
    data = ht.share_of(constant(25.0), constant(200.0))(ctx, {})
    assert data.value == pytest.approx(12.5)
    assert data.extra["unit"] == "%"


def test_delta_subtracts_and_carries_its_parts(ctx):
    data = ht.delta(constant(4550.0), constant(4100.0))(ctx, {})
    assert data.value == pytest.approx(450.0)
    assert data.extra["minuend"] == 4550.0
    assert data.extra["subtrahend"] == 4100.0


def test_delta_can_go_negative(ctx):
    assert ht.delta(constant(4100.0), constant(4550.0))(ctx, {}).value == pytest.approx(-450.0)


def test_delta_of_a_series(ctx):
    left = lambda ctx, config: WidgetData(series=series((1, 10), (2, 20)))
    right = lambda ctx, config: WidgetData(series=series((1, 4), (2, 5)))
    assert ht.delta(left, right)(ctx, {}).series == [
        (date(2026, 10, 1), pytest.approx(6.0)),
        (date(2026, 10, 2), pytest.approx(15.0)),
    ]


def test_cumulative_runs_a_total(ctx):
    hook = ht.cumulative(lambda ctx, config: WidgetData(series=series((1, 10), (2, -3), (3, 5))))
    data = hook(ctx, {})

    assert data.series == [
        (date(2026, 10, 1), pytest.approx(10.0)),
        (date(2026, 10, 2), pytest.approx(7.0)),
        (date(2026, 10, 3), pytest.approx(12.0)),
    ]
    assert data.value == pytest.approx(12.0)      # the running total is the period total
    assert data.extra["cumulative"] is True


def test_cumulative_of_a_scalar_leaves_it_alone(ctx):
    data = ht.cumulative(constant(7.5))(ctx, {})
    assert data.value == pytest.approx(7.5)
    assert data.series is None


def test_rolling_mean_grows_in_from_the_left(ctx):
    hook = ht.rolling_mean(
        lambda ctx, config: WidgetData(series=series((1, 10), (2, 20), (3, 30))), 2)
    data = hook(ctx, {})

    # The first point is the mean of what exists; the rest are true two-point means.
    assert [value for _, value in data.series] == [10.0, pytest.approx(15.0),
                                                   pytest.approx(25.0)]
    assert data.extra["window"] == 2


def test_rolling_mean_of_a_window_longer_than_the_series_still_works(ctx):
    hook = ht.rolling_mean(lambda ctx, config: WidgetData(series=series((1, 10))), 12)
    data = hook(ctx, {})
    assert len(data.series) == 1
    assert data.series[0][1] == pytest.approx(10.0)


def test_a_window_of_zero_or_less_is_clamped(ctx):
    hook = ht.rolling_mean(lambda ctx, config: WidgetData(series=series((1, 4), (2, 8))), 0)
    assert [v for _, v in hook(ctx, {}).series] == [4.0, 8.0]


def test_scale(ctx):
    data = ht.scale(constant(10.0), 3.0)(ctx, {})
    assert data.value == pytest.approx(30.0)


def test_scale_of_a_series(ctx):
    data = ht.scale(lambda ctx, config: WidgetData(series=series((1, 2), (2, 4))), 2.0)(ctx, {})
    assert [v for _, v in data.series] == [4.0, 8.0]


def test_scale_leaves_an_absent_value_absent(ctx):
    missing = lambda ctx, config: WidgetData()
    assert ht.scale(missing, 2.0)(ctx, {}).value is None


def test_combine_sums_and_means(ctx):
    assert ht.combine(constant(1.0), constant(2.0), constant(3.0))(ctx, {}).value == \
        pytest.approx(6.0)
    assert ht.combine(constant(1.0), constant(2.0), constant(3.0),
                      op="mean")(ctx, {}).value == pytest.approx(2.0)


def test_combine_ignores_the_absent_and_refuses_the_empty(ctx):
    missing = lambda ctx, config: None
    assert ht.combine(constant(1.0), missing)(ctx, {}).value == pytest.approx(1.0)
    assert ht.combine(missing, missing)(ctx, {}) is None


def test_templates_are_not_a_closed_grammar(ctx):
    """§28: anything outside the set is still just a Python function."""
    def income_over_expenses(ctx, config):
        data = ctx.store
        from finance_tool.engine import ledger as led
        totals = led.plan_totals(data, ctx.span)
        if not totals.expenses:
            return None
        return WidgetData(value=totals.income / totals.expenses)

    assert income_over_expenses.__class__.__name__ == "function"

    data = income_over_expenses(ctx, {})
    assert data.value == pytest.approx(4550.0 / 1299.49)


def test_the_registry_names_every_template():
    assert set(ht.TEMPLATES) == {"ratio", "share_of", "delta", "cumulative",
                                 "rolling_mean", "scale", "combine"}


# --------------------------------------------------------------------------- cache


def test_freeze_makes_anything_hashable():
    assert hc.freeze({"a": 1, "b": 2}) == hc.freeze({"b": 2, "a": 1})
    assert hc.freeze([1, {"a": [2, 3]}]) == hc.freeze([1, {"a": [2, 3]}])
    assert hash(hc.freeze({"a": [1, 2], "b": {"c": 3}}))
    assert hc.freeze(None) is None


def test_the_key_covers_everything_the_output_depends_on(store, ctx):
    base = hc.cache_key("hero", {"period": "month"}, ctx)

    assert base != hc.cache_key("hero", {"period": "month"}, replace(ctx, lens="year"))
    assert base != hc.cache_key("hero", {"period": "month"}, replace(ctx, start=date(2026, 9, 1)))
    assert base != hc.cache_key("hero", {"period": "week"}, ctx)
    assert base != hc.cache_key("other", {"period": "month"}, ctx)


def test_a_store_write_invalidates_by_exact_key(store, ctx):
    """§28: memoised on store.version, so invalidation is exact rather than timed."""
    before = hc.cache_key("hero", {}, ctx)
    store.version += 1
    after = hc.cache_key("hero", {}, replace(ctx, store=store.view()))
    assert before != after


def test_a_currency_change_invalidates(store, ctx):
    before = hc.cache_key("hero", {}, ctx)
    store.settings.currency = "NZD"
    after = hc.cache_key("hero", {}, replace(ctx, store=store.view()))
    assert before != after


def test_a_cached_call_is_computed_once(ctx):
    calls = []

    def hook(ctx, config):
        calls.append(config)
        return WidgetData(value=42.0)

    cache = hc.HookCache()
    first = cache.call("h", hook, ctx, {"a": 1})
    second = cache.call("h", hook, ctx, {"a": 1})

    assert first is second
    assert len(calls) == 1
    assert cache.hits == 1
    assert cache.misses == 1


def test_a_different_config_is_a_different_entry(ctx):
    cache = hc.HookCache()
    cache.call("h", lambda ctx, config: WidgetData(value=config.get("n", 0)), ctx, {"n": 1})
    cache.call("h", lambda ctx, config: WidgetData(value=config.get("n", 0)), ctx, {"n": 2})
    assert cache.misses == 2
    assert len(cache) == 2


def test_absence_is_cached_too(ctx):
    """A hook that returns None is a result, and asking again should not recompute it."""
    calls = []

    def hook(ctx, config):
        calls.append(1)
        return None

    cache = hc.HookCache()
    assert cache.call("h", hook, ctx) is None
    assert cache.call("h", hook, ctx) is None
    assert len(calls) == 1


def test_a_hook_is_not_handed_the_callers_config_dict(ctx):
    """A hook must not be able to mutate the config the caller keeps."""
    seen = {}

    def hook(ctx, config):
        config["touched"] = True
        seen.update(config)
        return None

    original = {"a": 1}
    hc.HookCache().call("h", hook, ctx, original)

    assert seen == {"a": 1, "touched": True}
    assert original == {"a": 1}


def test_the_cache_evicts_the_oldest_first(ctx):
    cache = hc.HookCache(maxsize=2)
    cache.call("a", lambda ctx, config: WidgetData(value=1.0), ctx)
    cache.call("b", lambda ctx, config: WidgetData(value=2.0), ctx)
    cache.call("c", lambda ctx, config: WidgetData(value=3.0), ctx)

    assert len(cache) == 2
    assert cache.get(hc.cache_key("a", {}, ctx))[0] is False
    assert cache.get(hc.cache_key("c", {}, ctx))[0] is True


def test_clearing_and_the_hit_rate(ctx):
    cache = hc.HookCache()
    assert cache.hit_rate is None

    cache.call("h", lambda ctx, config: WidgetData(value=1.0), ctx)
    cache.call("h", lambda ctx, config: WidgetData(value=1.0), ctx)
    assert cache.hit_rate == pytest.approx(0.5)

    cache.clear()
    assert len(cache) == 0
    assert cache.hit_rate is None


def test_a_maxsize_below_one_is_clamped():
    assert hc.HookCache(maxsize=0).maxsize == 1


def test_peers_are_gathered_from_the_canvas(ctx):
    data = {"a": WidgetData(value=1.0), "b": None, "c": WidgetData(value=3.0)}
    peers = hc.peers_of(None, data)

    assert set(peers) == {"a", "c"}
    assert hc.with_peers(ctx, peers).peer("a").value == 1.0
