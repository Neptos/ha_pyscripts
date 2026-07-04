"""Invariant/property tests for the Tesla slot-build path.

Drives ``_build_slot_list_with_effective_prices`` entirely through injected
params (now / nordpool_attrs / sell_attrs / effective_price_fn), so no live
``state`` reads happen. These assert intent (sort order, uniform energy,
past-slot exclusion), not brittle golden numbers.
"""

import datetime

import pytest


def _entry(start_dt, value, as_dt=False):
    """Build a 15-min raw_today/sell entry keyed on ``start_dt``.

    The source parses `start` (string via datetime.fromisoformat, or a datetime
    passed through) and keys both the sell lookup and the slot lookup off the
    resulting epoch-second, so the representation is interchangeable. When
    ``as_dt`` is True the entry carries a datetime start/end (exercising the
    datetime branch of the builder); otherwise offset-form isoformat strings.
    15-min entries are rebuilt by _normalize_price_data with datetime start/end,
    values unchanged.
    """
    end_dt = start_dt + datetime.timedelta(minutes=15)
    if as_dt:
        return {"start": start_dt, "end": end_dt, "value": value}
    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "value": value,
    }


@pytest.mark.parametrize("as_dt", [False, True], ids=["iso-string", "tz-aware-dt"])
def test_slotbuild_injected_deterministic(tesla, as_dt):
    tz = datetime.timezone(datetime.timedelta(hours=2))  # +02:00 offset form
    now = datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz)

    # One PAST slot (end <= now) that must be dropped, plus four future slots
    # whose values are intentionally out of price order to exercise the sort.
    past = _entry(datetime.datetime(2026, 7, 1, 11, 0, 0, tzinfo=tz), 99.0, as_dt)
    f1 = _entry(datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz), 5.0, as_dt)
    f2 = _entry(datetime.datetime(2026, 7, 1, 12, 15, 0, tzinfo=tz), 1.0, as_dt)
    f3 = _entry(datetime.datetime(2026, 7, 1, 12, 30, 0, tzinfo=tz), 8.0, as_dt)
    f4 = _entry(datetime.datetime(2026, 7, 1, 12, 45, 0, tzinfo=tz), 3.0, as_dt)

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
    assert all([s["end"] > now for s in slots])

    # Every slot carries uniform full-rate energy.
    assert all([s["energy"] == expected_energy for s in slots])

    # Sorted ascending by effective_price (cheapest first).
    prices = [s["effective_price"] for s in slots]
    assert prices == sorted(prices)
    assert prices == [1.0, 3.0, 5.0, 8.0]

    # sell_lookup HIT: sell_price equals the injected value (not a fallback).
    by_ts = {int(s["start"].timestamp()): s for s in slots}
    for e in (f1, f2, f3, f4):
        start = e["start"]
        if isinstance(start, str):
            start = datetime.datetime.fromisoformat(start)
        matched = by_ts[int(start.timestamp())]
        assert matched["sell_price"] == e["value"]


def test_slotbuild_naive_datetime_start(tesla):
    """Naive datetime starts exercise the .astimezone() fallback and still key."""
    local_tz = datetime.datetime.now().astimezone().tzinfo
    now = datetime.datetime(2026, 7, 1, 12, 0, 0, tzinfo=local_tz)

    # Naive datetime starts (no tzinfo). Builder assumes local via .astimezone().
    f1 = {
        "start": datetime.datetime(2026, 7, 1, 12, 0, 0),
        "end": datetime.datetime(2026, 7, 1, 12, 15, 0),
        "value": 5.0,
    }
    f2 = {
        "start": datetime.datetime(2026, 7, 1, 12, 15, 0),
        "end": datetime.datetime(2026, 7, 1, 12, 30, 0),
        "value": 3.0,
    }
    raw_today = [f1, f2]
    nordpool_attrs = {"raw_today": raw_today, "tomorrow_valid": False}
    sell_attrs = {"raw_today": list(raw_today)}

    expected_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    slots = tesla._build_slot_list_with_effective_prices(
        now=now,
        nordpool_attrs=nordpool_attrs,
        sell_attrs=sell_attrs,
        effective_price_fn=lambda s, b, se: (b, 0.0, expected_energy),
    )

    assert len(slots) == 2
    # Sell lookup HITS despite naive datetimes on both sides (keyed by epoch sec).
    for s in slots:
        assert s["sell_price"] == s["buy_price"]


def test_one_malformed_entry_degrades_not_aborts(tesla):
    """95 valid entries + 1 with value=None -> 95 slots (not []).

    Before the per-entry try/except, the None value raises TypeError at
    float() and the function-level except returned []. Now the bad entry is
    skipped and the pool shrinks by exactly 1.
    """
    tz = datetime.timezone(datetime.timedelta(hours=2))
    now = datetime.datetime(2026, 7, 1, 0, 0, 0, tzinfo=tz)

    entries = []
    for i in range(95):
        start = now + datetime.timedelta(minutes=15 * i)
        entries.append(_entry(start, 5.0))
    # One malformed entry: value=None -> float(None) raises TypeError.
    bad_start = now + datetime.timedelta(minutes=15 * 95)
    entries.append({
        "start": bad_start.isoformat(),
        "end": (bad_start + datetime.timedelta(minutes=15)).isoformat(),
        "value": None,
    })

    nordpool_attrs = {"raw_today": entries, "tomorrow_valid": False}
    expected_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    slots = tesla._build_slot_list_with_effective_prices(
        now=now,
        nordpool_attrs=nordpool_attrs,
        sell_attrs={},
        effective_price_fn=lambda s, b, se: (b, 0.0, expected_energy),
    )

    assert len(slots) == 95


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
