from datetime import datetime, timedelta, timezone
import json

# Temperature thresholds
BT7_SAFETY_LOW = 40.0
BT7_SAFETY_HIGH = 45.0
BT7_MORNING_TARGET = 50.0
BT7_LOSS_RATE = 1.5            # °C per hour cooling rate
HEATING_RATE_PER_15MIN = 1.25  # °C per 15-min heating interval (net, after losses)
MORNING_GUARANTEE_MAX_INTERVALS = 8  # 2 hours max (8 x 15min)
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
    """
    normalized = []

    for entry in price_dictionaries:
        if 'start' not in entry or 'end' not in entry or 'value' not in entry:
            continue

        start = _parse_dt(entry['start'])
        end = _parse_dt(entry['end'])
        duration_minutes = (end - start).total_seconds() / 60

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


def _build_hourly_price_pool():
    """Build pool of available hours with averaged prices from nordpool data.

    Returns (pool, source_string) where pool is a list of
    {hour_start, hour_end, price} dicts for current and future hours.
    """
    spot = sensor.nordpool_kwh_fi_eur_3_10_0

    raw_today = spot.raw_today
    if not raw_today:
        return [], "no_data"

    normalized = _normalize_price_data(raw_today)
    source = "today_only"

    if spot.tomorrow_valid:
        raw_tomorrow = spot.raw_tomorrow
        if raw_tomorrow:
            normalized += _normalize_price_data(raw_tomorrow)
            source = "today_tomorrow"

    # Group 15-min intervals by hour
    hourly = {}
    for entry in normalized:
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

    return pool, source


def _select_cheapest_hours(pool, count=3):
    """Select the cheapest N hours from the pool, sorted chronologically."""
    if not pool:
        return []

    by_price = sorted(pool, key=lambda x: x['price'])
    cheapest = by_price[:count]
    cheapest.sort(key=lambda x: x['hour_start'])
    return cheapest


def _evaluate_morning_guarantee(bt7, cheap_slots, pool):
    """Evaluate whether morning guarantee heating is needed and active now.

    Only active between 18:00 and 06:00. Projects BT7 at 06:00 using cooling
    rate and schedules cheapest consecutive heating block if projected temp
    is below target.

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

    # Project BT7 at 06:00
    projected_bt7 = bt7 - (BT7_LOSS_RATE * hours_until_morning)

    if projected_bt7 >= BT7_MORNING_TARGET:
        return False, "not_needed"

    # Calculate needed heating intervals (capped at max)
    deficit = BT7_MORNING_TARGET - projected_bt7
    needed_intervals = min(
        int(deficit / HEATING_RATE_PER_15MIN) + 1,
        MORNING_GUARANTEE_MAX_INTERVALS
    )

    # Exclude hours already in cheapest-3 schedule
    cheap_hour_starts = set()
    for slot in cheap_slots:
        cheap_hour_starts.add(slot['hour_start'])

    # Morning deadline
    morning_target_time = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if current_hour >= 18:
        morning_target_time += timedelta(days=1)

    # Build available overnight 15-min intervals from non-cheap pool hours
    overnight_intervals = []
    for hour_slot in pool:
        if hour_slot['hour_start'] in cheap_hour_starts:
            continue
        if hour_slot['hour_end'] > morning_target_time:
            continue
        for i in range(4):
            iv_start = hour_slot['hour_start'] + timedelta(minutes=15 * i)
            iv_end = hour_slot['hour_start'] + timedelta(minutes=15 * (i + 1))
            if iv_start >= now:
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
            cost = sum(iv['price'] for iv in block)
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
        'schedule_slots': '[]',
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

    # --- Layer 3: Cheapest 3 hours ---
    cheap_slots = []
    pool = []
    try:
        pool, source = _build_hourly_price_pool()
        debug['schedule_source'] = source

        cheap_slots = _select_cheapest_hours(pool, count=3)
        debug['schedule_slots'] = json.dumps([{
            'start': s['hour_start'].isoformat(),
            'end': s['hour_end'].isoformat(),
            'price': round(s['price'], 4)
        } for s in cheap_slots])

        # Next upcoming cheap slot
        upcoming = [s for s in cheap_slots if s['hour_end'] > now]
        debug['next_cheap_start'] = upcoming[0]['hour_start'].isoformat() if upcoming else 'none_remaining'

        # Check if current hour is in cheapest 3
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        for slot in cheap_slots:
            if slot['hour_start'] <= current_hour_start < slot['hour_end']:
                return 1, "cheapest_3h", debug
    except Exception as e:
        debug['schedule_error'] = str(e)
        debug['schedule_slots'] = '[]'
        debug['schedule_source'] = 'error'

    # --- Layer 4: Morning guarantee ---
    try:
        mg_active, mg_info = _evaluate_morning_guarantee(bt7, cheap_slots, pool)
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

    # Stability rule: don't drop cheapest_3h mid-hour on recalculation
    if (current_status > 0.5
            and current_reason in ('cheapest_3h', 'cheapest_3h_stability')
            and status == 0):
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        try:
            prev_slots_str = attrs.get('schedule_slots', '[]') if attrs else '[]'
            prev_slots = json.loads(prev_slots_str)
            for slot in prev_slots:
                slot_start = datetime.fromisoformat(slot['start'])
                slot_end = datetime.fromisoformat(slot['end'])
                if slot_start <= current_hour_start < slot_end:
                    status = 1
                    reason = "cheapest_3h_stability"
                    # Preserve previous schedule for next stability check
                    debug['schedule_slots'] = prev_slots_str
                    break
        except:
            pass

    # Track decision changes
    if int(round(current_status)) != status:
        last_change = now.isoformat()
    else:
        last_change = attrs.get('last_decision_change', now.isoformat()) if attrs else now.isoformat()

    # Write status
    input_number.hot_water_heating_status = status

    # Write fixed attributes
    input_number.hot_water_heating_status.reason = reason
    input_number.hot_water_heating_status.last_calculated = now.isoformat()
    input_number.hot_water_heating_status.last_decision_change = last_change

    # Write debug attributes
    for key, value in debug.items():
        state.setattr(f'input_number.hot_water_heating_status.{key}', value)
