"""Invariant/property tests for the Tesla slot-build path.

Drives ``_build_slot_list_with_effective_prices`` entirely through injected
params (now / nordpool_attrs / sell_attrs / effective_price_fn), so no live
``state`` reads happen. These assert intent (sort order, uniform energy,
past-slot exclusion), not brittle golden numbers.
"""

import datetime


def _entry(start_dt, value):
    """Build a 15-min raw_today/sell entry with tz-aware isoformat timestamps.

    The source parses `start` via datetime.fromisoformat(...) and keys both the
    sell lookup and the slot lookup off the resulting `.isoformat()`. Using the
    same offset-form isoformat on both the nordpool and sell sides guarantees
    sell_lookup HITS (so the flat-rate `state.get` fallback is NOT exercised).
    15-min entries pass through _normalize_price_data unchanged.
    """
    end_dt = start_dt + datetime.timedelta(minutes=15)
    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "value": value,
    }


def test_slotbuild_injected_deterministic(tesla):
    tz = datetime.timezone(datetime.timedelta(hours=2))  # +02:00 offset form
    now = datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz)

    # One PAST slot (end <= now) that must be dropped, plus four future slots
    # whose values are intentionally out of price order to exercise the sort.
    past = _entry(datetime.datetime(2026, 7, 1, 11, 0, 0, tzinfo=tz), 99.0)
    f1 = _entry(datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz), 5.0)
    f2 = _entry(datetime.datetime(2026, 7, 1, 12, 15, 0, tzinfo=tz), 1.0)
    f3 = _entry(datetime.datetime(2026, 7, 1, 12, 30, 0, tzinfo=tz), 8.0)
    f4 = _entry(datetime.datetime(2026, 7, 1, 12, 45, 0, tzinfo=tz), 3.0)

    raw_today = [past, f1, f2, f3, f4]
    nordpool_attrs = {"raw_today": raw_today, "tomorrow_valid": False}
    # Matching sell entries so sell_lookup HITS on every future slot.
    sell_attrs = {"raw_today": list(raw_today)}

    expected_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS

    # effective_price == buy_price; solar_energy 0; grid_energy == full slot.
    slots = tesla._build_slot_list_with_effective_prices(
        now=now,
        nordpool_attrs=nordpool_attrs,
        sell_attrs=sell_attrs,
        effective_price_fn=lambda s, b, se: (b, 0.0, expected_energy),
    )

    # Past slot dropped -> only the four future slots remain.
    assert len(slots) == 4

    # No slot ends at or before `now`.
    assert all(s["end"] > now for s in slots)

    # Every slot carries uniform full-rate energy.
    assert all(s["energy"] == expected_energy for s in slots)

    # Sorted ascending by effective_price (cheapest first).
    prices = [s["effective_price"] for s in slots]
    assert prices == sorted(prices)
    assert prices == [1.0, 3.0, 5.0, 8.0]

    # sell_lookup HIT: sell_price equals the injected value (not a fallback).
    by_start = {s["start"].isoformat(): s for s in slots}
    for e in (f1, f2, f3, f4):
        matched = by_start[e["start"]]
        assert matched["sell_price"] == e["value"]


def test_slotbuild_empty_when_no_today(tesla):
    tz = datetime.timezone(datetime.timedelta(hours=2))
    now = datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz)

    slots = tesla._build_slot_list_with_effective_prices(
        now=now,
        nordpool_attrs={},
        sell_attrs={},
        effective_price_fn=lambda s, b, se: (b, 0.0, 0.0),
    )
    assert slots == []
