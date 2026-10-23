from datetime import datetime, timedelta, timezone
import json

# Temperature thresholds
BT7_SAFETY_LOW = 40.0
BT7_SAFETY_HIGH = 45.0
BT7_MORNING_TARGET = 50.0
BT7_LOSS_RATE = 1.5            # °C per hour cooling rate
HEATING_RATE_PER_15MIN = 1.25  # °C per 15-min heating interval (net, after losses)
MORNING_GUARANTEE_MAX_INTERVALS = 8  # 2 hours max (8 x 15min)
DAILY_BLOCK_INTERVALS = 12     # 3 hours (12 x 15min)
PLAN_WINDOW_HOURS = 24
SOLAR_POWER_THRESHOLD = 3000   # Watts


def _parse_dt(value):
    """Parse a datetime value from string, timestamp, or pass through datetime."""
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return value


def _normalize_price_data(price_dictionaries):
    """Normalize price data to 15-minute intervals with datetime objects.

    Handles mixed format where some entries may span a full hour (due to timezone
    differences) by splitting hourly entries into 4 equal 15-minute prices.

    Entries missing start/end/value, or whose start/end cannot be parsed, are
    dropped with a warning. Output entries always carry datetime start/end; the
    value passes through untouched.

    Canonical shared helper: this function's source text is kept byte-identical
    across the deployed scripts and is enforced by tests/test_shared_helper_drift.py.
    """
    normalized = []

    for entry in price_dictionaries:
        if 'start' not in entry or 'end' not in entry or 'value' not in entry:
            log.warning(f"Skipping malformed price entry: {entry}")
            continue

        try:
            start = _parse_dt(entry['start'])
            end = _parse_dt(entry['end'])
            duration_minutes = (end - start).total_seconds() / 60
        except (ValueError, TypeError, AttributeError):
            log.warning(f"Skipping malformed price entry: {entry}")
            continue

        if duration_minutes > 45:
            for i in range(4):
                normalized.append({
                    'start': start + timedelta(minutes=15 * i),
                    'end': start + timedelta(minutes=15 * (i + 1)),
                    'value': entry['value']
                })
        else:
            normalized.append({
                'start': start,
                'end': end,
                'value': entry['value']
            })

    return normalized


def _load_price_intervals():
    """Read nordpool prices as normalized 15-min intervals.

    Returns (intervals, source_string).
    """
    spot = sensor.nordpool_kwh_fi_eur_3_10_0

    raw_today = spot.raw_today
    if not raw_today:
        return [], "no_data"

    intervals = _normalize_price_data(raw_today)
    source = "today_only"

    if spot.tomorrow_valid:
        raw_tomorrow = spot.raw_tomorrow
        if raw_tomorrow:
            intervals += _normalize_price_data(raw_tomorrow)
            source = "today_tomorrow"

    return intervals, source


def _build_hourly_price_pool(intervals):
    """Build pool of current and future hours with averaged prices.

    Returns a list of {hour_start, hour_end, price} dicts.
    """
    # Group 15-min intervals by hour
    hourly = {}
    for entry in intervals:
        hour_key = entry['start'].replace(minute=0, second=0, microsecond=0)
        if hour_key not in hourly:
            hourly[hour_key] = []
        hourly[hour_key].append(entry['value'])

    # Average prices per hour, filter out elapsed hours
    now = datetime.now().astimezone()
    current_hour = now.replace(minute=0, second=0, microsecond=0)

    pool = []
    for hour_start in sorted(hourly.keys()):
        if hour_start < current_hour:
            continue
        prices = hourly[hour_start]
        avg_price = sum(prices) / len(prices)
        pool.append({
            'hour_start': hour_start,
            'hour_end': hour_start + timedelta(hours=1),
            'price': avg_price
        })

    return pool


def _select_cheapest_block(intervals, window_start, window_end):
    """Cheapest run of DAILY_BLOCK_INTERVALS consecutive intervals starting in the window.

    Returns {start, end, price} with price as the block's average, or None.
    """
    candidates = [iv for iv in intervals if window_start <= iv['start'] < window_end]
    candidates.sort(key=lambda x: x['start'])

    best_block = None
    best_cost = float('inf')
    for i in range(len(candidates) - DAILY_BLOCK_INTERVALS + 1):
        block = candidates[i:i + DAILY_BLOCK_INTERVALS]

        is_consecutive = True
        for j in range(1, len(block)):
            if block[j]['start'] != block[j - 1]['end']:
                is_consecutive = False
                break

        if is_consecutive:
            cost = sum([iv['value'] for iv in block])
            if cost < best_cost:
                best_cost = cost
                best_block = block

    if best_block is None:
        return None
    return {
        'start': best_block[0]['start'],
        'end': best_block[-1]['end'],
        'price': best_cost / DAILY_BLOCK_INTERVALS,
    }


