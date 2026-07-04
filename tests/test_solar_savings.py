"""Unit tests for the pure arithmetic cost functions in SolarSavings.py.

All results are divided by 100.0 (c/kWh -> EUR). Access via `savings` fixture.
"""

import types
from datetime import datetime, timezone, timedelta

import pytest


def _price_row(start_dt, value):
    """Statistic row as returned by _get_statistic (dict with 'start'/'state')."""
    return {"start": start_dt, "state": str(value)}


def _hist_row(updated_dt, value):
    return types.SimpleNamespace(state=str(value), last_updated=updated_dt)


def test_overall_savings(savings):
    # (10*(5-2) + 4*2)/100 = 38/100 = 0.38
    result = savings._calculate_overall_solar_savings_last_hour(
        last_hour_exported_kwh=2,
        last_hour_produced_kwh=5,
        last_hour_buy_price=10,
        last_hour_sell_price=4,
    )
    assert result == pytest.approx(0.38)


def test_car_without_solar(savings):
    # 20*3/100 = 0.6
    result = savings._calculate_car_charge_cost_without_solar_last_hour(
        last_hour_buy_price=20, last_hour_charged_kwh=3
    )
    assert result == pytest.approx(0.6)


def test_car_with_solar(savings):
    # 20*4*0.5/100 = 0.4
    result = savings._calculate_car_charge_cost_with_solar_last_hour(
        last_hour_buy_price=20, last_hour_purchased_kwh=4, car_share_of_purchase=0.5
    )
    assert result == pytest.approx(0.4)


def test_heat_pump_without_solar(savings):
    # 20*2/100 = 0.4
    result = savings._calculate_heat_pump_cost_without_solar_last_hour(
        last_hour_buy_price=20, last_hour_heat_pump_used_kwh=2
    )
    assert result == pytest.approx(0.4)


def test_heat_pump_with_solar(savings):
    # 20*4*0.25/100 = 0.2
    result = savings._calculate_heat_pump_cost_with_solar_last_hour(
        last_hour_buy_price=20, last_hour_purchased_kwh=4, heat_pump_share_of_purchase=0.25
    )
    assert result == pytest.approx(0.2)


# --- M6: consumption-weighted-average interval-width -------------------------

BASE = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_weighted_avg_no_smear_across_price_boundary(savings, monkeypatch):
    """Regression: 5-min price rows must use a 5-min interval, not 15.

    Price A for [:00,:15) as rows :00/:05/:10; price B for [:15,:30) as rows
    :15/:20/:25. All consumption falls in the B window, so the weighted average
    must equal B exactly. Before the fix (15-min windows) the :05/:10 rows would
    overlap into the B window and smear the result toward A.
    """
    price_rows = [
        _price_row(BASE + timedelta(minutes=0), 10.0),   # A
        _price_row(BASE + timedelta(minutes=5), 10.0),   # A
        _price_row(BASE + timedelta(minutes=10), 10.0),  # A
        _price_row(BASE + timedelta(minutes=15), 2.0),   # B
        _price_row(BASE + timedelta(minutes=20), 2.0),   # B
        _price_row(BASE + timedelta(minutes=25), 2.0),   # B
    ]
    # Consumption entirely within [:15,:30): monotonically rising meter.
    hist_rows = [
        _hist_row(BASE + timedelta(minutes=16), 100.0),
        _hist_row(BASE + timedelta(minutes=21), 105.0),
        _hist_row(BASE + timedelta(minutes=26), 110.0),
    ]

    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: {"p": price_rows})
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: {"c": hist_rows})

    result = savings._calculate_weighted_average_price(BASE, BASE + timedelta(hours=1), "p", "c")
    assert result == pytest.approx(2.0)


def test_weighted_avg_differs_from_simple_average(savings, monkeypatch):
    """Consumption concentrated in the cheap interval pulls the weighted avg below the simple mean."""
    price_rows = [
        _price_row(BASE + timedelta(minutes=0), 20.0),
        _price_row(BASE + timedelta(minutes=5), 20.0),
        _price_row(BASE + timedelta(minutes=10), 20.0),
        _price_row(BASE + timedelta(minutes=15), 2.0),
        _price_row(BASE + timedelta(minutes=20), 2.0),
        _price_row(BASE + timedelta(minutes=25), 2.0),
    ]
    hist_rows = [
        _hist_row(BASE + timedelta(minutes=16), 0.0),
        _hist_row(BASE + timedelta(minutes=21), 5.0),
        _hist_row(BASE + timedelta(minutes=26), 10.0),
    ]
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: {"p": price_rows})
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: {"c": hist_rows})

    simple = sum([float(r["state"]) for r in price_rows]) / len(price_rows)
    result = savings._calculate_weighted_average_price(BASE, BASE + timedelta(hours=1), "p", "c")
    assert result == pytest.approx(2.0)
    assert result < simple


