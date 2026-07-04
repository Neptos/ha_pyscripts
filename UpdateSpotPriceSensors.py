from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timedelta, timezone
import asyncio

# Lookahead smoothing configuration
LOOKAHEAD_INTERVALS_HEATING = 16  # 4 hours for heating
LOOKAHEAD_INTERVALS_HOT_WATER = 4  # 1 hour for hot water
LOOKAHEAD_MAJORITY_THRESHOLD = 0.75
MIN_LOOKAHEAD_INTERVALS = 2  # Minimum 30 min of lookahead required
FALLBACK_COST_VALUE = 3  # Assume expensive when unavailable

def _get_statistic(
    start_time,
    end_time,
    statistic_ids,
    period,
    types):

    counter = 0
    task = get_instance(hass).async_add_executor_job(statistics_during_period, hass, start_time, end_time, statistic_ids, period, None, types)
    while not task.done():
        asyncio.sleep(1)
        counter = counter + 1
        if counter > 10:
            break
    if not task.done():
        log.warning("Recorder statistics query timed out")
        return None
    return task.result()


# Same-day cache for the 10-day long-term price statistics. The query's end_date
# is midnight-truncated, so all same-day results are identical: query once per day.
# A failed (empty) result is NOT cached, so a recovered recorder is picked up.
_long_term_cache = {'day': None, 'prices': None}


def _get_long_term_prices():

    today = datetime.now().date()
    if _long_term_cache['day'] == today and _long_term_cache['prices'] is not None:
        return _long_term_cache['prices']

    start_date = datetime.now() - timedelta(days=10)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    end_date = datetime.now()
    end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

    # Buy price sensor
    sensor = 'sensor.nordpool_kwh_fi_eur_3_10_0'

    stats = _get_statistic(start_date, end_date, [sensor], "hour", ['state'])
    rows = (stats or {}).get(sensor) or []
    stat = [{'start': d.get('start'), 'value': float(d.get('state'))} for d in rows if d.get('state') is not None]

    if not stat:
        log.warning("Long-term price statistics unavailable or empty")
        return []

    _long_term_cache['day'] = today
    _long_term_cache['prices'] = stat
    return stat


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


def _calculate_cost_for_price(price, threshold_cheap, threshold_avg, threshold_expensive):
    """Calculate 0-3 cost for a given price and thresholds."""
    if price < threshold_cheap:
        return 0
    elif price < threshold_avg:
        return 1
    elif price < threshold_expensive:
        return 2
    else:
        return 3


def _get_future_prices(normalized_today, normalized_tomorrow, tomorrow_valid, max_intervals):
    """Get list of upcoming 15-min prices from now.

    Args:
        normalized_today: Normalized price list for today
        normalized_tomorrow: Normalized price list for tomorrow
        tomorrow_valid: Whether tomorrow's prices are available
        max_intervals: Maximum number of intervals to return

    Returns:
        List of price values for upcoming intervals
    """
    now = datetime.now().astimezone()
    future_prices = []

    # Combine today and tomorrow (if available)
    all_prices = list(normalized_today)
    if tomorrow_valid and normalized_tomorrow:
        all_prices.extend(normalized_tomorrow)

    for entry in all_prices:
        if 'start' not in entry or 'value' not in entry:
            continue

        start = entry['start']
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace('Z', '+00:00'))

        # Check if this interval is in the future
        if start >= now:
            future_prices.append(entry['value'])
            if len(future_prices) >= max_intervals:
                break

    return future_prices


def _calculate_smoothed_cost(price_current, current_zone, thresholds, future_prices):
    """Only change zone if threshold crossing is sustained.

    Args:
        price_current: Current electricity price
        current_zone: Current displayed cost zone (0-3)
        thresholds: Tuple of (threshold_cheap, threshold_avg, threshold_expensive)
        future_prices: List of future prices for lookahead

    Returns:
        Tuple of (smoothed_zone, raw_zone, agreement_ratio)
    """
    raw_zone = _calculate_cost_for_price(price_current, *thresholds)

    # If no zone change, keep current
    if raw_zone == current_zone:
        return current_zone, raw_zone, 1.0

    # If insufficient lookahead data, use raw zone
    if len(future_prices) < MIN_LOOKAHEAD_INTERVALS:
        return raw_zone, raw_zone, 1.0

    # Zone change detected - check if sustained
    future_zones = [_calculate_cost_for_price(p, *thresholds) for p in future_prices]
    matching_count = 0
    for z in future_zones:
        if z == raw_zone:
            matching_count = matching_count + 1
    agreement = matching_count / len(future_zones)

    if agreement >= LOOKAHEAD_MAJORITY_THRESHOLD:
        return raw_zone, raw_zone, agreement  # Change zone
    else:
        return current_zone, raw_zone, agreement  # Stay in current zone


