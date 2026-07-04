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
        # Output entries carry datetime start/end, not iso strings
        assert isinstance(e['start'], datetime)
        assert isinstance(e['end'], datetime)
        assert (e['end'] - e['start']).total_seconds() == 15 * 60


def test_normalize_keeps_15min_asis(tesla):
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=15)
    entry = {'start': _iso(start), 'end': _iso(end), 'value': 3.0}

    out = tesla._normalize_price_data([entry])

    # Every entry is rebuilt with datetime start/end (canonical semantics),
    # so it is a fresh dict equal by value, not the same object.
    assert len(out) == 1
    assert out[0]['value'] == 3.0
    assert out[0]['start'] == start
    assert out[0]['end'] == end


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


def test_find_price_at_slot_datetime_entries(tesla):
    """_find_price_at_slot handles tz-aware and naive datetime entry starts.

    Covers the datetime branch (has timestamp attr) and the naive .astimezone()
    fallback at the source's entry-start normalization.
    """
    slot_start = datetime(2026, 7, 1, 10, 15, tzinfo=timezone.utc)

    # tz-aware datetime entry for the same instant.
    aware = [
        {'start': datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), 'value': 1.0},
        {'start': slot_start, 'value': 7.25},
    ]
    assert tesla._find_price_at_slot(aware, slot_start) == pytest.approx(7.25)

    # Naive datetime entry: interpreted as local via .astimezone(). Build a naive
    # datetime that represents the same instant as slot_start in local time.
    local_naive = slot_start.astimezone().replace(tzinfo=None)
    naive = [{'start': local_naive, 'value': 9.0}]
    assert tesla._find_price_at_slot(naive, slot_start) == pytest.approx(9.0)


def test_slotbuild_sell_lookup_representation_independent(tesla):
    """M17: sell entries as UTC-'Z' strings, buy as local +HH:MM strings.

    Same instants, different string representations. The epoch-second keying
    means built slots carry the time-varying sell price, not the flat/50%
    fallback. Before the keying fix (isoformat string keys) the two
    representations never matched and every slot fell to the fallback.
    """
    # Two consecutive 15-min slots. Buy side uses +03:00 local strings.
    buy_starts = [
        datetime(2026, 7, 1, 9, 0, tzinfo=timezone(timedelta(hours=3))),
        datetime(2026, 7, 1, 9, 15, tzinfo=timezone(timedelta(hours=3))),
    ]
    now = datetime(2026, 7, 1, 8, 0, tzinfo=timezone(timedelta(hours=3)))

    raw_today = []
    for s in buy_starts:
        e = s + timedelta(minutes=15)
        raw_today.append({'start': s.isoformat(), 'end': e.isoformat(), 'value': 20.0})

    # Sell side: SAME instants but expressed as UTC with a trailing 'Z'.
    sell_today = []
    for i, s in enumerate(buy_starts):
        s_utc = s.astimezone(timezone.utc)
        e_utc = s_utc + timedelta(minutes=15)
        # Format with 'Z' suffix instead of +00:00.
        sell_today.append({
            'start': s_utc.isoformat().replace('+00:00', 'Z'),
            'end': e_utc.isoformat().replace('+00:00', 'Z'),
            'value': 3.0 + i,  # time-varying: 3.0, 4.0
        })

    nordpool_attrs = {'raw_today': raw_today, 'tomorrow_valid': False}
    sell_attrs = {'raw_today': sell_today}
    expected_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS

    slots = tesla._build_slot_list_with_effective_prices(
        now=now,
        nordpool_attrs=nordpool_attrs,
        sell_attrs=sell_attrs,
        effective_price_fn=lambda s, b, se: (b, 0.0, expected_energy),
    )

    assert len(slots) == 2
    slots.sort(key=lambda s: s['start'])
    # Time-varying sell price carried through (not flat / 50% of 20 == 10).
    assert slots[0]['sell_price'] == pytest.approx(3.0)
    assert slots[1]['sell_price'] == pytest.approx(4.0)


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


def test_blended_negative_buy_price_still_blends(tesla):
    """L11: negative buy price returns the blend, not the 999 sentinel.

    Only buy_price None short-circuits to 999 now; a negative (e.g. curtailment)
    buy price is a real, usable price and must flow through the blend.
    """
    result = tesla._calculate_blended_effective_price(2000, -1.0, 1.0)
    assert result != 999.0
    assert result < 0.5


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
