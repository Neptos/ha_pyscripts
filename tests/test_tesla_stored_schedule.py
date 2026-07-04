"""Round-trip + malformed tests for the Tesla stored-schedule read path.

Pins the contract between the write side (``_store_schedule`` /
``_calculate_and_store_schedule``) and the read side (``_get_stored_schedule`` +
``_is_current_time_in_scheduled_slot``): the JSON slot key names (``start`` /
``end`` / ``solar_energy`` ...), the ISO timestamp format, and the
Z-replacement parse at ``TeslaSmartCharging.py:1657-1660`` that
``tesla_charging_control`` (``:1970-1974``) depends on. If a future edit renames
a slot key or changes the timestamp encoding, these round-trip assertions break.

Conventions (KD-10): local tz derived from the frozen clock, never a hard-coded
offset. No generator expressions (repo rule).
"""

import datetime

from freezegun import freeze_time


def _local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def _price_entries(base_dt, prices):
    """Consecutive 15-min {start,end,value} isoformat entries (c/kWh)."""
    entries = []
    for i in range(len(prices)):
        start_dt = base_dt + datetime.timedelta(minutes=15 * i)
        end_dt = start_dt + datetime.timedelta(minutes=15)
        entries.append({
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "value": prices[i],
        })
    return entries


def _nordpool_attrs(today, tomorrow=None):
    attrs = {"raw_today": today, "tomorrow_valid": False, "raw_tomorrow": []}
    if tomorrow is not None:
        attrs["raw_tomorrow"] = tomorrow
        attrs["tomorrow_valid"] = True
    return attrs


def _record_schedule(tesla, world):
    """Run the below-MIN mandatory scenario; return the captured schedule_json.

    Primed exactly like ``test_tesla_schedule_flow::test_below_min_soc_schedules_mandatory``.
    """
    tz = _local_tz()
    base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
    prices = [2.0] * 24
    get = {
        tesla.TESLA_BATTERY_LEVEL: "30",
        tesla.TESLA_CHARGE_LIMIT: "80",
        tesla.OUTDOOR_TEMP_SENSOR: "10",
        tesla.OUTPUT_MAX_AVG_PRICE: "0",
        tesla.SELL_PRICE_SENSOR: "2.0",
    }
    attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
    w = world(tesla, get=get, attrs=attrs)
    result = tesla._calculate_and_store_schedule()
    assert result["success"]
    key = tesla.OUTPUT_CHARGING_STATUS + ".schedule_json"
    return w.state.attrs_written[key]


def test_round_trip_in_and_out_of_slot(tesla, world):
    """Write a schedule, read it back, assert slot keys + in/out-of-slot logic."""
    with freeze_time("2026-01-15 22:00:00"):
        schedule_json = _record_schedule(tesla, world)

        # Second world primed with the recorded JSON on the status entity.
        attrs = {tesla.OUTPUT_CHARGING_STATUS: {"schedule_json": schedule_json}}
        world(tesla, attrs=attrs)

        schedule = tesla._get_stored_schedule()
        assert schedule is not None
        assert schedule["slots"]
        first = schedule["slots"][0]
        # Read side parses these keys; pin their presence.
        assert "start" in first
        assert "end" in first
        assert "solar_energy" in first

        # Parse first slot's window from the JSON (ISO strings, read side does the
        # same via datetime.fromisoformat with Z-replacement).
        slot_start = datetime.datetime.fromisoformat(first["start"].replace("Z", "+00:00"))
        slot_end = datetime.datetime.fromisoformat(first["end"].replace("Z", "+00:00"))

    # Re-freeze inside the first slot -> (True, that slot). freezegun interprets
    # a naive datetime as local wall-clock, so feed the slot midpoint stripped of
    # tzinfo (slot times are already local, from the frozen clock above).
    mid = slot_start + (slot_end - slot_start) / 2
    with freeze_time(mid.replace(tzinfo=None)):
        in_slot, current = tesla._is_current_time_in_scheduled_slot(schedule)
        assert in_slot is True
        assert current["start"] == first["start"]

    # Re-freeze outside all slots (well before the first slot) -> (False, None).
    before = slot_start - datetime.timedelta(hours=2)
    with freeze_time(before.replace(tzinfo=None)):
        in_slot, current = tesla._is_current_time_in_scheduled_slot(schedule)
        assert in_slot is False
        assert current is None


