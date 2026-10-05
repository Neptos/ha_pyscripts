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
    # buy 10 * 3 direct + sell 4 * (2 exported + 1 stored) = 42 -> 0.42
    result = savings._calculate_overall_solar_savings_last_hour(
        solar_to_house_kwh=3, solar_to_grid_kwh=2, solar_to_battery_kwh=1,
        buy_price=10, sell_price=4,
    )
    assert result == pytest.approx(0.42)


def test_battery_savings_sign_and_value(savings):
    # charging hour: -(10*2 grid in) - (4*1 solar in) = -24 -> -0.24
    charging = savings._calculate_battery_savings_last_hour(
        battery_to_house_kwh=0, battery_to_grid_kwh=0, grid_to_battery_kwh=2, solar_to_battery_kwh=1,
        buy_price=10, sell_price=4,
    )
    assert charging == pytest.approx(-0.24)
    # discharging hour: 10*2.5 to house + 4*0.5 to grid = 27 -> 0.27
    discharging = savings._calculate_battery_savings_last_hour(
        battery_to_house_kwh=2.5, battery_to_grid_kwh=0.5, grid_to_battery_kwh=0, solar_to_battery_kwh=0,
        buy_price=10, sell_price=4,
    )
    assert discharging == pytest.approx(0.27)


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

FLOWS = {
    "solar_to_house": "sensor.kotiakku_solar_to_house_kwh",
    "solar_to_grid": "sensor.kotiakku_solar_to_grid_kwh",
    "solar_to_battery": "sensor.kotiakku_solar_to_battery_kwh",
    "grid_to_house": "sensor.kotiakku_grid_to_house_kwh",
    "grid_to_battery": "sensor.kotiakku_grid_to_battery_kwh",
    "battery_to_house": "sensor.kotiakku_battery_to_house_kwh",
    "battery_to_grid": "sensor.kotiakku_battery_to_grid_kwh",
}
HELPERS = (
    "input_number.solar_savings",
    "input_number.battery_savings",
    "input_number.car_charge_without_solar",
    "input_number.car_charge_with_solar",
    "input_number.heat_pump_cost_without_solar",
    "input_number.heat_pump_cost_with_solar",
    "input_number.heat_pump_consumed_kwh",
)


def _flow_history(deltas):
    """History rows for each Kotiakku flow total rising by the given kWh."""
    return {FLOWS[k]: [_hist_row(BASE, 100.0), _hist_row(BASE, 100.0 + v)] for k, v in deltas.items()}


def test_calculate_solar_savings_full_flow(savings, world, monkeypatch):
    """One hour with every flow active: solar 3 direct, 2 exported, 1 stored;
    grid 4 to house, 2 to battery; battery 1 to house. Buy 10, sell 4 c/kWh.
    Car charged 2 kWh, heat pump used 1.5 kWh. One 'unavailable' row must be
    tolerated (M8)."""
    tesla = "sensor.tesla_wall_connector_energy"
    history = _flow_history({
        "solar_to_house": 3.0, "solar_to_grid": 2.0, "solar_to_battery": 1.0,
        "grid_to_house": 4.0, "grid_to_battery": 2.0,
        "battery_to_house": 1.0, "battery_to_grid": 0.0,
    })
    history[FLOWS["solar_to_house"]].insert(1, types.SimpleNamespace(state="unavailable", last_updated=BASE))
    history[tesla] = [_hist_row(BASE, 0.0), _hist_row(BASE, 2000.0)]

    monkeypatch.setattr(savings, "_calculate_weighted_average_price",
                        lambda start, end, price_id, *a, **k: 10.0 if "nordpool" in price_id else 4.0)
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: history)
    monkeypatch.setattr(savings, "_get_statistic", lambda *a, **k: pytest.fail("prices should not fall back"))

    get_map = {"sensor.nibe_energy_used_last_hour": "1.5"}
    get_map.update({e: "100" for e in FLOWS.values()})
    get_map.update({h: "0" for h in HELPERS})
    attrs = {h: {"device_class": "monetary"} for h in HELPERS}
    w = world(savings, get=get_map, attrs=attrs)

    savings.calculateSolarSavingsLastHour()

    written = dict(w.state.set_calls)
    # solar: 10*3 + 4*(2+1) = 42 c
    assert written["input_number.solar_savings"] == pytest.approx(0.42)
    # battery: 10*1 to house - 10*2 grid in - 4*1 solar in = -14 c
    assert written["input_number.battery_savings"] == pytest.approx(-0.14)
    # house total 3+4+1 = 8 kWh, non-solar 5 kWh -> car share 2/8, pump share 1.5/8
    assert written["input_number.car_charge_without_solar"] == pytest.approx(0.20)
    assert written["input_number.car_charge_with_solar"] == pytest.approx(10 * 5 * (2 / 8) / 100)
    assert written["input_number.heat_pump_cost_without_solar"] == pytest.approx(0.15)
    assert written["input_number.heat_pump_cost_with_solar"] == pytest.approx(10 * 5 * (1.5 / 8) / 100)
    assert written["input_number.heat_pump_consumed_kwh"] == pytest.approx(1.5)