def _push_fallback_indicators(reason):
    """Push conservative (expensive) cost indicators when Nordpool data is unavailable.

    Writes FALLBACK_COST_VALUE as the value of the three cost input_numbers.
    Deliberately writes no attributes: no fresh thresholds/prices exist, and
    stale attributes from the last successful run stay visible for diagnosis.
    """
    log.warning(f"Nordpool data unavailable ({reason}) - pushing fallback cost {FALLBACK_COST_VALUE} (expensive)")
    input_number.spot_price_cost_heating = FALLBACK_COST_VALUE
    input_number.spot_price_cost_hot_water = FALLBACK_COST_VALUE
    input_number.spot_price_cost = FALLBACK_COST_VALUE


@service
def spotPriceSensorsTestService(action=None, id=None):
    """Service to execute code through HA"""
    log.warning(f"Manually triggering test service")
    calculateSpotPriceAverages()
    updateSpotPriceSensors()

@time_trigger("cron(1 * * * *)")
def calculateSpotPriceAverages():
    """Calculates monthly and yearly spot price averages"""

    buy_price_entity_id = 'sensor.nordpool_kwh_fi_eur_3_10_0'

    now = datetime.now()
    monthly_start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        monthly_end_date = now.replace(year=now.year+1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        monthly_end_date = now.replace(month=now.month+1, day=1, hour=0, minute=0, second=0, microsecond=0)

    monthly_raw = _get_statistic(monthly_start_date, monthly_end_date, [buy_price_entity_id], "hour", ['state'])
    monthly_rows = (monthly_raw or {}).get(buy_price_entity_id) or []
    monthly_prices = [float(d.get('state')) for d in monthly_rows if d.get('state') is not None]
    if not monthly_prices:
        log.warning("Monthly spot price statistics unavailable or empty - skipping monthly average")
    else:
        input_number.electricity_buy_price_monthly_average = sum(monthly_prices) / len(monthly_prices)

    yearly_start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    yearly_end_date = now.replace(year=now.year+1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    # Daily-mean period: <=365 rows and a mean-of-daily-means is fine for an indicator.
    yearly_raw = _get_statistic(yearly_start_date, yearly_end_date, [buy_price_entity_id], "day", ['state'])
    yearly_rows = (yearly_raw or {}).get(buy_price_entity_id) or []
    yearly_prices = [float(d.get('state')) for d in yearly_rows if d.get('state') is not None]
    if not yearly_prices:
        log.warning("Yearly spot price statistics unavailable or empty - skipping yearly average")
    else:
        input_number.electricity_buy_price_yearly_average = sum(yearly_prices) / len(yearly_prices)


@time_trigger("cron(2,17,32,47 * * * *)")
def updateSpotPriceSensors():
    """Update spot price sensors based on future spot prices"""

    # Spot price sensor. pyscript raises NameError when the entity is entirely
    # absent from the state machine (integration failed setup / not yet loaded),
    # AttributeError when the entity exists but raw_today is missing. Both are
    # Nordpool-side outages -> fail toward "expensive".
    try:
        spot_price_sensor = sensor.nordpool_kwh_fi_eur_3_10_0
        # Normalize to handle mixed hourly/15-min format (once, reused below).
        normalized_today = _normalize_price_data(spot_price_sensor.raw_today)
    except (AttributeError, NameError):
        _push_fallback_indicators("no raw_today")
        return

    if not normalized_today:
        _push_fallback_indicators("no raw_today")
        return

    tomorrow_valid = spot_price_sensor.tomorrow_valid
    normalized_tomorrow = []
    if tomorrow_valid:
        normalized_tomorrow = _normalize_price_data(spot_price_sensor.raw_tomorrow)

    # Note: the "25/75_percent" values below are (avg+min)/2 and (avg+max)/2
    # midpoints, NOT statistical percentiles.
    prices_today = [d['value'] for d in normalized_today if 'value' in d]
    price_average_today = sum(prices_today) / len(prices_today)
    price_min_today = min(prices_today)
    price_max_today = max(prices_today)
    price_25_percent_today = (price_average_today + price_min_today) / 2
    price_75_percent_today = (price_average_today + price_max_today) / 2

    # Short-term = today + tomorrow (when valid).
    short_prices = [d['value'] for d in (normalized_today + normalized_tomorrow) if 'value' in d]
    price_average_short = sum(short_prices) / len(short_prices)
    price_min_short = min(short_prices)
    price_max_short = max(short_prices)
    price_25_percent_short = (price_average_short + price_min_short) / 2
    price_75_percent_short = (price_average_short + price_max_short) / 2

    # Long-term = 10-day historical hourly average ONLY (not blended with the
    # short-term data). If the recorder is down (_get_long_term_prices returns
    # []), fall back to the short-term thresholds so a recorder-only outage still
    # produces a meaningful zone (this is NOT a Nordpool outage -> no fallback push).
    long_prices = [d['value'] for d in _get_long_term_prices() if 'value' in d]
    if long_prices:
        price_average_long = sum(long_prices) / len(long_prices)
        price_min_long = min(long_prices)
        price_max_long = max(long_prices)
    else:
        log.warning("Long-term prices empty - using short-term thresholds for long indicator")
        price_average_long = price_average_short
        price_min_long = price_min_short
        price_max_long = price_max_short
    price_25_percent_long = (price_average_long + price_min_long) / 2
    price_75_percent_long = (price_average_long + price_max_long) / 2

    try:
        price_current = spot_price_sensor.current_price
    except (AttributeError, NameError):
        _push_fallback_indicators("no current_price")
        return
    if price_current is None:
        _push_fallback_indicators("no current_price")
        return

    # Get future prices for lookahead. Compute once at the heating horizon and
    # slice for the shorter hot-water horizon (heating >= hot water).
    future_prices_heating = _get_future_prices(
        normalized_today, normalized_tomorrow,
        tomorrow_valid, LOOKAHEAD_INTERVALS_HEATING
    )
    future_prices_hot_water = future_prices_heating[:LOOKAHEAD_INTERVALS_HOT_WATER]

    # --- HEATING INDICATOR (long-term based, 4-hour smoothing) ---
    threshold_cheap_long = price_25_percent_long
    threshold_expensive_long = price_75_percent_long
    thresholds_long = (threshold_cheap_long, price_average_long, threshold_expensive_long)

    # Get current zone from existing input_number (persists across runs)
    try:
        current_zone_heating = int(float(state.get('input_number.spot_price_cost_heating')))
    except:
        current_zone_heating = FALLBACK_COST_VALUE  # Default to expensive on first run

    # Calculate with threshold-crossing smoothing
    smoothed_zone_heating, raw_zone_heating, agreement_heating = _calculate_smoothed_cost(
        price_current, current_zone_heating, thresholds_long, future_prices_heating
    )

    # Store heating indicator
    input_number.spot_price_cost_heating = smoothed_zone_heating
    input_number.spot_price_cost_heating.threshold_cheap = threshold_cheap_long
    input_number.spot_price_cost_heating.threshold_avg = price_average_long
    input_number.spot_price_cost_heating.threshold_expensive = threshold_expensive_long
    input_number.spot_price_cost_heating.raw_cost = raw_zone_heating
    input_number.spot_price_cost_heating.lookahead_agreement = agreement_heating
    input_number.spot_price_cost_heating.price_current = price_current

    # --- HOT WATER INDICATOR (short-term based, 1-hour smoothing) ---
    threshold_cheap_short = price_25_percent_short
    threshold_expensive_short = price_75_percent_short
    thresholds_short = (threshold_cheap_short, price_average_short, threshold_expensive_short)

    # Get current zone
    try:
        current_zone_hw = int(float(state.get('input_number.spot_price_cost_hot_water')))
    except:
        current_zone_hw = FALLBACK_COST_VALUE

    # Calculate with 1-hour threshold-crossing smoothing
    smoothed_zone_hw, raw_zone_hw, agreement_hw = _calculate_smoothed_cost(
        price_current, current_zone_hw, thresholds_short, future_prices_hot_water
    )

    # Store hot water indicator
    input_number.spot_price_cost_hot_water = smoothed_zone_hw
    input_number.spot_price_cost_hot_water.threshold_cheap = threshold_cheap_short
    input_number.spot_price_cost_hot_water.threshold_avg = price_average_short
    input_number.spot_price_cost_hot_water.threshold_expensive = threshold_expensive_short
    input_number.spot_price_cost_hot_water.raw_cost = raw_zone_hw
    input_number.spot_price_cost_hot_water.lookahead_agreement = agreement_hw
    input_number.spot_price_cost_hot_water.price_current = price_current

    # --- BACKWARD COMPATIBILITY: Keep old combined indicator ---
    input_number.spot_price_cost.price_current = price_current
    input_number.spot_price_cost.price_average_short = price_average_short
    input_number.spot_price_cost.price_min_short = price_min_short
    input_number.spot_price_cost.price_max_short = price_max_short
    input_number.spot_price_cost.price_25_percent_short = price_25_percent_short
    input_number.spot_price_cost.price_75_percent_short = price_75_percent_short

    input_number.spot_price_cost.price_average_long = price_average_long
    input_number.spot_price_cost.price_min_long = price_min_long
    input_number.spot_price_cost.price_max_long = price_max_long
    input_number.spot_price_cost.price_25_percent_long = price_25_percent_long
    input_number.spot_price_cost.price_75_percent_long = price_75_percent_long

    input_number.spot_price_cost.price_average_today = price_average_today
    input_number.spot_price_cost.price_min_today = price_min_today
    input_number.spot_price_cost.price_max_today = price_max_today
    input_number.spot_price_cost.price_25_percent_today = price_25_percent_today
    input_number.spot_price_cost.price_75_percent_today = price_75_percent_today

    # Old combined cost calculation (kept for backward compatibility)
    cost_value = 0
    cost_value_addition = 0

    if price_current < price_25_percent_short:
        cost_value = 0
    elif price_current < price_average_short:
        cost_value = 1
    else:
        cost_value = 2

    if price_current < price_average_long:
        cost_value_addition = 0
    else:
        cost_value_addition = 1

    input_number.spot_price_cost = cost_value + cost_value_addition