def test_store_sorts_slots_chronologically_across_offsets(tesla, world):
    """Two slots with mixed UTC offsets sort chronologically, not lexically.

    Slot A: 2026-01-15T23:30:00+03:00  (== 20:30 UTC, chronologically FIRST)
    Slot B: 2026-01-15T22:00:00+02:00  (== 20:00 UTC ... wait, choose so that
    the lexicographic order of the ISO strings is the OPPOSITE of chronological).

    Lexicographically "2026-01-15T22:00:00+02:00" < "2026-01-15T23:30:00+03:00",
    but chronologically 22:00+02:00 (20:00Z) is BEFORE 23:30+03:00 (20:30Z), so
    here lexical and chronological happen to agree. To force disagreement we make
    the string that is lexically FIRST be chronologically SECOND.
    """
    # Lexically-first string but chronologically-later instant:
    #   B = "2026-01-15T21:00:00+01:00" -> 20:00 UTC
    #   A = "2026-01-15T22:30:00+03:00" -> 19:30 UTC (earlier!)
    # Lexicographic: "2026-01-15T21:..." < "2026-01-15T22:..." so B sorts first
    # by string, but A is chronologically first.
    slot_a = {
        "start": "2026-01-15T22:30:00+03:00",  # 19:30 UTC — chronologically first
        "end": "2026-01-15T22:45:00+03:00",
        "buy_price": 2.0, "sell_price": 1.0, "effective_price": 2.0,
        "solar_energy": 0.0, "grid_energy": 2.25, "energy": 2.25,
    }
    slot_b = {
        "start": "2026-01-15T21:00:00+01:00",  # 20:00 UTC — chronologically second
        "end": "2026-01-15T21:15:00+01:00",
        "buy_price": 3.0, "sell_price": 1.0, "effective_price": 3.0,
        "solar_energy": 0.0, "grid_energy": 2.25, "energy": 2.25,
    }

    with freeze_time("2026-01-15 18:00:00"):
        w = world(tesla, attrs={})
        # Pass slots in the WRONG (chronologically-reversed) order.
        tesla._store_schedule(
            [
                {"start": tesla_dt(slot_b["start"]), "end": tesla_dt(slot_b["end"]),
                 "buy_price": 3.0, "sell_price": 1.0, "effective_price": 3.0,
                 "solar_energy": 0.0, "grid_energy": 2.25, "energy": 2.25},
                {"start": tesla_dt(slot_a["start"]), "end": tesla_dt(slot_a["end"]),
                 "buy_price": 2.0, "sell_price": 1.0, "effective_price": 2.0,
                 "solar_energy": 0.0, "grid_energy": 2.25, "energy": 2.25},
            ],
            mode="scheduled_optional",
        )

        key = tesla.OUTPUT_CHARGING_STATUS + ".next_slot_start"
        next_start = w.state.attrs_written[key]
        # Chronologically-first slot (A, 19:30 UTC) must be next_slot_start,
        # even though B's ISO string is lexically smaller.
        assert next_start == slot_a["start"]


def tesla_dt(iso):
    """Parse an ISO string to an aware datetime (module-independent helper)."""
    return datetime.datetime.fromisoformat(iso)


def test_malformed_json_returns_none_and_warns(tesla, world):
    """Non-JSON schedule_json -> _get_stored_schedule None + a warning logged."""
    with freeze_time("2026-01-15 22:00:00"):
        attrs = {tesla.OUTPUT_CHARGING_STATUS: {"schedule_json": "{not json"}}
        w = world(tesla, attrs=attrs)

        assert tesla._get_stored_schedule() is None
        warnings = [rec for rec in w.log.records if rec[0] == "warning"]
        assert warnings


def test_missing_key_returns_none_without_warning(tesla, world):
    """Absent schedule_json key -> None, no warning."""
    with freeze_time("2026-01-15 22:00:00"):
        attrs = {tesla.OUTPUT_CHARGING_STATUS: {}}
        w = world(tesla, attrs=attrs)

        assert tesla._get_stored_schedule() is None
        warnings = [rec for rec in w.log.records if rec[0] == "warning"]
        assert warnings == []
