"""Full-flow scenario tests for ``_calculate_and_store_schedule``.

Drives the whole two-pass scheduler end-to-end through the Step-2 recording
fakes (``world`` fixture) with freezegun controlling the module clock. No source
change: freezegun patches the exec-loaded module's ``datetime`` binding (Step 1
registered it in ``sys.modules``); the fakes capture every ``state``/``log``/
``input_text``/``input_number`` touch (Step 2).

Conventions:
- All Tesla prices are c/kWh. Slot energy is uniform
  ``MAX_CHARGE_RATE_KW * SLOT_DURATION_HOURS`` = 2.25 kWh.
- ``_calculate_and_store_schedule`` returns
  ``{success, mandatory_slots, optional_slots, message}`` — there is NO ``mode``
  key. Mode is read from the attribute written by ``_store_schedule``:
  ``world.state.attrs_written["input_number.tesla_charging_status.mode"]``.
- Solar is primed to zero-yield (sun/solar sensors left unset) so
  effective_price == buy_price for clean assertions.
- Sell lookup is intentionally missed (no sell raw_today) so sell price falls to
  the flat ``state.get(SELL_PRICE_SENSOR)`` prime.
- No generator expressions anywhere (repo convention) — list comprehensions only.
"""

import datetime

from freezegun import freeze_time


# --- Module-level helpers ----------------------------------------------------


def _local_tz():
    """Local tz derived from the frozen clock (KD-7 — never hard-code an offset).

    Must be called inside a ``freeze_time`` block so it matches the source's
    ``datetime.now().astimezone()`` reads.
    """
    return datetime.datetime.now().astimezone().tzinfo


def _price_entries(base_dt, prices):
    """Build consecutive 15-min ``{start,end,value}`` isoformat entries.

    Same shape as ``test_tesla_slotbuild.py::_entry``: tz-aware isoformat
    timestamps, 15-min spans that pass through ``_normalize_price_data``
    unchanged. ``prices`` is a list of c/kWh values; the i-th entry starts at
    ``base_dt + i*15min``.
    """
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
    """Build the Nordpool ``getattr`` payload the source reads.

    Source reads ``raw_today``, ``raw_tomorrow`` and ``tomorrow_valid``.
    """
    attrs = {"raw_today": today, "tomorrow_valid": False, "raw_tomorrow": []}
    if tomorrow is not None:
        attrs["raw_tomorrow"] = tomorrow
        attrs["tomorrow_valid"] = True
    return attrs


# Shared constant references (read from the module inside each test via fixture).
_MODE_KEY = "input_number.tesla_charging_status.mode"


def _slot_energy(tesla):
    return tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS


def _mode_written(world):
    return world.state.attrs_written.get(_MODE_KEY)


def _weighted_avg(slots):
    total_cost = sum([s["effective_price"] * s["energy"] for s in slots])
    total_energy = sum([s["energy"] for s in slots])
    return total_cost / total_energy if total_energy > 0 else 0.0


# --- Scenario 1 --------------------------------------------------------------


def test_below_min_soc_schedules_mandatory(tesla, world):
    """SOC below MIN guarantee -> mandatory slots before deadline are scheduled."""
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        deadline = tesla._get_next_deadline()
        # Plentiful cheap slots starting at 23:00 (before the 07:00 deadline).
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        prices = [2.0] * 24  # 24 slots = 6 hours, all cheap
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
        assert result["mandatory_slots"]
        assert all([s["start"] < deadline for s in result["mandatory_slots"]])
        mand_energy = sum([s["energy"] for s in result["mandatory_slots"]])
        assert mand_energy >= tesla._calculate_kwh_needed(30, tesla.MIN_SOC_GUARANTEE)
        assert _mode_written(w) in (
            "scheduled_mandatory", "scheduled_mandatory_optional",
        )


# --- Scenario 2 --------------------------------------------------------------


def _run_cold(tesla, world, temp):
    """Run a below-MIN schedule with the given temp prime; return mandatory energy."""
    tz = _local_tz()
    base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
    prices = [2.0] * 24
    get = {
        tesla.TESLA_BATTERY_LEVEL: "30",
        tesla.TESLA_CHARGE_LIMIT: "80",
        tesla.OUTPUT_MAX_AVG_PRICE: "0",
        tesla.SELL_PRICE_SENSOR: "2.0",
    }
    if temp is not None:
        get[tesla.OUTDOOR_TEMP_SENSOR] = temp
    attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
    world(tesla, get=get, attrs=attrs)
    result = tesla._calculate_and_store_schedule()
    return sum([s["energy"] for s in result["mandatory_slots"]])