def test_calculate_solar_savings_skips_battery_metric_when_helper_missing(savings, world, monkeypatch):
    """input_number.battery_savings not created yet: pyscript raises NameError on
    state.get; book everything else, warn once, never write the missing helper."""
    tesla = "sensor.tesla_wall_connector_energy"
    history = _flow_history({k: 1.0 for k in FLOWS})
    history[tesla] = [_hist_row(BASE, 0.0), _hist_row(BASE, 1000.0)]
    monkeypatch.setattr(savings, "_calculate_weighted_average_price", lambda *a, **k: 10.0)
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: history)

    get_map = {"sensor.nibe_energy_used_last_hour": "1.0"}
    get_map.update({e: "100" for e in FLOWS.values()})
    get_map.update({h: "0" for h in HELPERS if h != "input_number.battery_savings"})
    w = world(savings, get=get_map, attrs={h: {"device_class": "monetary"} for h in HELPERS})

    class _State(type(w.state)):
        def get(self, entity):
            if entity == "input_number.battery_savings":
                raise NameError("name 'input_number.battery_savings' is not defined")
            return super().get(entity)
    w.state.__class__ = _State
    savings._WARNED.clear()

    savings.calculateSolarSavingsLastHour()
    savings.calculateSolarSavingsLastHour()

    written = {entity for entity, _ in w.state.set_calls}
    assert "input_number.battery_savings" not in written
    assert "input_number.solar_savings" in written
    assert len([r for r in w.log.records if r[0] == "warning" and "battery_savings" in r[1]]) == 1


def test_calculate_solar_savings_totals_unavailable_books_no_solar_benefit(savings, world, monkeypatch):
    """Grid/inverter totals unavailable (Elisa took the inverter, no P1 yet):
    cost with solar == cost without solar, overall savings untouched, heat pump
    kWh still accumulated, and no history-based solar math is attempted."""
    tesla = "sensor.tesla_wall_connector_energy"
    history = {tesla: [_hist_row(BASE, 0.0), _hist_row(BASE, 2000.0)]}

    monkeypatch.setattr(savings, "_calculate_weighted_average_price", lambda *a, **k: 10.0)
    monkeypatch.setattr(savings, "_get_history", lambda *a, **k: history)
    monkeypatch.setattr(savings, "_calculate_overall_solar_savings_last_hour", lambda *a, **k: pytest.fail("solar path must be skipped"))

    get_map = {
        "sensor.nibe_energy_used_last_hour": "1.5",
        "sensor.kotiakku_solar_to_house_kwh": "unavailable",
        "input_number.solar_savings": "100",
        "input_number.car_charge_without_solar": "0",
        "input_number.car_charge_with_solar": "0",
        "input_number.heat_pump_cost_without_solar": "0",
        "input_number.heat_pump_cost_with_solar": "0",
        "input_number.heat_pump_consumed_kwh": "0",
    }
    attrs = {k: {"device_class": "monetary"} for k in get_map if k.startswith("input_number.")}
    w = world(savings, get=get_map, attrs=attrs)

    savings.calculateSolarSavingsLastHour()

    written = dict(w.state.set_calls)
    assert "input_number.solar_savings" not in written
    # 2 kWh car at 10 c/kWh = 0.20 EUR; 1.5 kWh heat pump = 0.15 EUR
    assert written["input_number.car_charge_without_solar"] == pytest.approx(0.20)
    assert written["input_number.car_charge_with_solar"] == pytest.approx(0.20)
    assert written["input_number.heat_pump_cost_without_solar"] == pytest.approx(0.15)
    assert written["input_number.heat_pump_cost_with_solar"] == pytest.approx(0.15)
    assert written["input_number.heat_pump_consumed_kwh"] == pytest.approx(1.5)


def test_energy_totals_available(savings, world):
    world(savings, get={"sensor.a": "12.5", "sensor.b": "unavailable", "sensor.c": None})
    assert savings._energy_totals_available("sensor.a")
    assert not savings._energy_totals_available("sensor.a", "sensor.b")
    assert not savings._energy_totals_available("sensor.c")


def test_energy_totals_available_missing_entity_raises_nameerror(savings, monkeypatch):
    """pyscript raises NameError for an entity that does not exist (integration
    disabled); that must read as unavailable, not crash the hourly run."""
    class _State:
        def get(self, entity_id):
            raise NameError(f"name '{entity_id}' is not defined")
    monkeypatch.setattr(savings, "state", _State())
    assert not savings._energy_totals_available("sensor.power_meter_consumption")