def test_weighted_avg_empty_interval_then_populated(savings, monkeypatch):
    """L15 two-pointer: an empty price interval must not steal the next interval's deltas.

    Interval A [:00,:05) has NO consumption deltas; interval B [:05,:10) does.
    The forward index must skip A cleanly and attribute B's deltas to price B, so
    the weighted average equals price B exactly. An over-advancing index would
    drop or mis-attribute B's deltas.

    Stress for the skip-stale-deltas phase: a consumption delta is timestamped
    BEFORE interval A's start (:00). With the skip phase intact that stale delta
    is discarded and A stays empty (result == B == 3.0). If the skip phase is
    removed/broken, the stale delta gets swept into A's accumulate window
    [:00,:05) at the expensive price 20.0, dragging the weighted average up to
    (20*5 + 3*10)/15 ≈ 8.67 and this assertion FAILS.
    """
    price_rows = [
        _price_row(BASE + timedelta(minutes=0), 20.0),  # A: empty interval
        _price_row(BASE + timedelta(minutes=5), 3.0),   # B: has consumption
    ]
    # Meter readings. The :-4 -> :-2 rise produces a +5 delta timestamped at :-4
    # (STALE, before A's :00 start). The flat :-2 -> :06 pair yields no positive
    # delta, isolating the stale delta from interval A. Deltas at :06 (+4) and
    # :08 (+6) fall inside interval B [:05,:10).
    hist_rows = [
        _hist_row(BASE + timedelta(minutes=-4), 90.0),
        _hist_row(BASE + timedelta(minutes=-2), 95.0),
        _hist_row(BASE + timedelta(minutes=6), 95.0),
        _hist_row(BASE + timedelta(minutes=8), 99.0),
        _hist_row(BASE + timedelta(minutes=9), 105.0),
    ]
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: {"p": price_rows})
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: {"c": hist_rows})

    result = savings._calculate_weighted_average_price(BASE, BASE + timedelta(hours=1), "p", "c")
    assert result == pytest.approx(3.0)


def test_weighted_avg_uses_passed_consumption_history(savings, monkeypatch):
    """L4: a passed consumption_history row list is used without any _get_history call."""
    price_rows = [
        _price_row(BASE + timedelta(minutes=0), 20.0),
        _price_row(BASE + timedelta(minutes=5), 2.0),
    ]
    hist_rows = [
        _hist_row(BASE + timedelta(minutes=6), 0.0),
        _hist_row(BASE + timedelta(minutes=8), 10.0),
    ]
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: {"p": price_rows})
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: pytest.fail("history should not be fetched"))

    result = savings._calculate_weighted_average_price(
        BASE, BASE + timedelta(hours=1), "p", "c", consumption_history=hist_rows)
    assert result == pytest.approx(2.0)


def test_weighted_avg_falls_back_to_simple_when_history_missing(savings, monkeypatch):
    price_rows = [
        _price_row(BASE + timedelta(minutes=0), 4.0),
        _price_row(BASE + timedelta(minutes=5), 8.0),
    ]
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: {"p": price_rows})
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: None)

    result = savings._calculate_weighted_average_price(BASE, BASE + timedelta(hours=1), "p", "c")
    assert result == pytest.approx(6.0)  # simple average of 4 and 8


def test_weighted_avg_single_price_point_shortcut(savings, monkeypatch):
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: {"p": [_price_row(BASE, 7.5)]})
    # _get_history must not even be consulted for a single price point.
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: pytest.fail("history should not be fetched"))

    result = savings._calculate_weighted_average_price(BASE, BASE + timedelta(hours=1), "p", "c")
    assert result == pytest.approx(7.5)


# --- M4: netting + share-of-purchase -----------------------------------------

