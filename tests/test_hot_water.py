"""Unit tests for HotWaterOptimizer morning-guarantee + stability rules.

Covers the H2 fix: ``_evaluate_morning_guarantee`` must include the currently
-running 15-min interval in ``overnight_intervals`` so a mid-block cron run
(``:03/:18/:33/:48``) activates heating (candidate filter is ``iv_end > now``,
not ``iv_start >= now``).

Conventions (KD-10): time-sensitive tests derive the local tz from the frozen
clock via ``datetime.now().astimezone().tzinfo`` — never a hard-coded UTC
offset. ``_evaluate_morning_guarantee`` is called directly with hand-built pool
dicts ``{hour_start, hour_end, price}``. No generator expressions (repo rule).
"""

import datetime
import json

from freezegun import freeze_time


def _local_tz():
    """Local tz derived from the frozen clock; call inside a ``freeze_time`` block."""
    return datetime.datetime.now().astimezone().tzinfo


def _hour(y, mo, d, h, tz, price):
    """Build one pool entry: {hour_start, hour_end, price}."""
    start = datetime.datetime(y, mo, d, h, 0, 0, tzinfo=tz)
    return {
        "hour_start": start,
        "hour_end": start + datetime.timedelta(hours=1),
        "price": price,
    }


# --- H2 regression: mid-block activation --------------------------------------


def test_morning_guarantee_active_mid_block(hotwater):
    """23:03 local, cheapest overnight hour starts 23:00, bt7 low -> activates.

    Pre-fix (``iv_start >= now``) the 23:00-23:15 interval is dropped so the
    cheapest block cannot start at/before 23:03 and activation fails; post-fix
    (``iv_end > now``) the running interval is kept and the block activates.
    """
    with freeze_time("2026-01-15 23:03:00"):
        tz = _local_tz()
        # Overnight pool: several hours starting at 23:00. Make 23:00 cheapest.
        pool = [
            _hour(2026, 1, 15, 23, tz, 1.0),
            _hour(2026, 1, 16, 0, tz, 5.0),
            _hour(2026, 1, 16, 1, tz, 5.0),
        ]
        # bt7 low enough to need heating (huge deficit -> multiple intervals).
        active, info = hotwater._evaluate_morning_guarantee(30.0, None, pool)

        assert active is True
        assert " - " in info
        start_str, end_str = info.split(" - ")
        block_start = datetime.datetime.fromisoformat(start_str.strip())
        block_end = datetime.datetime.fromisoformat(end_str.strip())
        now = datetime.datetime.now().astimezone()
        assert block_start <= now < block_end


# --- Active-hours boundaries --------------------------------------------------


def test_not_active_before_18(hotwater):
    """17:59 local -> not active hours."""
    with freeze_time("2026-01-15 17:59:00"):
        tz = _local_tz()
        pool = [_hour(2026, 1, 15, 18, tz, 1.0)]
        active, info = hotwater._evaluate_morning_guarantee(30.0, None, pool)
        assert active is False
        assert info == "not_active_hours"


def test_active_at_18_with_deficit_schedules(hotwater):
    """18:00 with a deficit -> a block is scheduled (returns an iso range)."""
    with freeze_time("2026-01-15 18:00:00"):
        tz = _local_tz()
        # Plenty of overnight hours 18:00..05:00 so intervals suffice.
        pool = []
        for h in range(18, 24):
            pool.append(_hour(2026, 1, 15, h, tz, 2.0))
        for h in range(0, 6):
            pool.append(_hour(2026, 1, 16, h, tz, 2.0))
        active, info = hotwater._evaluate_morning_guarantee(30.0, None, pool)
        # Deficit exists; a consecutive block is found. It may or may not be
        # active at 18:00 depending on chosen cheapest block, but info must be
        # an iso range (not a "not_*" sentinel).
        assert " - " in info


