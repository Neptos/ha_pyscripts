"""Unit tests for EnergyContext net solar surplus.

Flow values are Kotiakku 5-min powers in W (all non-negative); numbers are
modelled on recorded October 2026 data. No generator expressions (repo rule).
"""


def _flows(solar_to_grid=0.0, solar_to_battery=0.0, battery_to_house=0.0, grid_to_house=0.0):
    return {
        "solar_to_grid": solar_to_grid,
        "solar_to_battery": solar_to_battery,
        "battery_to_house": battery_to_house,
        "grid_to_house": grid_to_house,
    }


# --- battery fills anyway -----------------------------------------------------


def test_full_battery_fills(energy):
    assert energy._battery_fills_anyway(94.0, 0.0, 0.0) is True


def test_big_forecast_fills_battery(energy):
    # headroom 21 * 0.55 = 11.55; house 0.7 * 6 = 4.2; forecast 25 * 0.8 = 20 -> 4.25 spare
    assert energy._battery_fills_anyway(40.0, 25.0, 6.0) is True


def test_small_forecast_does_not_fill(energy):
    # forecast 18 * 0.8 = 14.4 - 4.2 - 11.55 = -1.35
    assert energy._battery_fills_anyway(40.0, 18.0, 6.0) is False


def test_margin_required(energy):
    # forecast 22 * 0.8 = 17.6 - 4.2 - 11.55 = 1.85 < 2 kWh margin
    assert energy._battery_fills_anyway(40.0, 22.0, 6.0) is False


# --- net surplus --------------------------------------------------------------


def test_battery_charging_not_counted_when_it_would_not_fill(energy):
    flows = _flows(solar_to_battery=3000.0)
    assert energy._net_surplus(flows, False) == 0.0


def test_battery_charging_counted_when_it_fills_anyway(energy):
    flows = _flows(solar_to_grid=200.0, solar_to_battery=3000.0)
    assert energy._net_surplus(flows, True) == 3200.0


def test_loads_on_battery_or_grid_make_it_negative(energy):
    flows = _flows(battery_to_house=1500.0, grid_to_house=300.0)
    assert energy._net_surplus(flows, True) == -1800.0

