"""Tests for the solar-forecast + solar-aware effective-price path.

Covers the per-day daylight gating (projecting sun wall-clock onto the slot
date), the remaining-today curve renormalization, and the effective-price blend.

Conventions (KD-10): local tz derived from the frozen clock, never a hard-coded
offset. No generator expressions (repo rule).
"""

import datetime

import pytest
from freezegun import freeze_time


def _local_tz():
    """Local tz from the frozen clock. Call inside a freeze_time block."""
    return datetime.datetime.now().astimezone().tzinfo


def _sun_get(tz, rising_dt, setting_dt, remaining_today=None, tomorrow=None):
    """Build the state.get map priming sun + solar sensors.

    Sun sensors report ISO strings (source parses with fromisoformat +
    Z-replacement). Solar sensors report kWh floats as strings.
    """

    def _make(tesla):
        get = {
            tesla.SUN_NEXT_RISING: rising_dt.isoformat(),
            tesla.SUN_NEXT_SETTING: setting_dt.isoformat(),
        }
        if remaining_today is not None:
            get[tesla.SOLAR_REMAINING_TODAY] = str(remaining_today)
        if tomorrow is not None:
            get[tesla.SOLAR_PRODUCTION_TOMORROW] = str(tomorrow)
        return get

    return _make


# --- M1: per-day daylight gating --------------------------------------------


def test_m1_inverted_sun_order_today_and_tomorrow(tesla, world):
    """next_rising tomorrow 04:00, next_setting today 22:00 (inverted daytime).

    Both today's 16:00 slot and tomorrow's 12:00 slot must get a nonzero
    forecast. Before the fix, the raw next-event comparison zeroed both (today's
    slot < tomorrow's sunrise; tomorrow's slot >= today's sunset).
    """
    with freeze_time("2026-07-01 15:00:00"):
        tz = _local_tz()
        # Inverted order: rising is tomorrow, setting is today.
        rising = datetime.datetime(2026, 7, 2, 4, 0, 0, tzinfo=tz)
        setting = datetime.datetime(2026, 7, 1, 22, 0, 0, tzinfo=tz)
        get = _sun_get(tz, rising, setting, remaining_today=6.0, tomorrow=30.0)(tesla)
        world(tesla, get=get)

        today_slot = datetime.datetime(2026, 7, 1, 16, 0, 0, tzinfo=tz)
        tomorrow_slot = datetime.datetime(2026, 7, 2, 12, 0, 0, tzinfo=tz)

        today_kw = tesla._get_solar_forecast_for_slot(today_slot)
        tomorrow_kw = tesla._get_solar_forecast_for_slot(tomorrow_slot)

        assert today_kw > 0.0
        assert tomorrow_kw > 0.0


def test_m1_outside_daylight_window_is_zero(tesla, world):
    """A slot before projected sunrise gets zero forecast."""
    with freeze_time("2026-07-01 03:00:00"):
        tz = _local_tz()
        rising = datetime.datetime(2026, 7, 1, 4, 0, 0, tzinfo=tz)
        setting = datetime.datetime(2026, 7, 1, 22, 0, 0, tzinfo=tz)
        get = _sun_get(tz, rising, setting, remaining_today=30.0)(tesla)
        world(tesla, get=get)

        # Slot at 03:30 is before the 04:00 projected sunrise.
        slot = datetime.datetime(2026, 7, 1, 3, 30, 0, tzinfo=tz)
        assert tesla._get_solar_forecast_for_slot(slot) == 0.0


def test_m1_sun_unavailable_skips_daylight_gate(tesla, world):
    """Sun sensors absent -> daylight gate skipped, SOLAR_CURVE hour still gates.

    Omitting SUN_NEXT_RISING/SUN_NEXT_SETTING from the get map drives
    _get_sunrise_sunset() to (None, None). The `if sunrise and sunset:` guard is
    then skipped, so a slot inside a SOLAR_CURVE hour must still get a nonzero
    forecast (hour-membership check remains the sole gate).
    """
    with freeze_time("2026-07-01 15:00:00"):
        tz = _local_tz()
        # No sun sensors primed; only the solar remaining-today forecast.
        world(tesla, get={tesla.SOLAR_REMAINING_TODAY: "6.0"})

        # Hour 16 is in SOLAR_CURVE.
        slot = datetime.datetime(2026, 7, 1, 16, 0, 0, tzinfo=tz)
        assert tesla._get_solar_forecast_for_slot(slot) > 0.0