def test_cross_midnight_block_never_past_0600(hotwater):
    """03:00 local -> cross-midnight arithmetic; block never extends past 06:00."""
    with freeze_time("2026-01-16 03:00:00"):
        tz = _local_tz()
        # Remaining overnight hours 03:00, 04:00, 05:00 (06:00 excluded: hour_end
        # would exceed morning_target_time).
        pool = [
            _hour(2026, 1, 16, 3, tz, 2.0),
            _hour(2026, 1, 16, 4, tz, 2.0),
            _hour(2026, 1, 16, 5, tz, 2.0),
            _hour(2026, 1, 16, 6, tz, 2.0),  # must be excluded (ends 07:00)
        ]
        active, info = hotwater._evaluate_morning_guarantee(30.0, None, pool)
        assert " - " in info
        _start, end_str = info.split(" - ")
        block_end = datetime.datetime.fromisoformat(end_str.strip())
        morning = datetime.datetime(2026, 1, 16, 6, 0, 0, tzinfo=tz)
        assert block_end <= morning


# --- not_needed ---------------------------------------------------------------


def test_not_needed_when_projected_at_target(hotwater):
    """projected_bt7 >= BT7_MORNING_TARGET -> (False, 'not_needed')."""
    with freeze_time("2026-01-15 23:00:00"):
        tz = _local_tz()
        pool = [_hour(2026, 1, 15, 23, tz, 2.0)]
        # 23:00 -> hours_until_morning = 7. loss = 1.5*7 = 10.5.
        # Need projected = bt7 - 10.5 >= 50 -> bt7 >= 60.5.
        active, info = hotwater._evaluate_morning_guarantee(61.0, None, pool)
        assert active is False
        assert info == "not_needed"


# --- needed_intervals rounding ------------------------------------------------


def test_needed_intervals_rounding(hotwater, monkeypatch):
    """deficit 2.6 C at 1.25 C/interval -> int(2.6/1.25)+1 = 3 intervals.

    Craft bt7 so projected deficit is exactly 2.6 with zero cheap heating and
    a single overnight hour whose 4 intervals give a unique cheapest 3-block.
    """
    with freeze_time("2026-01-15 23:00:00"):
        tz = _local_tz()
        # hours_until_morning = 7 -> loss 10.5. projected = bt7 - 10.5.
        # Want deficit = 50 - projected = 2.6 -> projected = 47.4 -> bt7 = 57.9.
        pool = [
            _hour(2026, 1, 15, 23, tz, 1.0),
            _hour(2026, 1, 16, 0, tz, 5.0),
        ]
        active, info = hotwater._evaluate_morning_guarantee(57.9, None, pool)
        assert " - " in info
        start_str, end_str = info.split(" - ")
        block_start = datetime.datetime.fromisoformat(start_str.strip())
        block_end = datetime.datetime.fromisoformat(end_str.strip())
        # 3 intervals = 45 minutes.
        assert (block_end - block_start) == datetime.timedelta(minutes=45)


def test_needed_intervals_capped(hotwater):
    """Enormous deficit caps needed_intervals at MORNING_GUARANTEE_MAX_INTERVALS."""
    with freeze_time("2026-01-15 23:00:00"):
        tz = _local_tz()
        # Provide many overnight hours so intervals are plentiful.
        pool = []
        for h in range(23, 24):
            pool.append(_hour(2026, 1, 15, h, tz, 2.0))
        for h in range(0, 6):
            pool.append(_hour(2026, 1, 16, h, tz, 2.0))
        # bt7 very low -> deficit >> max.
        active, info = hotwater._evaluate_morning_guarantee(10.0, None, pool)
        assert " - " in info
        start_str, end_str = info.split(" - ")
        block_start = datetime.datetime.fromisoformat(start_str.strip())
        block_end = datetime.datetime.fromisoformat(end_str.strip())
        max_minutes = hotwater.MORNING_GUARANTEE_MAX_INTERVALS * 15
        assert (block_end - block_start) <= datetime.timedelta(minutes=max_minutes)