def _plan_daily_block(prev_plan, intervals, now):
    """Return the daily heating block plan, replanning only when due.

    A plan is made for the newest price day, normally when tomorrow's prices
    are published, and covers the next PLAN_WINDOW_HOURS. It is then kept,
    even after its block has run, until a newer price day is available, so
    the block cannot drift onto pricier hours as cheap ones pass. New prices
    never replace a block that has not finished; if that block runs past
    midnight, the newest price day is today's and the replan still happens.
    """
    if not intervals:
        return prev_plan

    newest_date = max([iv['start'] for iv in intervals]).astimezone().date().isoformat()
    if prev_plan is not None and (
            prev_plan['planned_for'] >= newest_date or prev_plan['end'] > now):
        return prev_plan

    interval_start = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    window_end = now + timedelta(hours=PLAN_WINDOW_HOURS)
    block = _select_cheapest_block(intervals, interval_start, window_end)
    if block is None:
        return prev_plan

    block['planned_for'] = newest_date
    block['planned_at'] = now
    return block


def _plan_attributes(plan):
    """Serialize a plan into status helper attributes."""
    return {
        'schedule_slots': json.dumps([{
            'start': plan['start'].isoformat(),
            'end': plan['end'].isoformat(),
            'price': round(plan['price'], 4),
        }]),
        'plan_for_date': plan['planned_for'],
        'plan_created': plan['planned_at'].isoformat(),
    }


def _load_stored_plan(attrs):
    """Parse a plan stored by _plan_attributes, or None if absent or invalid."""
    try:
        slot = json.loads(attrs['schedule_slots'])[0]
        return {
            'start': datetime.fromisoformat(slot['start']),
            'end': datetime.fromisoformat(slot['end']),
            'price': slot['price'],
            'planned_for': attrs['plan_for_date'],
            'planned_at': datetime.fromisoformat(attrs['plan_created']),
        }
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _evaluate_morning_guarantee(bt7, block, pool):
    """Evaluate whether morning guarantee heating is needed and active now.

    Only active between 18:00 and 06:00. Projects BT7 at 06:00 using cooling
    rate and schedules cheapest consecutive heating block if projected temp
    is below target. The daily block's remaining heating before 06:00 is
    credited, and the guarantee never schedules inside it.

    Returns (is_active, slot_info_string).
    """
    now = datetime.now().astimezone()
    current_hour = now.hour

    # Only active 18:00-06:00
    if 6 <= current_hour < 18:
        return False, "not_active_hours"

    # Calculate hours until 06:00
    if current_hour >= 18:
        hours_until_morning = (24 - current_hour) + 6
    else:
        hours_until_morning = 6 - current_hour

    # Morning deadline
    morning_target_time = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if current_hour >= 18:
        morning_target_time += timedelta(days=1)

    # Account for heating from the daily block that falls before 06:00
    heating_from_block = 0
    if block:
        overlap_start = max(block['start'], now)
        overlap_end = min(block['end'], morning_target_time)
        if overlap_end > overlap_start:
            overlap_intervals = (overlap_end - overlap_start).total_seconds() / 900
            heating_from_block = overlap_intervals * HEATING_RATE_PER_15MIN

    # Project BT7 at 06:00 (cooling minus expected heating from the daily block)
    projected_bt7 = bt7 - (BT7_LOSS_RATE * hours_until_morning) + heating_from_block

    if projected_bt7 >= BT7_MORNING_TARGET:
        return False, "not_needed"

    # Calculate needed heating intervals (capped at max)
    deficit = BT7_MORNING_TARGET - projected_bt7
    needed_intervals = min(
        int(deficit / HEATING_RATE_PER_15MIN) + 1,
        MORNING_GUARANTEE_MAX_INTERVALS
    )

    # Build available overnight 15-min intervals outside the daily block
    overnight_intervals = []
    for hour_slot in pool:
        if hour_slot['hour_end'] > morning_target_time:
            continue
        for i in range(4):
            iv_start = hour_slot['hour_start'] + timedelta(minutes=15 * i)
            iv_end = hour_slot['hour_start'] + timedelta(minutes=15 * (i + 1))
            if block and iv_start < block['end'] and iv_end > block['start']:
                continue
            if iv_end > now:
                overnight_intervals.append({
                    'start': iv_start,
                    'end': iv_end,
                    'price': hour_slot['price']
                })

    if len(overnight_intervals) < needed_intervals:
        return False, "insufficient_intervals"

    # Find cheapest consecutive block of needed length
    best_block = None
    best_cost = float('inf')

    for i in range(len(overnight_intervals) - needed_intervals + 1):
        block = overnight_intervals[i:i + needed_intervals]

        # Verify consecutive (each starts where previous ends)
        is_consecutive = True
        for j in range(1, len(block)):
            if block[j]['start'] != block[j - 1]['end']:
                is_consecutive = False
                break

        if is_consecutive:
            cost = sum([iv['price'] for iv in block])
            if cost < best_cost:
                best_cost = cost
                best_block = block

    if best_block is None:
        return False, "no_consecutive_block"

    block_start = best_block[0]['start']
    block_end = best_block[-1]['end']
    slot_info = f"{block_start.isoformat()} - {block_end.isoformat()}"

    if block_start <= now < block_end:
        return True, slot_info

    return False, slot_info


