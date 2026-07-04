"""Cost-indicator pipeline + entry-guard tests for UpdateSpotPriceSensors.py.

Covers M16 (pipeline / _get_future_prices unit coverage) and M13 (fallback
zone-3 entry guards). Fake sensor is injected via ``mod.sensor``; the ``world``
fixture captures ``input_number`` value and attribute writes.
"""

import types
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time


@pytest.fixture
def long_term_cache_reset(spot):
    """Reset the module-level long-term cache after each test that touches it.

    The ``spot`` module fixture is session-scoped and ``_get_long_term_prices``
    mutates ``mod._long_term_cache`` in place (monkeypatch does NOT auto-revert
    in-place dict mutations), so cached state would otherwise leak into unrelated
    later tests. Teardown runs even on assertion failure.
    """
    yield
    spot._long_term_cache = {'day': None, 'prices': None}


# ---------------------------------------------------------------------------
# _get_future_prices unit tests
# ---------------------------------------------------------------------------

def test_future_prices_excludes_past(spot):
    with freeze_time("2026-07-03 12:00:00"):
        now = datetime.now().astimezone()
        today = [
            {'start': now - timedelta(minutes=30), 'value': 1.0},
            {'start': now + timedelta(minutes=15), 'value': 2.0},
            {'start': now + timedelta(minutes=30), 'value': 3.0},
        ]
        out = spot._get_future_prices(today, [], False, 10)
    assert out == [2.0, 3.0]


def test_future_prices_excludes_past_string_starts(spot):
    with freeze_time("2026-07-03 12:00:00"):
        now = datetime.now().astimezone()
        today = [
            {'start': (now - timedelta(minutes=30)).isoformat(), 'value': 1.0},
            {'start': (now + timedelta(minutes=15)).isoformat(), 'value': 2.0},
            {'start': (now + timedelta(minutes=30)).isoformat(), 'value': 3.0},
        ]
        out = spot._get_future_prices(today, [], False, 10)
    assert out == [2.0, 3.0]


def test_future_prices_respects_max(spot):
    with freeze_time("2026-07-03 12:00:00"):
        now = datetime.now().astimezone()
        today = [{'start': now + timedelta(minutes=15 * i), 'value': float(i)} for i in range(1, 8)]
        out = spot._get_future_prices(today, [], False, 3)
    assert out == [1.0, 2.0, 3.0]


def test_future_prices_appends_tomorrow_only_when_valid(spot):
    with freeze_time("2026-07-03 12:00:00"):
        now = datetime.now().astimezone()
        today = [{'start': now + timedelta(minutes=15), 'value': 1.0}]
        tomorrow = [{'start': now + timedelta(hours=13), 'value': 9.0}]
        with_tomorrow = spot._get_future_prices(today, tomorrow, True, 10)
        without_tomorrow = spot._get_future_prices(today, tomorrow, False, 10)
    assert with_tomorrow == [1.0, 9.0]
    assert without_tomorrow == [1.0]


def test_future_prices_skips_missing_start(spot):
    with freeze_time("2026-07-03 12:00:00"):
        now = datetime.now().astimezone()
        today = [
            {'value': 5.0},  # no 'start'
            {'start': now + timedelta(minutes=15), 'value': 2.0},
        ]
        out = spot._get_future_prices(today, [], False, 10)
    assert out == [2.0]


# ---------------------------------------------------------------------------
# Pipeline test with a fake nordpool sensor
# ---------------------------------------------------------------------------

def _raw_curve(base_day, values):
    """Build hour-shaped raw entries (>45 min) at hourly steps from base_day midnight."""
    entries = []
    for i, v in enumerate(values):
        start = base_day + timedelta(hours=i)
        entries.append({
            'start': start.isoformat(),
            'end': (start + timedelta(hours=1)).isoformat(),
            'value': v,
        })
    return entries


def test_pipeline_writes_zones_and_thresholds(spot, world, monkeypatch):
    w = world(spot)
    monkeypatch.setattr(spot, "_get_long_term_prices", lambda: [])

    with freeze_time("2026-07-03 00:00:00"):
        base = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        # 4 hourly prices -> normalized into 16 x 15-min entries
        raw_today = _raw_curve(base, [2.0, 4.0, 6.0, 8.0])
        fake_sensor = types.SimpleNamespace(
            raw_today=raw_today,
            raw_tomorrow=[],
            tomorrow_valid=False,
            current_price=8.0,
        )
        monkeypatch.setattr(
            spot, "sensor",
            types.SimpleNamespace(nordpool_kwh_fi_eur_3_10_0=fake_sensor),
        )
        spot.updateSpotPriceSensors()

    # prices = [2,4,6,8] each repeated 4x -> avg 5, min 2, max 8
    avg, mn, mx = 5.0, 2.0, 8.0
    cheap = (avg + mn) / 2   # 3.5
    expensive = (avg + mx) / 2  # 6.5

    writes = w.input_number.writes
    attrs = w.input_number.attr_writes

    # current_price 8.0 >= expensive 6.5 -> raw zone 3
    assert writes["spot_price_cost_heating"] == 3
    assert writes["spot_price_cost_hot_water"] == 3

    assert attrs["spot_price_cost_heating.threshold_cheap"] == cheap
    assert attrs["spot_price_cost_heating.threshold_avg"] == avg
    assert attrs["spot_price_cost_heating.threshold_expensive"] == expensive
    assert attrs["spot_price_cost_hot_water.threshold_cheap"] == cheap
    assert attrs["spot_price_cost_hot_water.threshold_expensive"] == expensive

    # Legacy combined: current 8.0 >= avg_short 5.0 -> cost 2; >= avg_long 5.0 -> +1 = 3
    assert writes["spot_price_cost"] == 3