# --- daily block exclusion ----------------------------------------------------


def test_daily_block_excluded_from_guarantee(hotwater):
    """Intervals inside the daily block never appear in the guarantee block."""
    with freeze_time("2026-01-15 23:00:00"):
        tz = _local_tz()
        cheap_hour = _hour(2026, 1, 15, 23, tz, 0.5)  # cheapest, but in the daily block
        pool = [
            cheap_hour,
            _hour(2026, 1, 16, 0, tz, 2.0),
            _hour(2026, 1, 16, 1, tz, 2.0),
        ]
        block = {"start": cheap_hour["hour_start"], "end": cheap_hour["hour_end"]}
        active, info = hotwater._evaluate_morning_guarantee(40.0, block, pool)
        assert " - " in info
        start_str, _end = info.split(" - ")
        block_start = datetime.datetime.fromisoformat(start_str.strip())
        assert block_start >= datetime.datetime(2026, 1, 16, 0, 0, 0, tzinfo=tz)


def test_daily_block_before_morning_credited(hotwater):
    """A 3h daily block before 06:00 covers the deficit -> guarantee not needed."""
    with freeze_time("2026-01-15 23:00:00"):
        tz = _local_tz()
        pool = [_hour(2026, 1, 15, 23, tz, 2.0)]
        block = {
            "start": datetime.datetime(2026, 1, 16, 2, 0, 0, tzinfo=tz),
            "end": datetime.datetime(2026, 1, 16, 5, 0, 0, tzinfo=tz),
        }
        # 23:00 -> loss 1.5 * 7 = 10.5; block credit 12 * 1.25 = 15; 46 - 10.5 + 15 = 50.5.
        active, info = hotwater._evaluate_morning_guarantee(46.0, block, pool)
        assert active is False
        assert info == "not_needed"


# --- stability rules via updateHotWaterHeatingStatus --------------------------


def test_morning_guarantee_stability(hotwater, world, monkeypatch):
    """Prior reason morning_guarantee + stored slot spanning now -> stability."""
    with freeze_time("2026-01-15 23:30:00"):
        tz = _local_tz()
        mg_start = datetime.datetime(2026, 1, 15, 23, 15, 0, tzinfo=tz)
        mg_end = datetime.datetime(2026, 1, 15, 23, 45, 0, tzinfo=tz)
        mg_slot = f"{mg_start.isoformat()} - {mg_end.isoformat()}"
        attrs = {
            "input_number.hot_water_heating_status": {
                "reason": "morning_guarantee",
                "morning_guarantee_slot": mg_slot,
            }
        }
        get = {"input_number.hot_water_heating_status": "1"}
        w = world(hotwater, get=get, attrs=attrs)
        monkeypatch.setattr(
            hotwater, "_make_heating_decision",
            lambda: (0, "default_block", {
                "schedule_source": "today_tomorrow",
                "schedule_slots": "[]",
                "next_cheap_start": "none",
                "morning_guarantee_slot": "not_evaluated",
            }),
        )

        hotwater.updateHotWaterHeatingStatus()

        # L14: status value + attrs written via one state.set(name, value, **kwargs).
        assert ("input_number.hot_water_heating_status", 1) in w.state.set_calls
        assert w.state.attrs_written[
            "input_number.hot_water_heating_status.reason"
        ] == "morning_guarantee_stability"


# --- daily cheapest 3h block plan ---------------------------------------------


def _intervals(start, prices):
    """Consecutive 15-min price intervals beginning at ``start``."""
    out = []
    for i in range(len(prices)):
        iv_start = start + datetime.timedelta(minutes=15 * i)
        out.append({
            "start": iv_start,
            "end": iv_start + datetime.timedelta(minutes=15),
            "value": prices[i],
        })
    return out