def test_net_energy_flows_purchased_exceeds_exported(savings):
    purchased, exported, solar = savings._net_energy_flows(5.0, 2.0, 1.0)
    assert purchased == pytest.approx(3.0)
    assert exported == pytest.approx(0.0)
    assert solar == pytest.approx(3.0)  # 1.0 + 2.0 moved into self-consumption


def test_net_energy_flows_exported_exceeds_purchased(savings):
    purchased, exported, solar = savings._net_energy_flows(2.0, 5.0, 1.0)
    assert purchased == pytest.approx(0.0)
    assert exported == pytest.approx(3.0)
    assert solar == pytest.approx(3.0)  # 1.0 + 2.0


def test_net_energy_flows_no_overlap_unchanged(savings):
    # Only exporting, nothing purchased -> no netting.
    purchased, exported, solar = savings._net_energy_flows(0.0, 5.0, 1.0)
    assert (purchased, exported, solar) == (0.0, 5.0, 1.0)


def test_share_of_purchase_zero_when_nothing_purchased(savings):
    assert savings._share_of_purchase(3.0, 0.0, 4.0) == 0.0


def test_share_of_purchase_proportional(savings):
    # 2 / (4 + 4) = 0.25
    assert savings._share_of_purchase(2.0, 4.0, 4.0) == pytest.approx(0.25)


# --- M8: robust delta / accumulation -----------------------------------------

def test_delta_from_history_ignores_unavailable_rows(savings):
    rows = [
        _hist_row(BASE, 100.0),
        types.SimpleNamespace(state="unavailable", last_updated=BASE),
        _hist_row(BASE, 105.0),
        types.SimpleNamespace(state="unknown", last_updated=BASE),
        _hist_row(BASE, 110.0),
    ]
    assert savings._delta_from_history(rows) == pytest.approx(10.0)


def test_delta_from_history_fewer_than_two_valid_returns_zero(savings):
    rows = [
        _hist_row(BASE, 100.0),
        types.SimpleNamespace(state="unavailable", last_updated=BASE),
    ]
    assert savings._delta_from_history(rows) == 0.0


def test_delta_from_history_idle_single_row_stays_silent(savings, world):
    """An idle sensor yielding 0-1 rows this hour is normal — no warning spam."""
    w = world(savings)
    assert savings._delta_from_history([_hist_row(BASE, 100.0)]) == 0.0
    assert savings._delta_from_history([]) == 0.0
    assert not any(
        "fewer than 2 valid points" in msg for _level, msg in w.log.records
    )


def test_delta_from_history_warns_when_fewer_than_two_valid(savings, world):
    """M8: a whole hour of unavailable data must leave a diagnostic warning."""
    w = world(savings)
    rows = [
        types.SimpleNamespace(state="unavailable", last_updated=BASE),
        types.SimpleNamespace(state="unknown", last_updated=BASE),
    ]
    assert savings._delta_from_history(rows) == 0.0
    assert any(
        level == "warning" and "fewer than 2 valid points" in msg
        for level, msg in w.log.records
    )


def test_sum_value_to_sensor_starts_from_zero_on_unknown(savings, world):
    w = world(savings, get={"input_number.x": "unknown"}, attrs={"input_number.x": {"device_class": "monetary"}})
    savings._sum_value_to_sensor(1.5, "input_number.x")
    # 0.0 + 1.5 written back.
    assert ("input_number.x", 1.5) in w.state.set_calls
    assert any("non-numeric" in msg for level, msg in w.log.records)


# --- M4/M7/M8: full calculateSolarSavingsLastHour world test -----------------

