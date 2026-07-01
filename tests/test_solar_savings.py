"""Unit tests for the pure arithmetic cost functions in SolarSavings.py.

All results are divided by 100.0 (c/kWh -> EUR). Access via `savings` fixture.
"""

import pytest


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