def _plan(start, end, planned_for, planned_at, price=1.0):
    return {
        "start": start,
        "end": end,
        "price": price,
        "planned_for": planned_for,
        "planned_at": planned_at,
    }


def test_new_prices_plan_cheapest_consecutive_3h(hotwater):
    """Tomorrow's prices arrive -> cheapest 12 consecutive intervals in next 24h."""
    with freeze_time("2026-01-15 14:20:00"):
        tz = _local_tz()
        now = datetime.datetime.now().astimezone()
        day_start = datetime.datetime(2026, 1, 15, 0, 0, 0, tzinfo=tz)
        prices = [10.0] * (48 * 4)
        # Cheap 03:00-06:00 tomorrow, plus one lone very cheap interval.
        for i in range(27 * 4, 30 * 4):
            prices[i] = 1.0
        prices[40 * 4] = 0.1
        intervals = _intervals(day_start, prices)

        plan = hotwater._plan_daily_block(None, intervals, now)

        assert plan["start"] == datetime.datetime(2026, 1, 16, 3, 0, 0, tzinfo=tz)
        assert plan["end"] == datetime.datetime(2026, 1, 16, 6, 0, 0, tzinfo=tz)
        assert plan["planned_for"] == "2026-01-16"


def test_plan_window_limited_to_24h(hotwater):
    """A cheaper block more than 24h after planning is out of reach."""
    with freeze_time("2026-01-15 14:20:00"):
        tz = _local_tz()
        now = datetime.datetime.now().astimezone()
        day_start = datetime.datetime(2026, 1, 15, 0, 0, 0, tzinfo=tz)
        prices = [10.0] * (48 * 4)
        for i in range(20 * 4, 23 * 4):     # today 20:00-23:00
            prices[i] = 5.0
        for i in range(44 * 4, 47 * 4):     # tomorrow 20:00-23:00, cheaper
            prices[i] = 1.0
        intervals = _intervals(day_start, prices)

        plan = hotwater._plan_daily_block(None, intervals, now)

        assert plan["start"] == datetime.datetime(2026, 1, 15, 20, 0, 0, tzinfo=tz)


def test_plan_kept_after_block_passed(hotwater):
    """The drift bug: once the block has run, no new block until new prices."""
    with freeze_time("2026-01-16 07:03:00"):
        tz = _local_tz()
        now = datetime.datetime.now().astimezone()
        prev = _plan(
            datetime.datetime(2026, 1, 16, 3, 0, 0, tzinfo=tz),
            datetime.datetime(2026, 1, 16, 6, 0, 0, tzinfo=tz),
            "2026-01-16",
            datetime.datetime(2026, 1, 15, 14, 20, 0, tzinfo=tz),
        )
        intervals = _intervals(
            datetime.datetime(2026, 1, 16, 0, 0, 0, tzinfo=tz), [10.0] * 96)

        plan = hotwater._plan_daily_block(prev, intervals, now)

        assert plan is prev


def test_new_prices_wait_for_running_block(hotwater):
    """New prices mid-block: keep the running block, replan after it ends."""
    with freeze_time("2026-01-16 14:20:00"):
        tz = _local_tz()
        now = datetime.datetime.now().astimezone()
        prev = _plan(
            datetime.datetime(2026, 1, 16, 13, 0, 0, tzinfo=tz),
            datetime.datetime(2026, 1, 16, 16, 0, 0, tzinfo=tz),
            "2026-01-16",
            datetime.datetime(2026, 1, 15, 14, 20, 0, tzinfo=tz),
        )
        intervals = _intervals(
            datetime.datetime(2026, 1, 16, 0, 0, 0, tzinfo=tz), [10.0] * 192)

        assert hotwater._plan_daily_block(prev, intervals, now) is prev

    with freeze_time("2026-01-16 16:03:00"):
        now = datetime.datetime.now().astimezone()
        plan = hotwater._plan_daily_block(prev, intervals, now)
        assert plan["planned_for"] == "2026-01-17"
        assert plan["start"] >= prev["end"]