def test_cold_weather_adds_slots(tesla, world):
    """Extreme cold adds exactly the cold buffer; unavailable temp adds 1 slot."""
    with freeze_time("2026-01-15 22:00:00"):
        slot_energy = _slot_energy(tesla)

        mild = _run_cold(tesla, world, "5")        # >= 0 C -> 0 extra
        extreme = _run_cold(tesla, world, "-15")   # < -10 C -> 2 extra
        # extreme selects >= mild's energy by exactly 2 slots of cold buffer.
        cold_buffer = 2 * slot_energy
        assert extreme - mild == cold_buffer

        # temp unavailable -> temp is None branch -> +1 extra slot.
        unavail = _run_cold(tesla, world, "unavailable")
        assert unavail - mild == 1 * slot_energy


# --- Scenario 3 --------------------------------------------------------------


def test_at_or_above_limit_no_charging(tesla, world):
    """SOC at/above limit -> 'Already at charge limit', empty slots, mode complete."""
    with freeze_time("2026-01-15 22:00:00"):
        get = {
            tesla.TESLA_BATTERY_LEVEL: "85",
            tesla.TESLA_CHARGE_LIMIT: "80",
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        w = world(tesla, get=get, attrs={})

        result = tesla._calculate_and_store_schedule()

        assert result["message"] == "Already at charge limit"
        assert result["mandatory_slots"] == []
        assert result["optional_slots"] == []
        assert _mode_written(w) == "complete"


# --- Scenario 4 --------------------------------------------------------------


def test_no_slots_before_deadline(tesla, world):
    """All surviving slots are post-deadline -> 'before deadline' guard fires.

    Freeze at 06:59, SOC below MIN. Supply ONLY slots whose end > 06:59 AND
    start >= 07:00 so the slot-builder keeps them (all_slots non-empty, bypassing
    the "No price data available" guard) while none is before the deadline
    (slots_before_deadline == []). Slots ending <= now would be dropped by the
    builder's `if end <= now: continue`, which would silently test the WRONG
    early return.
    """
    with freeze_time("2026-01-16 06:59:00"):
        tz = _local_tz()
        deadline = tesla._get_next_deadline()  # today 07:00
        # First slot starts at 07:00 (== deadline, so start < deadline is False),
        # ends at 07:15 (> now). Every slot is at/after the deadline.
        base = datetime.datetime(2026, 1, 16, 7, 0, 0, tzinfo=tz)
        prices = [2.0] * 8
        get = {
            tesla.TESLA_BATTERY_LEVEL: "30",
            tesla.TESLA_CHARGE_LIMIT: "80",
            tesla.OUTDOOR_TEMP_SENSOR: "10",
            tesla.OUTPUT_MAX_AVG_PRICE: "0",
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        world(tesla, get=get, attrs=attrs)

        # Sanity: the first supplied slot starts exactly at the deadline.
        assert base >= deadline

        result = tesla._calculate_and_store_schedule()

        assert result["success"] is False
        assert "before deadline" in result["message"]


# --- Scenario 5 --------------------------------------------------------------


def test_price_ceiling_drops_optional(tesla, world):
    """Optional-only schedule with a low ceiling drops expensive slots."""
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        # SOC 55 >= MIN (50) -> no mandatory. Optional spans cheap -> expensive.
        prices = [1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 40.0]
        ceiling = 3.0
        get = {
            tesla.TESLA_BATTERY_LEVEL: "55",
            tesla.TESLA_CHARGE_LIMIT: "90",
            tesla.OUTPUT_MAX_AVG_PRICE: str(ceiling),
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        w = world(tesla, get=get, attrs=attrs)

        result = tesla._calculate_and_store_schedule()

        assert result["success"]
        assert result["mandatory_slots"] == []
        kept = result["optional_slots"]
        assert kept  # at least one optional kept
        assert _weighted_avg(kept) <= ceiling + 1e-9
        # At least one expensive slot dropped: kept count < supplied count.
        assert len(kept) < len(prices)
        assert _mode_written(w) == "scheduled_optional"


# --- Scenario 6 --------------------------------------------------------------


def test_mandatory_plus_optional_mix(tesla, world):
    """SOC below MIN with favorable prices and high limit -> both passes populate."""
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        prices = [2.0] * 28  # 7 hours of cheap slots, spanning past deadline
        get = {
            tesla.TESLA_BATTERY_LEVEL: "40",
            tesla.TESLA_CHARGE_LIMIT: "90",
            tesla.OUTDOOR_TEMP_SENSOR: "10",
            tesla.OUTPUT_MAX_AVG_PRICE: "0",
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        w = world(tesla, get=get, attrs=attrs)

        result = tesla._calculate_and_store_schedule()

        assert result["success"]
        # Uniform 2.0 pricing + SOC 40 (below MIN 50) forces >= 4 mandatory slots,
        # and the high 90% limit leaves room for optional fill; consolidation can
        # reorder but not empty either set, so >= 1 each is the true invariant.
        assert len(result["mandatory_slots"]) >= 1
        assert len(result["optional_slots"]) >= 1
        assert _mode_written(w) == "scheduled_mandatory_optional"

        # soc_after_mandatory invariant: total energy targets charge_limit's kWh
        # equivalent, within one slot-energy.
        soc = 40.0
        limit = 90.0
        total_energy = sum([s["energy"] for s in result["mandatory_slots"] + result["optional_slots"]])
        expected_soc = min(round(soc + total_energy * tesla.CHARGING_EFFICIENCY / tesla.BATTERY_CAPACITY_KWH * 100), 100)
        # expected_soc lands within one slot's SOC-delta of the charge limit.
        slot_soc_delta = _slot_energy(tesla) * tesla.CHARGING_EFFICIENCY / tesla.BATTERY_CAPACITY_KWH * 100
        assert abs(expected_soc - limit) <= slot_soc_delta + 0.5


# --- Scenario 7 --------------------------------------------------------------


def test_insufficient_slots_for_mandatory(tesla, world):
    """Fewer cheap slots than mandatory need -> success True, warning logged."""
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        # SOC 20 -> needs ~13.3 kWh (6 slots) but only 2 slots supplied.
        prices = [2.0, 2.0]
        get = {
            tesla.TESLA_BATTERY_LEVEL: "20",
            tesla.TESLA_CHARGE_LIMIT: "80",
            tesla.OUTDOOR_TEMP_SENSOR: "10",
            tesla.OUTPUT_MAX_AVG_PRICE: "0",
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        w = world(tesla, get=get, attrs=attrs)

        result = tesla._calculate_and_store_schedule()

        assert result["success"] is True
        warnings = [msg for (lvl, msg) in w.log.records if "Insufficient slots" in str(msg)]
        assert warnings


# --- Scenario 8 --------------------------------------------------------------


def test_stored_attributes_correct(tesla, world):
    """Stored attributes match the emitted schedule and recorder fakes fire."""
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        prices = [2.0] * 24
        soc = 40.0
        limit = 80.0
        get = {
            tesla.TESLA_BATTERY_LEVEL: str(soc),
            tesla.TESLA_CHARGE_LIMIT: str(limit),
            tesla.OUTDOOR_TEMP_SENSOR: "10",
            tesla.OUTPUT_MAX_AVG_PRICE: "0",
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        w = world(tesla, get=get, attrs=attrs)

        result = tesla._calculate_and_store_schedule()
        assert result["success"]

        selected = result["mandatory_slots"] + result["optional_slots"]
        assert selected

        aw = w.state.attrs_written
        mode = _mode_written(w)
        # Mode recorded matches the emitted-mode logic for a below-MIN + optional run.
        assert mode == "scheduled_mandatory_optional"

        # avg_price_c_kwh == round(energy-weighted avg, 1). Under uniform 2.0
        # pricing the weighted avg is exactly 2.0, so exact equality carries no
        # float-rounding risk here (expected_avg still recomputed from result).
        expected_avg = round(_weighted_avg(selected), 1)
        assert aw["input_number.tesla_charging_status.avg_price_c_kwh"] == expected_avg

        # expected_soc formula.
        total_energy = sum([s["energy"] for s in selected])
        expected_soc = min(
            round(soc + total_energy * tesla.CHARGING_EFFICIENCY / tesla.BATTERY_CAPACITY_KWH * 100),
            100,
        )
        assert aw["input_number.tesla_charging_status.expected_soc"] == expected_soc

        # slot_count matches selected total.
        assert aw["input_number.tesla_charging_status.slot_count"] == len(selected)

        # input_text summary recorded.
        assert "tesla_charging_schedule" in w.input_text.writes
        assert isinstance(w.input_text.writes["tesla_charging_schedule"], str)

        # input_number status code recorded (FakeInputNumber recorder).
        assert "tesla_charging_status" in w.input_number.writes
        assert isinstance(w.input_number.writes["tesla_charging_status"], int)


# --- Scenario 9: charge-limit-unavailable fallback ---------------------------


def test_charge_limit_unavailable_falls_back_to_mandatory(tesla, world):
    """SOC known but charge limit unset -> mandatory-only schedule, not abort.

    Before the fallback, a missing charge limit returned {'success': False}. Now
    it falls back to MIN_SOC_GUARANTEE so the mandatory 50% guarantee is still
    scheduled (mode scheduled_mandatory).
    """
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        prices = [2.0] * 24
        get = {
            tesla.TESLA_BATTERY_LEVEL: "30",
            # TESLA_CHARGE_LIMIT intentionally unset -> _get_charge_limit None.
            tesla.OUTDOOR_TEMP_SENSOR: "10",
            tesla.OUTPUT_MAX_AVG_PRICE: "0",
            tesla.SELL_PRICE_SENSOR: "2.0",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        w = world(tesla, get=get, attrs=attrs)

        result = tesla._calculate_and_store_schedule()

        assert result["success"] is True
        assert result["mandatory_slots"]
        assert result["optional_slots"] == []
        assert _mode_written(w) == "scheduled_mandatory"


# --- L6 flap regression ------------------------------------------------------


def test_controller_writes_status_at_most_once(tesla, world):
    """L6: a full tesla_charging_control tick writes tesla_charging_status <= 1x.

    Before the fix, the per-tick _calculate_and_store_schedule() wrote status 1
    (Scheduled) and then the controller overwrote it (2 or 0), producing a
    visible 2->1->2 flap. With update_status=False on the internal recalc, only
    the controller's final _update_charging_status writes the status entity.
    """
    with freeze_time("2026-01-15 22:00:00"):
        tz = _local_tz()
        base = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=tz)
        prices = [2.0] * 24
        get = {
            tesla.OUTPUT_SMART_CHARGING_ENABLED: "on",
            tesla.TESLA_LOCATION: "home",
            tesla.TESLA_CHARGE_CABLE: "on",
            tesla.TESLA_CHARGING_STATE: "stopped",
            tesla.TESLA_BATTERY_LEVEL: "30",
            tesla.TESLA_CHARGE_LIMIT: "80",
            tesla.OUTDOOR_TEMP_SENSOR: "10",
            tesla.OUTPUT_MAX_AVG_PRICE: "0",
            tesla.SELL_PRICE_SENSOR: "2.0",
            tesla.TESLA_CHARGE_CURRENT: "0",
            tesla.GRID_POWER_15MIN_AVG: "-500",
            tesla.GRID_POWER_CURRENT: "-500",
            tesla.SUN_NEXT_RISING: "2026-01-16T06:00:00+02:00",
            tesla.SUN_NEXT_SETTING: "2026-01-16T16:00:00+02:00",
        }
        attrs = {tesla.NORDPOOL_SENSOR: _nordpool_attrs(_price_entries(base, prices))}
        w = world(tesla, get=get, attrs=attrs)

        tesla.tesla_charging_control()

        status_writes = [
            e for e in w.input_number.write_log
            if e[0].endswith("tesla_charging_status")
        ]
        assert len(status_writes) <= 1


# --- Micro-coverage: _get_next_deadline --------------------------------------


def test_next_deadline_today_vs_tomorrow(tesla):
    """Before 07:00 -> today's 07:00; at/after 07:00 -> tomorrow's 07:00.

    Clock-only (no ``world`` fixture). Dates are derived from the frozen clock
    (KD-7) — no hard-coded tz offset. Confirms the else-branch return.
    """
    with freeze_time("2026-01-15 06:00:00"):
        now = datetime.datetime.now().astimezone()
        deadline = tesla._get_next_deadline()
        assert deadline.hour == tesla.CHARGE_DEADLINE_HOUR
        assert deadline.date() == now.date()

    with freeze_time("2026-01-15 08:00:00"):
        now = datetime.datetime.now().astimezone()
        deadline = tesla._get_next_deadline()
        assert deadline.hour == tesla.CHARGE_DEADLINE_HOUR
        assert deadline.date() == now.date() + datetime.timedelta(days=1)
