"""Unit tests for pure pricing/energy functions in TeslaSmartCharging.py.

All access goes through the injected `tesla` fixture module (see tests/conftest.py).
Prices are in c/kWh throughout.
"""

from datetime import datetime, timedelta, timezone

import pytest


def _iso(dt):
    """Round-trip through fromisoformat to match the source parse path (+HH:MM offset)."""
    return datetime.fromisoformat(dt.isoformat()).isoformat()


def test_normalize_splits_hourly_into_four(tesla):
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    entry = {'start': _iso(start), 'end': _iso(end), 'value': 5.5}

    out = tesla._normalize_price_data([entry])

    assert len(out) == 4
    for e in out:
        assert e['value'] == 5.5
        s = datetime.fromisoformat(e['start'])
        en = datetime.fromisoformat(e['end'])
        assert (en - s).total_seconds() == 15 * 60


def test_normalize_keeps_15min_asis(tesla):
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=15)
    entry = {'start': _iso(start), 'end': _iso(end), 'value': 3.0}

    out = tesla._normalize_price_data([entry])

    assert len(out) == 1
    assert out[0] is entry


def test_find_price_at_slot_matches_and_missing(tesla):
    slot_start = datetime(2026, 7, 1, 10, 15, tzinfo=timezone.utc)
    prices = [
        {'start': _iso(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)), 'value': 1.0},
        {'start': _iso(slot_start), 'value': 7.25},
    ]

    matched = tesla._find_price_at_slot(prices, slot_start)
    assert isinstance(matched, float)
    assert matched == 7.25

    absent = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)
    assert tesla._find_price_at_slot(prices, absent) is None


def test_blended_pure_solar_returns_sell_price(tesla):
    # excess >= MIN_CHARGE_POWER_W -> pure solar, effective price = sell_price
    assert tesla._calculate_blended_effective_price(
        tesla.MIN_CHARGE_POWER_W, 10, 4
    ) == 4


def test_blended_weighted_average(tesla):
    # surplus at 50% of MIN_CHARGE_POWER_W -> 0.5*sell + 0.5*buy
    surplus = tesla.MIN_CHARGE_POWER_W * 0.5
    result = tesla._calculate_blended_effective_price(surplus, 10, 4)
    assert result == pytest.approx(0.5 * 4 + 0.5 * 10)


def test_blended_edge_cases(tesla):
    # excess < 0 -> treated as 0 -> full grid price (buy)
    assert tesla._calculate_blended_effective_price(-100, 10, 4) == pytest.approx(10.0)
    # buy None -> high default
    assert tesla._calculate_blended_effective_price(0, None, 4) == 999.0
    # sell None -> treated as 0; at 50% surplus effective = grid_fraction * buy
    surplus = tesla.MIN_CHARGE_POWER_W * 0.5
    assert tesla._calculate_blended_effective_price(surplus, 10, None) == pytest.approx(0.5 * 10)


def test_kwh_needed_efficiency(tesla):
    # (30/100 * 75) / 0.9 == 25.0
    assert tesla._calculate_kwh_needed(50, 80) == pytest.approx(25.0)
    # current >= target -> 0.0
    assert tesla._calculate_kwh_needed(80, 80) == 0.0
    assert tesla._calculate_kwh_needed(90, 80) == 0.0


def test_charging_hours_needed(tesla):
    # 9 kWh at MAX_CHARGE_RATE_KW (9) == 1.0 h
    assert tesla._calculate_charging_hours_needed(9) == pytest.approx(1.0)
    assert tesla._calculate_charging_hours_needed(0) == 0.0
    assert tesla._calculate_charging_hours_needed(-5) == 0.0


def test_target_amps_clamped(tesla):
    # 6900W / 690 == 10A, within range
    assert tesla._calculate_target_amps_from_power(6900) == 10
    # below min -> MIN_CHARGE_AMPS
    assert tesla._calculate_target_amps_from_power(100) == tesla.MIN_CHARGE_AMPS
    # above max -> MAX_CHARGE_AMPS
    assert tesla._calculate_target_amps_from_power(999999) == tesla.MAX_CHARGE_AMPS