def test_no_plan_falls_back_to_rest_of_today(hotwater):
    """No stored plan and no tomorrow prices -> plan within remaining today."""
    with freeze_time("2026-01-16 08:10:00"):
        tz = _local_tz()
        now = datetime.datetime.now().astimezone()
        prices = [10.0] * 96
        for i in range(2 * 4, 5 * 4):       # 02:00-05:00, already passed
            prices[i] = 0.5
        for i in range(12 * 4, 15 * 4):     # 12:00-15:00
            prices[i] = 3.0
        intervals = _intervals(
            datetime.datetime(2026, 1, 16, 0, 0, 0, tzinfo=tz), prices)

        plan = hotwater._plan_daily_block(None, intervals, now)

        assert plan["start"] == datetime.datetime(2026, 1, 16, 12, 0, 0, tzinfo=tz)
        assert plan["planned_for"] == "2026-01-16"


def test_block_past_midnight_replans_for_today(hotwater):
    """New prices arrived while the block ran into midnight -> replan after it.

    After midnight only today's prices are published, so the replan must key
    on the newest price day rather than on tomorrow's prices being present.
    """
    with freeze_time("2026-01-17 00:03:00"):
        tz = _local_tz()
        now = datetime.datetime.now().astimezone()
        prev = _plan(
            datetime.datetime(2026, 1, 16, 21, 0, 0, tzinfo=tz),
            datetime.datetime(2026, 1, 17, 0, 0, 0, tzinfo=tz),
            "2026-01-16",
            datetime.datetime(2026, 1, 16, 7, 12, 0, tzinfo=tz),
        )
        prices = [10.0] * 96
        for i in range(2 * 4, 5 * 4):       # 02:00-05:00
            prices[i] = 1.0
        intervals = _intervals(
            datetime.datetime(2026, 1, 17, 0, 0, 0, tzinfo=tz), prices)

        plan = hotwater._plan_daily_block(prev, intervals, now)

        assert plan["planned_for"] == "2026-01-17"
        assert plan["start"] == datetime.datetime(2026, 1, 17, 2, 0, 0, tzinfo=tz)


def test_stored_plan_round_trip(hotwater):
    with freeze_time("2026-01-15 14:20:00"):
        tz = _local_tz()
        plan = _plan(
            datetime.datetime(2026, 1, 16, 3, 0, 0, tzinfo=tz),
            datetime.datetime(2026, 1, 16, 6, 0, 0, tzinfo=tz),
            "2026-01-16",
            datetime.datetime(2026, 1, 15, 14, 20, 0, tzinfo=tz),
        )
        attrs = hotwater._plan_attributes(plan)
        loaded = hotwater._load_stored_plan(attrs)
        assert loaded == plan
        # Attributes from before the daily plan existed carry no plan.
        assert hotwater._load_stored_plan({"schedule_slots": attrs["schedule_slots"]}) is None


# --- solar override on net solar surplus --------------------------------------


def test_solar_override_starts_above_threshold(hotwater):
    assert hotwater._solar_override_active(2000.0, 300.0, False) is True
    assert hotwater._solar_override_active(1999.0, 300.0, False) is False


def test_solar_override_keeps_with_own_draw_added_back(hotwater):
    # Heating took 2 kW of a 2.2 kW surplus: 200 + 2000 >= 1500 keeps it on.
    assert hotwater._solar_override_active(200.0, 2000.0, True) is True


def test_solar_override_stops_when_draining_battery(hotwater):
    # Immersion heater 7 kW on a 3 kW surplus: -4000 + 7000 looks fine, but drains 4 kW.
    assert hotwater._solar_override_active(-4000.0, 7000.0, True) is False


def test_solar_override_off_without_surplus_data(hotwater):
    assert hotwater._solar_override_active(None, 0.0, True) is False