def _check_solar_override():
    """Check if solar production justifies allowing heating.

    Returns True if inverter > 3kW AND Tesla wall connector not charging.
    """
    try:
        inverter_power = float(state.get('sensor.inverter_average_active_power'))
        tesla_charging = state.get('binary_sensor.tesla_wall_connector_contactor_closed') == 'on'
        return inverter_power > SOLAR_POWER_THRESHOLD and not tesla_charging
    except:
        return False


def _check_temperature_safety(bt7, cost_zone, current_status):
    """Temperature safety check with hysteresis.

    Only active in cost zones 0-2. Below 40°C: heat. Above 45°C: stop.
    Between 40-45°C: maintain current state (hysteresis).

    Returns (should_heat, reason) or (None, None) if not applicable.
    """
    if cost_zone > 2:
        return None, None

    if bt7 < BT7_SAFETY_LOW:
        return True, f"temp_safety_below_{int(BT7_SAFETY_LOW)}"
    elif bt7 > BT7_SAFETY_HIGH:
        return False, f"temp_safety_above_{int(BT7_SAFETY_HIGH)}"
    else:
        # Hysteresis: maintain current heating state
        if current_status > 0.5:
            return True, "temp_safety_hysteresis_heating"
        return None, None


def _make_heating_decision():
    """Evaluate decision layers in priority order (first match wins).

    Layers:
    1. Manual override
    2. Solar override
    3. Cheapest 3 hours
    4. Morning guarantee
    5. Temperature safety
    6. Default block

    Returns (status, reason, debug_data).
    """
    debug = {
        'schedule_source': 'not_evaluated',
        'next_cheap_start': 'not_evaluated',
        'morning_guarantee_slot': 'not_evaluated',
    }
    now = datetime.now().astimezone()

    # Read BT7 temperature
    try:
        bt7 = float(state.get('sensor.nibe_varmvatten_topp_bt7'))
        debug['bt7_temperature'] = bt7
    except:
        debug['bt7_temperature'] = 'unavailable'
        debug['error'] = 'BT7 sensor unavailable'
        return 0, "bt7_unavailable", debug

    # Read BT6 temperature (informational)
    try:
        bt6 = float(state.get('sensor.nibe_varmvatten_laddning_bt6'))
        debug['bt6_temperature'] = bt6
    except:
        debug['bt6_temperature'] = 'unavailable'

    # Read cost zone
    try:
        cost_zone = int(float(state.get('input_number.spot_price_cost_hot_water')))
        debug['cost_zone'] = cost_zone
    except:
        cost_zone = 3
        debug['cost_zone'] = 'unavailable_default_3'

    # Current status for hysteresis
    try:
        current_status = float(state.get('input_number.hot_water_heating_status'))
    except:
        current_status = 0

    # --- Layer 1: Manual override ---
    if state.get('input_boolean.heat_offset_manual_override') == 'on':
        return 1, "manual_override", debug

    # --- Layer 2: Solar override ---
    if _check_solar_override():
        return 1, "solar_override", debug

    # --- Layer 3: Daily cheapest 3h block ---
    plan = None
    pool = []
    try:
        intervals, source = _load_price_intervals()
        debug['schedule_source'] = source
        pool = _build_hourly_price_pool(intervals)

        prev_plan = _load_stored_plan(state.getattr('input_number.hot_water_heating_status') or {})
        plan = _plan_daily_block(prev_plan, intervals, now)
        if plan:
            debug.update(_plan_attributes(plan))
            debug['next_cheap_start'] = plan['start'].isoformat() if plan['end'] > now else 'none_remaining'

            if plan['start'] <= now < plan['end']:
                return 1, "cheapest_3h", debug
    except Exception as e:
        debug['schedule_error'] = str(e)
        debug['schedule_source'] = 'error'

    # --- Layer 4: Morning guarantee ---
    try:
        mg_active, mg_info = _evaluate_morning_guarantee(bt7, plan, pool)
        debug['morning_guarantee_slot'] = mg_info
        if mg_active:
            return 1, "morning_guarantee", debug
    except Exception as e:
        debug['morning_guarantee_slot'] = f"error: {e}"

    # --- Layer 5: Temperature safety ---
    safety_result, safety_reason = _check_temperature_safety(bt7, cost_zone, current_status)
    if safety_result is True:
        return 1, safety_reason, debug

    # --- Layer 6: Default block ---
    return 0, "default_block", debug


