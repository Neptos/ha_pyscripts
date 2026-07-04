"""Unit tests for the pure pricing functions in UpdateSpotPriceSensors.py.

All access via the `spot` fixture module (see tests/conftest.py).
"""

from datetime import datetime, timedelta, timezone


def _iso(dt):
    return datetime.fromisoformat(dt.isoformat()).isoformat()


def test_normalize_splits_hourly(spot):
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    entry = {'start': _iso(start), 'end': _iso(end), 'value': 4.2}

    out = spot._normalize_price_data([entry])

    assert len(out) == 4
    for e in out:
        assert e['value'] == 4.2
        # Output entries carry datetime start/end, not iso strings
        assert isinstance(e['start'], datetime)
        assert isinstance(e['end'], datetime)
    # 4th entry starts at :45
    assert out[3]['start'].minute == 45


def test_normalize_keeps_15min(spot):
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=15)
    entry = {'start': _iso(start), 'end': _iso(end), 'value': 2.0}

    out = spot._normalize_price_data([entry])

    # Every entry is rebuilt with datetime start/end (canonical semantics),
    # so it is a fresh dict equal by value, not the same object.
    assert len(out) == 1
    assert out[0]['value'] == 2.0
    assert out[0]['start'] == start
    assert out[0]['end'] == end


def test_normalize_drops_malformed(spot):
    # missing 'end' and 'value' -> dropped with a warning
    entry = {'start': 'whatever'}
    out = spot._normalize_price_data([entry])
    assert out == []


def test_cost_for_price_buckets(spot):
    assert spot._calculate_cost_for_price(1, 2, 4, 6) == 0
    assert spot._calculate_cost_for_price(3, 2, 4, 6) == 1
    assert spot._calculate_cost_for_price(5, 2, 4, 6) == 2
    assert spot._calculate_cost_for_price(9, 2, 4, 6) == 3


def test_smoothed_no_zone_change(spot):
    # price 1.0 -> zone 0, equals current_zone 0 -> no change
    assert spot._calculate_smoothed_cost(1.0, 0, (2, 4, 6), []) == (0, 0, 1.0)


def test_smoothed_sustained_change(spot):
    # price 9.0 -> zone 3, all future confirm -> change to 3
    assert spot._calculate_smoothed_cost(9.0, 0, (2, 4, 6), [9, 9, 9, 9]) == (3, 3, 1.0)


def test_smoothed_not_sustained(spot):
    # price 9.0 -> zone 3, only 1/4 future agree (0.25 < 0.75) -> stay in current zone 0
    assert spot._calculate_smoothed_cost(9.0, 0, (2, 4, 6), [1, 1, 1, 9]) == (0, 3, 0.25)