def test_calculate_solar_savings_full_flow(savings, world, monkeypatch):
    """Primed histories including one 'unavailable' row: all six accumulators
    written, no exception (crashed before the M8 fix)."""
    exported = "sensor.power_meter_exported"
    yield_id = "sensor.inverter_total_yield"
    tesla = "sensor.tesla_wall_connector_energy"
    purchased = "sensor.power_meter_consumption"

    history = {
        exported: [_hist_row(BASE, 0.0), _hist_row(BASE, 1.0)],
        yield_id: [
            _hist_row(BASE, 10.0),
            types.SimpleNamespace(state="unavailable", last_updated=BASE),
            _hist_row(BASE, 14.0),
        ],
        tesla: [_hist_row(BASE, 0.0), _hist_row(BASE, 2000.0)],
        purchased: [_hist_row(BASE, 0.0), _hist_row(BASE, 3.0)],
    }

    monkeypatch.setattr(savings, "_calculate_weighted_average_price", lambda *a, **k: 5.0)
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: history)
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: pytest.fail("prices should not fall back"))

    get_map = {
        "sensor.nibe_energy_used_last_hour": "1.5",
        "input_number.solar_savings": "0",
        "input_number.car_charge_without_solar": "0",
        "input_number.car_charge_with_solar": "0",
        "input_number.heat_pump_cost_without_solar": "0",
        "input_number.heat_pump_cost_with_solar": "0",
        "input_number.heat_pump_consumed_kwh": "0",
    }
    attrs = {k: {"device_class": "monetary"} for k in get_map if k.startswith("input_number.")}
    w = world(savings, get=get_map, attrs=attrs)

    savings.calculateSolarSavingsLastHour()

    written = {entity for entity, _ in w.state.set_calls}
    for helper in (
        "input_number.solar_savings",
        "input_number.car_charge_without_solar",
        "input_number.car_charge_with_solar",
        "input_number.heat_pump_cost_without_solar",
        "input_number.heat_pump_cost_with_solar",
        "input_number.heat_pump_consumed_kwh",
    ):
        assert helper in written


# --- M7: caller-side fallback-price guard (SolarSavings.py:258-269) -----------

BUY = "sensor.nordpool_kwh_fi_eur_3_10_0"
SELL = "sensor.electricity_sell_price"


def _run_with_fallback_stat(savings, world, monkeypatch, stat_return):
    """Force the weighted-price path to fail and drive the fallback _get_statistic.

    Returns the _World so callers can assert on log/writes. With the L4 change
    the combined history fetch runs FIRST, so it is stubbed to a valid dict;
    the price-fallback guard (weighted avg None + missing/empty stat) must still
    warn and skip before any accumulator write.
    """
    monkeypatch.setattr(savings, "_calculate_weighted_average_price", lambda *a, **k: None)
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: stat_return)
    history = {
        "sensor.power_meter_exported": [_hist_row(BASE, 0.0), _hist_row(BASE, 1.0)],
        "sensor.inverter_total_yield": [_hist_row(BASE, 10.0), _hist_row(BASE, 14.0)],
        "sensor.tesla_wall_connector_energy": [_hist_row(BASE, 0.0), _hist_row(BASE, 2000.0)],
        "sensor.power_meter_consumption": [_hist_row(BASE, 0.0), _hist_row(BASE, 3.0)],
    }
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: history)
    w = world(savings, get={"sensor.nibe_energy_used_last_hour": "1.5"})
    savings.calculateSolarSavingsLastHour()
    return w


def test_fallback_guard_missing_buy_key_warns_and_skips(savings, world, monkeypatch):
    """Weighted avg None + stat dict missing the buy key -> warn, no accumulator writes."""
    w = _run_with_fallback_stat(
        savings, world, monkeypatch, {SELL: [{"state": "2.0"}]}
    )
    assert w.state.set_calls == []
    assert any(level == "warning" and "buy price" in msg for level, msg in w.log.records)


def test_fallback_guard_empty_dict_warns_and_skips(savings, world, monkeypatch):
    """Weighted avg None + empty stat dict -> warn, no accumulator writes."""
    w = _run_with_fallback_stat(savings, world, monkeypatch, {})
    assert w.state.set_calls == []
    assert any(level == "warning" and "buy price" in msg for level, msg in w.log.records)


def test_fallback_guard_none_stat_warns_and_skips(savings, world, monkeypatch):
    """Weighted avg None + _get_statistic returns None (timeout) -> warn, no writes."""
    w = _run_with_fallback_stat(savings, world, monkeypatch, None)
    assert w.state.set_calls == []
    assert any(level == "warning" and "buy price" in msg for level, msg in w.log.records)


def test_fallback_guard_missing_sell_key_warns_and_skips(savings, world, monkeypatch):
    """Buy present, sell key missing -> the sell guard warns, no accumulator writes."""
    w = _run_with_fallback_stat(
        savings, world, monkeypatch, {BUY: [{"state": "5.0"}]}
    )
    assert w.state.set_calls == []
    assert any(level == "warning" and "sell price" in msg for level, msg in w.log.records)