@state_trigger("sensor.nibe_varmvatten_topp_bt7")
def onBt7Change(**kwargs):
    """React to BT7 changes for temperature safety layer only.

    Only acts when the current optimizer reason is a temp_safety* reason,
    so it never interferes with cheapest_3h, morning_guarantee, etc.
    """
    try:
        attrs = state.getattr('input_number.hot_water_heating_status')
        if not attrs:
            return
        current_reason = attrs.get('reason', '')
        if not current_reason.startswith('temp_safety'):
            return
        current_status = float(state.get('input_number.hot_water_heating_status'))
        if current_status < 0.5:
            return
    except:
        return

    try:
        bt7 = float(state.get('sensor.nibe_varmvatten_topp_bt7'))
    except:
        return

    if bt7 > BT7_SAFETY_HIGH:
        now = datetime.now().astimezone()
        input_number.hot_water_heating_status = 0
        input_number.hot_water_heating_status.reason = "default_block"
        input_number.hot_water_heating_status.last_calculated = now.isoformat()
        input_number.hot_water_heating_status.last_decision_change = now.isoformat()
        state.setattr('input_number.hot_water_heating_status.bt7_temperature', bt7)


@service
def hotWaterOptimizerTestService(action=None, id=None):
    """Service to manually trigger hot water optimizer."""
    log.warning("Manually triggering hot water optimizer test service")
    updateHotWaterHeatingStatus()


@time_trigger("cron(3,18,33,48 * * * *)")
def updateHotWaterHeatingStatus():
    """Update hot water heating status based on decision layers.

    Writes decision to input_number.hot_water_heating_status (0=block, 1=allow)
    with debug attributes.
    """
    now = datetime.now().astimezone()

    status, reason, debug = _make_heating_decision()

    # Read previous state for stability rule and change tracking
    try:
        current_status = float(state.get('input_number.hot_water_heating_status'))
        attrs = state.getattr('input_number.hot_water_heating_status')
        current_reason = attrs.get('reason', '') if attrs else ''
    except:
        current_status = 0
        attrs = None
        current_reason = ''

    # Stability rule: don't drop morning_guarantee mid-block on recalculation
    if (current_status > 0.5
            and current_reason in ('morning_guarantee', 'morning_guarantee_stability')
            and status == 0):
        try:
            prev_mg_slot = attrs.get('morning_guarantee_slot', '') if attrs else ''
            if ' - ' in prev_mg_slot:
                mg_start_str, mg_end_str = prev_mg_slot.split(' - ')
                mg_start = datetime.fromisoformat(mg_start_str.strip())
                mg_end = datetime.fromisoformat(mg_end_str.strip())
                if mg_start <= now < mg_end:
                    status = 1
                    reason = "morning_guarantee_stability"
                    debug['morning_guarantee_slot'] = prev_mg_slot
        except:
            pass

    # Track decision changes
    if int(round(current_status)) != status:
        last_change = now.isoformat()
    else:
        last_change = attrs.get('last_decision_change', now.isoformat()) if attrs else now.isoformat()

    # Write status value + all attributes in one merge call. state.set(name,
    # value, **kwargs) sets the value and MERGES the kwargs into the existing
    # attributes (pyscript semantics), so other attributes on the entity are
    # preserved (never new_attributes= full-replace).
    attrs = {
        'reason': reason,
        'last_calculated': now.isoformat(),
        'last_decision_change': last_change,
        **debug,
    }
    state.set('input_number.hot_water_heating_status', status, **attrs)