def test_pipeline_long_thresholds_from_historical_list(spot, world, monkeypatch):
    """L10: heating (long) thresholds come from _get_long_term_prices(), NOT short-term.

    Stub the long-term list to values DISJOINT from the short-term curve so that if
    the code ever fell back to short-term data (empty-list branch), these assertions
    would fail. Long list [10,20,30] -> avg 20, min 10, max 30 ->
    cheap (avg+min)/2 = 15, expensive (avg+max)/2 = 25. Short-term curve stays
    [2,4,6,8] -> avg 5, cheap 3.5, expensive 6.5.
    """
    w = world(spot)
    # Entries mirror _get_long_term_prices() output shape: {'start', 'value'}.
    long_list = [{'start': None, 'value': v} for v in (10.0, 20.0, 30.0)]
    monkeypatch.setattr(spot, "_get_long_term_prices", lambda: long_list)

    with freeze_time("2026-07-03 00:00:00"):
        base = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        raw_today = _raw_curve(base, [2.0, 4.0, 6.0, 8.0])
        fake_sensor = types.SimpleNamespace(
            raw_today=raw_today,
            raw_tomorrow=[],
            tomorrow_valid=False,
            current_price=8.0,
        )
        monkeypatch.setattr(
            spot, "sensor",
            types.SimpleNamespace(nordpool_kwh_fi_eur_3_10_0=fake_sensor),
        )
        spot.updateSpotPriceSensors()

    long_avg, long_cheap, long_expensive = 20.0, 15.0, 25.0
    short_avg, short_cheap, short_expensive = 5.0, 3.5, 6.5

    attrs = w.input_number.attr_writes

    # Heating indicator carries the LONG thresholds (from the historical list).
    assert attrs["spot_price_cost_heating.threshold_avg"] == long_avg
    assert attrs["spot_price_cost_heating.threshold_cheap"] == long_cheap
    assert attrs["spot_price_cost_heating.threshold_expensive"] == long_expensive

    # Legacy combined entity mirrors the long stats (price_25/75_percent_long).
    assert attrs["spot_price_cost.price_average_long"] == long_avg
    assert attrs["spot_price_cost.price_25_percent_long"] == long_cheap
    assert attrs["spot_price_cost.price_75_percent_long"] == long_expensive

    # Sanity: the long values are NOT the short-term values (guards against a
    # silent fallback-to-short-term regression giving a coincidental pass).
    assert long_avg != short_avg
    assert long_cheap != short_cheap
    assert long_expensive != short_expensive

    # Hot water indicator stays SHORT-term based (unchanged by the long list).
    assert attrs["spot_price_cost_hot_water.threshold_avg"] == short_avg
    assert attrs["spot_price_cost_hot_water.threshold_cheap"] == short_cheap
    assert attrs["spot_price_cost_hot_water.threshold_expensive"] == short_expensive


# ---------------------------------------------------------------------------
# L2 same-day long-term cache
# ---------------------------------------------------------------------------

def test_long_term_prices_cached_same_day(spot, world, monkeypatch, long_term_cache_reset):
    """Two same-day _get_long_term_prices() calls issue only ONE recorder query."""
    world(spot)  # install recording fakes so log.warning etc. don't hit _Noop
    calls = {"n": 0}

    def _counting_stat(*a, **k):
        calls["n"] += 1
        sensor_id = "sensor.nordpool_kwh_fi_eur_3_10_0"
        return {sensor_id: [{"start": None, "state": "5.0"}, {"start": None, "state": "7.0"}]}

    monkeypatch.setattr(spot, "_get_statistic", _counting_stat)

    with freeze_time("2026-07-03 10:00:00"):
        first = spot._get_long_term_prices()
        second = spot._get_long_term_prices()

    assert first == second
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# M13 fallback regression tests
# ---------------------------------------------------------------------------

def _assert_fallback(w):
    writes = w.input_number.writes
    assert writes["spot_price_cost_heating"] == 3
    assert writes["spot_price_cost_hot_water"] == 3
    assert writes["spot_price_cost"] == 3
    assert w.input_number.attr_writes == {}
    assert any("unavailable" in msg for level, msg in w.log.records)


def test_fallback_missing_raw_today(spot, world, monkeypatch):
    w = world(spot)
    fake_sensor = types.SimpleNamespace(current_price=1.0)  # no raw_today attr
    monkeypatch.setattr(
        spot, "sensor",
        types.SimpleNamespace(nordpool_kwh_fi_eur_3_10_0=fake_sensor),
    )
    spot.updateSpotPriceSensors()
    _assert_fallback(w)


def test_fallback_empty_raw_today(spot, world, monkeypatch):
    w = world(spot)
    fake_sensor = types.SimpleNamespace(
        raw_today=[], raw_tomorrow=[], tomorrow_valid=False, current_price=1.0,
    )
    monkeypatch.setattr(
        spot, "sensor",
        types.SimpleNamespace(nordpool_kwh_fi_eur_3_10_0=fake_sensor),
    )
    spot.updateSpotPriceSensors()
    _assert_fallback(w)


def test_fallback_current_price_none(spot, world, monkeypatch):
    w = world(spot)
    monkeypatch.setattr(spot, "_get_long_term_prices", lambda: [])
    with freeze_time("2026-07-03 00:00:00"):
        base = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        raw_today = _raw_curve(base, [2.0, 4.0, 6.0, 8.0])
        fake_sensor = types.SimpleNamespace(
            raw_today=raw_today,
            raw_tomorrow=[],
            tomorrow_valid=False,
            current_price=None,
        )
        monkeypatch.setattr(
            spot, "sensor",
            types.SimpleNamespace(nordpool_kwh_fi_eur_3_10_0=fake_sensor),
        )
        spot.updateSpotPriceSensors()
    _assert_fallback(w)