# --- M18: remaining-today curve renormalization ------------------------------


def test_m18_remaining_curve_renormalized(tesla, world):
    """At 15:00 with 6 kWh remaining, hour-16 slot uses renormalized fraction.

    available_kw == 6 * curve[16]/sum(curve[h] for h>=15) * 0.80 - 1.0 baseload.
    Before the fix the full-day fraction was used (much smaller), which combined
    with baseload subtraction yielded 0.0.
    """
    with freeze_time("2026-07-01 15:00:00"):
        tz = _local_tz()
        rising = datetime.datetime(2026, 7, 1, 4, 0, 0, tzinfo=tz)
        setting = datetime.datetime(2026, 7, 1, 22, 0, 0, tzinfo=tz)
        get = _sun_get(tz, rising, setting, remaining_today=6.0)(tesla)
        world(tesla, get=get)

        slot = datetime.datetime(2026, 7, 1, 16, 0, 0, tzinfo=tz)
        avail = tesla._get_solar_forecast_for_slot(slot)

        remaining_sum = sum([tesla.SOLAR_CURVE[h] for h in tesla.SOLAR_CURVE if h >= 15])
        frac = tesla.SOLAR_CURVE[16] / remaining_sum
        expected = 6.0 * frac * tesla.SOLAR_FORECAST_CONFIDENCE - tesla.BASELOAD_ESTIMATE_KW

        assert avail == pytest.approx(expected, abs=0.05)
        assert avail > 0.0


# --- M14: solar-aware effective price ----------------------------------------


def test_m14_partial_solar_blends_between_prices(tesla, world):
    """Partial-solar slot -> sell_price < effective < buy_price, solar-proportional."""
    with freeze_time("2026-07-01 12:00:00"):
        tz = _local_tz()
        rising = datetime.datetime(2026, 7, 1, 4, 0, 0, tzinfo=tz)
        setting = datetime.datetime(2026, 7, 1, 22, 0, 0, tzinfo=tz)
        # Modest remaining forecast -> partial solar coverage of the slot.
        get = _sun_get(tz, rising, setting, remaining_today=10.0)(tesla)
        world(tesla, get=get)

        slot = datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz)
        buy_price = 20.0
        sell_price = 4.0
        eff, solar_e, grid_e = tesla._calculate_effective_price(slot, buy_price, sell_price)

        assert solar_e > 0.0
        assert grid_e > 0.0
        assert sell_price < eff < buy_price


def test_m14_full_solar_coverage_equals_sell(tesla, world):
    """Forecast large enough to cover full charge rate -> effective == sell, grid == 0."""
    with freeze_time("2026-07-01 12:00:00"):
        tz = _local_tz()
        rising = datetime.datetime(2026, 7, 1, 4, 0, 0, tzinfo=tz)
        setting = datetime.datetime(2026, 7, 1, 22, 0, 0, tzinfo=tz)
        # Huge remaining forecast -> solar covers the whole slot + baseload.
        get = _sun_get(tz, rising, setting, remaining_today=500.0)(tesla)
        world(tesla, get=get)

        slot = datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz)
        buy_price = 20.0
        sell_price = 4.0
        eff, solar_e, grid_e = tesla._calculate_effective_price(slot, buy_price, sell_price)

        assert grid_e == pytest.approx(0.0)
        assert eff == pytest.approx(sell_price)


def test_m14_outside_curve_hour_equals_buy(tesla, world):
    """Slot at hour 22 (outside SOLAR_CURVE) -> effective == buy, solar == 0."""
    with freeze_time("2026-07-01 12:00:00"):
        tz = _local_tz()
        rising = datetime.datetime(2026, 7, 1, 4, 0, 0, tzinfo=tz)
        setting = datetime.datetime(2026, 7, 1, 23, 30, 0, tzinfo=tz)
        get = _sun_get(tz, rising, setting, remaining_today=30.0)(tesla)
        world(tesla, get=get)

        slot = datetime.datetime(2026, 7, 1, 22, 0, 0, tzinfo=tz)
        buy_price = 20.0
        sell_price = 4.0
        eff, solar_e, grid_e = tesla._calculate_effective_price(slot, buy_price, sell_price)

        assert solar_e == pytest.approx(0.0)
        assert eff == pytest.approx(buy_price)
