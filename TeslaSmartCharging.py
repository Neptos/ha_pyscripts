"""Tesla Smart Charging Optimization Script for Home Assistant pyscript.

This pyscript optimizes Tesla charging based on Nordpool spot prices and solar production.
It creates charging schedules to minimize electricity costs while ensuring the vehicle
reaches the desired charge level by the configured deadline.

================================================================================
REQUIRED HOME ASSISTANT HELPERS
================================================================================

Create these helpers in Home Assistant BEFORE deploying this script:

1. input_number.tesla_charging_status
   - Minimum: -1 (for error state)
   - Maximum: 10
   - Step: 1
   - Unit: (none)
   - Status codes:
       0 = Idle/Not charging
       1 = Scheduled charging (waiting for slot)
       2 = Active charging (grid power)
       3 = Active charging (solar surplus)
       4 = Paused (waiting for cheaper rate)
       5 = Complete (target SOC reached)
      -1 = Error/Unavailable

2. input_text.tesla_charging_schedule
   - Maximum length: 255 characters
   - Stores truncated JSON schedule; full schedule in status sensor attributes

3. input_boolean.tesla_smart_charging_enabled
   - Master on/off toggle for smart charging
   - When off, all automation is disabled

================================================================================
KEY FEATURES
================================================================================

1. SCHEDULED CHARGING (Price Optimization)
   - Daily schedule calculation at 15:00 when tomorrow's Nordpool prices available
   - Two-pass greedy algorithm:
     Pass 1 (Mandatory): Select cheapest slots BEFORE deadline to reach MIN_SOC_GUARANTEE
     Pass 2 (Optional): Select cheapest remaining slots to reach charge_limit
   - 15-minute slot granularity matching Nordpool pricing

2. SOLAR OPPORTUNISTIC CHARGING
   - Monitors grid power export during daylight hours
   - Three-tier logic:
     * PURE solar (>=4500W surplus): Charges using 100% solar at calculated amps
     * BLENDED (1500-4500W surplus): Charges at min amps if grid price is cheap (<75% avg)
     * STOP (<1500W surplus): Stops opportunistic charging
   - 5-minute minimum interval between charge state changes prevents rapid cycling
   - Does not interfere with scheduled slots

3. AUTOMATIC REPLANNING
   - Recalculates schedule when car arrives home
   - Recalculates schedule when charging cable is connected
   - Stops Tesla's auto-start charging if outside scheduled slot

================================================================================
GRID POWER CONVENTION
================================================================================

This script follows Home Assistant's standard grid power convention:
  - NEGATIVE values = exporting power to grid (solar surplus)
  - POSITIVE values = importing power from grid (consumption)

Example: grid_power = -2000 means exporting 2kW to the grid

================================================================================
TRIGGER SCHEDULE SUMMARY
================================================================================

Time Triggers:
  - cron(0 15 * * *)     : Daily schedule calculation (15:00)
  - cron(2,17,32,47 * * * *): Schedule execution every 15 minutes

State Triggers:
  - device_tracker.location == 'home' : Car arrives home -> recalculate schedule
  - binary_sensor.charge_cable == 'on': Cable connected -> recalculate schedule
  - sensor.power_meter_active_power   : Grid power change -> solar opportunism

Services:
  - pyscript.calculate_tesla_charging_schedule : Manual schedule calculation
  - pyscript.tesla_smart_charging_test_service : Debug/test service

================================================================================
SENSOR CONFIGURATION
================================================================================

Update the ENTITY IDS section below with your specific Tesla and energy sensor
entity IDs before deployment.

================================================================================
"""

from datetime import datetime, timedelta
import json

# =============================================================================
# CONFIGURATION CONSTANTS
# =============================================================================

# --- Solar Production Curve Distribution ---
# Raw solar production curve - extended to cover hours 5-19 for seasonal variation
# Values represent relative production at each hour (will be normalized to sum to 1.0)
_SOLAR_CURVE_RAW = {
    5: 0.008, 6: 0.020, 7: 0.045, 8: 0.080, 9: 0.105,
    10: 0.120, 11: 0.130, 12: 0.130, 13: 0.120, 14: 0.105,
    15: 0.080, 16: 0.045, 17: 0.020, 18: 0.008, 19: 0.004,
}
# Normalize to sum exactly to 1.0
_SOLAR_CURVE_SUM = sum(_SOLAR_CURVE_RAW.values())
SOLAR_CURVE = {h: v / _SOLAR_CURVE_SUM for h, v in _SOLAR_CURVE_RAW.items()}

# --- Battery and Charging Parameters ---
BATTERY_CAPACITY_KWH = 75  # Tesla battery capacity in kWh
CHARGING_EFFICIENCY = 0.90  # 90% charging efficiency (wall to battery)
MAX_CHARGE_RATE_KW = 9  # Maximum charge rate: 13A x 3-phase x 230V = ~9kW
MIN_CHARGE_AMPS = 6  # Minimum charging amperage (Tesla minimum)
MAX_CHARGE_AMPS = 13  # Maximum charging amperage (circuit limit)
VOLTAGE = 230  # Grid voltage
PHASES = 3  # 3-phase charging

# --- Charging Schedule Parameters ---
MIN_SOC_GUARANTEE = 50  # Minimum SOC (%) guaranteed by deadline
CHARGE_DEADLINE_HOUR = 7  # Deadline hour (7:00 AM)
CHARGE_DEADLINE_MINUTE = 0  # Deadline minute
MIN_CHANGE_INTERVAL_SECONDS = 300  # Minimum 5 minutes between charge adjustments

# --- Solar Charging Parameters ---
# Minimum charge power: 6A * 230V * 3 phases = 4140W
MIN_CHARGE_POWER_W = MIN_CHARGE_AMPS * VOLTAGE * PHASES

# Solar Charging Thresholds
SOLAR_START_THRESHOLD_W = 4500  # Pure solar: surplus covers full minimum charge power (4140W + margin)

# Blended Solar+Grid Charging Parameters
SOLAR_BLENDED_MIN_W = 1500  # Minimum solar surplus for blended solar+grid charging
BLENDED_PRICE_THRESHOLD_FACTOR = 0.75  # 75% of daily average = cheap enough for blended

SOLAR_FORECAST_CONFIDENCE = 0.80  # 80% confidence factor for solar forecasts
BASELOAD_ESTIMATE_KW = 1.0  # Estimated base load of house (kW)

# --- Location/Availability ---
HOME_LOCATION = "home"  # Device tracker state when at home

# =============================================================================
# ENTITY IDS
# =============================================================================

# --- Tesla Entities ---
TESLA_BATTERY_LEVEL = "sensor.battery_level"
TESLA_LOCATION = "device_tracker.location"
TESLA_CHARGE_SWITCH = "switch.charge"
TESLA_CHARGE_CURRENT = "number.charge_current"
TESLA_CHARGING_STATE = "sensor.charging"
TESLA_CHARGE_CABLE = "binary_sensor.charge_cable"
TESLA_CHARGER_POWER = "sensor.charger_power"
TESLA_CHARGE_LIMIT = "number.charge_limit"

# --- Price Entities ---
NORDPOOL_SENSOR = "sensor.nordpool_kwh_fi_eur_3_10_0"
SELL_PRICE_SENSOR = "sensor.electricity_sell_price"

# --- Solar Forecast Entities (Solcast) ---
SOLAR_REMAINING_TODAY = "sensor.energy_production_today_remaining"
SOLAR_CURRENT_HOUR = "sensor.energy_current_hour"
SOLAR_NEXT_HOUR = "sensor.energy_next_hour"
SOLAR_PRODUCTION_TOMORROW = "sensor.energy_production_tomorrow"

# --- Solar Production/Grid Entities ---
SOLAR_POWER_CURRENT = "sensor.inverter_average_active_power"
GRID_POWER_CURRENT = "sensor.power_meter_active_power"

# --- Sun Entities ---
SUN_NEXT_RISING = "sensor.sun_next_rising"
SUN_NEXT_SETTING = "sensor.sun_next_setting"

# --- Output Entities (create as HA helpers) ---
OUTPUT_CHARGING_STATUS = "input_number.tesla_charging_status"
OUTPUT_CHARGING_SCHEDULE = "input_text.tesla_charging_schedule"
OUTPUT_SMART_CHARGING_ENABLED = "input_boolean.tesla_smart_charging_enabled"


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def _normalize_price_data(price_dictionaries):
    """Normalize price data to 15-minute intervals.

    Handles mixed format where some entries may span a full hour (due to timezone
    differences) by splitting hourly entries into 4 equal 15-minute prices.

    Copied from UpdateSpotPriceSensors.py for consistency.
    """
    normalized = []

    for entry in price_dictionaries:
        if 'start' not in entry or 'end' not in entry or 'value' not in entry:
            # Keep entries without proper start/end/value as-is
            normalized.append(entry)
            continue

        # Handle both string and datetime objects
        start = entry['start']
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace('Z', '+00:00'))
        end = entry['end']
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace('Z', '+00:00'))
        duration_minutes = (end - start).total_seconds() / 60

        # If duration is roughly 1 hour (allow small variations), split into 4x15min
        if duration_minutes > 45:
            for i in range(4):
                interval_start = start + timedelta(minutes=15 * i)
                interval_end = start + timedelta(minutes=15 * (i + 1))
                normalized.append({
                    'start': interval_start.isoformat(),
                    'end': interval_end.isoformat(),
                    'value': entry['value']
                })
        else:
            # Already 15-minute (or other) interval, keep as-is
            normalized.append(entry)

    return normalized


def _get_sunrise_sunset():
    """Get sunrise and sunset times from HA sun entities.

    Returns:
        tuple: (sunrise_datetime, sunset_datetime) or (None, None) if unavailable
    """
    try:
        sunrise_str = state.get(SUN_NEXT_RISING)
        sunset_str = state.get(SUN_NEXT_SETTING)

        if sunrise_str is None or sunset_str is None:
            log.warning("Sun sensor states are unavailable")
            return None, None

        # Parse ISO format datetime strings
        sunrise = datetime.fromisoformat(sunrise_str.replace('Z', '+00:00'))
        sunset = datetime.fromisoformat(sunset_str.replace('Z', '+00:00'))

        return sunrise, sunset
    except Exception as e:
        log.warning(f"Error parsing sunrise/sunset times: {e}")
        return None, None


def _is_car_at_home():
    """Check if the Tesla is at home location.

    Returns:
        bool: True if car is at home, False otherwise
    """
    try:
        location = state.get(TESLA_LOCATION)
        return location == HOME_LOCATION
    except Exception as e:
        log.warning(f"Error checking car location: {e}")
        return False


def _is_cable_connected():
    """Check if the charging cable is connected to the car.

    Returns:
        bool: True if cable is connected, False otherwise
    """
    try:
        cable_state = state.get(TESLA_CHARGE_CABLE)
        return cable_state == "on" or cable_state == True
    except Exception as e:
        log.warning(f"Error checking cable connection: {e}")
        return False


def _is_smart_charging_enabled():
    """Check if smart charging is enabled by the user.

    Returns:
        bool: True if smart charging is enabled, False otherwise
    """
    try:
        enabled = state.get(OUTPUT_SMART_CHARGING_ENABLED)
        return enabled == "on" or enabled == True
    except Exception as e:
        log.warning(f"Error checking smart charging enabled state: {e}")
        return False


def _get_current_soc():
    """Get current battery state of charge.

    Returns:
        float: Current SOC percentage or None if unavailable
    """
    try:
        soc = state.get(TESLA_BATTERY_LEVEL)
        if soc is not None:
            return float(soc)
        return None
    except Exception as e:
        log.warning(f"Error getting current SOC: {e}")
        return None


def _get_charge_limit():
    """Get current charge limit setting.

    Returns:
        float: Charge limit percentage or None if unavailable
    """
    try:
        limit = state.get(TESLA_CHARGE_LIMIT)
        if limit is not None:
            return float(limit)
        return None
    except Exception as e:
        log.warning(f"Error getting charge limit: {e}")
        return None


def _get_current_solar_surplus():
    """Calculate current solar surplus available for charging.

    Solar surplus = solar production - base load - grid export
    Positive value means excess solar available for charging.

    Returns:
        float: Solar surplus in Watts (positive = surplus, negative = deficit)
    """
    try:
        solar_power = float(state.get(SOLAR_POWER_CURRENT) or 0)
        grid_power = float(state.get(GRID_POWER_CURRENT) or 0)

        # Grid power is negative when exporting, positive when importing
        # Solar surplus = what we're exporting + baseload estimate
        # If grid_power < 0, we're exporting, surplus = abs(grid_power)
        # If grid_power > 0, we're importing, surplus = 0 (or negative)
        surplus = -grid_power  # Negate because export is negative

        return surplus
    except Exception as e:
        log.warning(f"Error calculating solar surplus: {e}")
        return 0.0


def _get_current_prices():
    """Get current buy and sell prices for the current 15-minute slot.

    Retrieves prices from Nordpool and sell price sensors for the slot that
    contains the current time. Uses raw_today attribute for time-specific
    prices, falling back to current state value if time-varying data unavailable.

    Returns:
        tuple: (buy_price, sell_price) as floats in EUR/kWh, or (None, None) if unavailable
    """
    try:
        # Calculate current 15-minute slot start time
        now = datetime.now().astimezone()
        slot_minute = (now.minute // 15) * 15
        slot_start = now.replace(minute=slot_minute, second=0, microsecond=0)

        # Get Nordpool price data
        nordpool_attrs = state.getattr(NORDPOOL_SENSOR) or {}
        raw_today = nordpool_attrs.get('raw_today', [])

        if not raw_today:
            log.warning("No raw_today price data available for current prices")
            return None, None

        # Normalize price data to 15-minute intervals
        normalized_prices = _normalize_price_data(raw_today)

        # Find buy price for current slot
        buy_price = None
        for entry in normalized_prices:
            if 'start' not in entry or 'value' not in entry:
                continue

            # Parse start time - handle both string and datetime objects
            entry_start = entry['start']
            if isinstance(entry_start, str):
                entry_start = datetime.fromisoformat(entry_start.replace('Z', '+00:00'))
            elif hasattr(entry_start, 'timestamp'):
                # It's already a datetime, ensure timezone-aware
                if entry_start.tzinfo is None:
                    entry_start = entry_start.astimezone()

            # Compare slot starts (both should be timezone-aware now)
            # Convert both to same timezone for comparison
            entry_start_local = entry_start.astimezone(slot_start.tzinfo)

            if (entry_start_local.hour == slot_start.hour and
                entry_start_local.minute == slot_start.minute and
                entry_start_local.date() == slot_start.date()):
                buy_price = float(entry['value'])
                break

        if buy_price is None:
            log.warning(f"Could not find buy price for slot starting at {slot_start}")
            return None, None

        # Get sell price - try time-varying first, fall back to flat rate
        sell_price = None
        sell_attrs = state.getattr(SELL_PRICE_SENSOR) or {}
        sell_raw_today = sell_attrs.get('raw_today', [])

        if sell_raw_today:
            # Try to find time-specific sell price
            sell_normalized = _normalize_price_data(sell_raw_today)
            for entry in sell_normalized:
                if 'start' not in entry or 'value' not in entry:
                    continue

                entry_start = entry['start']
                if isinstance(entry_start, str):
                    entry_start = datetime.fromisoformat(entry_start.replace('Z', '+00:00'))
                elif hasattr(entry_start, 'timestamp'):
                    if entry_start.tzinfo is None:
                        entry_start = entry_start.astimezone()

                entry_start_local = entry_start.astimezone(slot_start.tzinfo)

                if (entry_start_local.hour == slot_start.hour and
                    entry_start_local.minute == slot_start.minute and
                    entry_start_local.date() == slot_start.date()):
                    sell_price = float(entry['value'])
                    break

        # Fall back to current state value if time-varying not found
        if sell_price is None:
            sell_price_state = state.get(SELL_PRICE_SENSOR)
            if sell_price_state is not None:
                try:
                    sell_price = float(sell_price_state)
                except (ValueError, TypeError):
                    log.warning(f"Could not parse sell price state: {sell_price_state}")
                    sell_price = None

        if sell_price is None:
            log.warning("Could not determine sell price, using estimate from buy price")
            # Conservative estimate: sell price typically ~50% of buy price
            sell_price = buy_price * 0.5

        log.debug(f"Current prices for slot {slot_start.strftime('%H:%M')}: "
                  f"buy={buy_price:.4f}, sell={sell_price:.4f} EUR/kWh")

        return buy_price, sell_price

    except Exception as e:
        log.warning(f"Error getting current prices: {e}")
        return None, None


def _calculate_blended_effective_price(excess_solar_w, buy_price, sell_price):
    """Calculate blended effective price based on current solar surplus.

    This function determines the effective cost per kWh when charging with a mix
    of solar and grid power. Used for real-time charging decisions.

    The blending logic:
    - If excess_solar >= MIN_CHARGE_POWER_W (4140W): Pure solar charging,
      effective price = sell_price (opportunity cost of not exporting)
    - Otherwise: Weighted average based on solar fraction
      effective_price = (solar_fraction * sell_price) + (grid_fraction * buy_price)

    Args:
        excess_solar_w: Current excess solar power available in Watts
        buy_price: Current grid electricity buy price (EUR/kWh)
        sell_price: Current solar export/sell price (EUR/kWh)

    Returns:
        float: Blended effective price per kWh in EUR
    """
    # Handle edge cases
    if excess_solar_w is None or excess_solar_w < 0:
        excess_solar_w = 0.0

    if buy_price is None or buy_price < 0:
        # If no buy price available, can't calculate - return a high default
        return 1.0

    if sell_price is None or sell_price < 0:
        # If no sell price, assume zero opportunity cost for solar
        sell_price = 0.0

    # Pure solar: excess solar covers full minimum charge power
    if excess_solar_w >= MIN_CHARGE_POWER_W:
        # Charging is "free" except for opportunity cost of not selling
        return sell_price

    # Blended: part solar, part grid
    # Calculate what fraction of charging power comes from solar
    solar_fraction = excess_solar_w / MIN_CHARGE_POWER_W
    grid_fraction = 1.0 - solar_fraction

    # Weighted average price
    # Solar portion: opportunity cost (could have sold at sell_price)
    # Grid portion: actual cost (buy_price)
    effective_price = (solar_fraction * sell_price) + (grid_fraction * buy_price)

    return effective_price


def _is_price_cheap(price):
    """Check if the given price is considered cheap for blended charging.

    Compares the price against the daily average from Nordpool sensor.
    A price is considered "cheap" if it's below BLENDED_PRICE_THRESHOLD_FACTOR
    (default 75%) of the daily average.

    This function is used by the blended solar charging logic to determine
    whether grid prices are favorable enough to charge using a mix of
    solar and grid power.

    Args:
        price: Current electricity price in EUR/kWh to evaluate

    Returns:
        bool: True if price is cheap (below threshold), False otherwise.
              Returns False if price is None or no price data available
              (conservative default - don't charge when uncertain).
    """
    # Handle None or invalid price input
    if price is None:
        log.debug("_is_price_cheap: price is None, returning False (conservative)")
        return False

    try:
        # Get Nordpool sensor attributes
        nordpool_attrs = state.getattr(NORDPOOL_SENSOR) or {}
        raw_today = nordpool_attrs.get('raw_today', [])

        if not raw_today:
            log.debug("_is_price_cheap: No raw_today price data, returning False (conservative)")
            return False

        # Extract prices from raw_today entries
        prices = []
        for entry in raw_today:
            if 'value' in entry and entry['value'] is not None:
                try:
                    prices.append(float(entry['value']))
                except (ValueError, TypeError):
                    continue

        if not prices:
            log.debug("_is_price_cheap: No valid prices found in raw_today, returning False (conservative)")
            return False

        # Calculate daily average
        daily_average = sum(prices) / len(prices)

        # Calculate threshold
        threshold = daily_average * BLENDED_PRICE_THRESHOLD_FACTOR

        # Compare price to threshold
        is_cheap = price < threshold

        log.debug(f"_is_price_cheap: price={price:.4f}, avg={daily_average:.4f}, "
                  f"threshold={threshold:.4f} ({BLENDED_PRICE_THRESHOLD_FACTOR*100:.0f}%), "
                  f"is_cheap={is_cheap}")

        return is_cheap

    except Exception as e:
        log.warning(f"_is_price_cheap: Error calculating price threshold: {e}")
        return False


def _is_during_daylight():
    """Check if current time is during daylight hours.

    Returns:
        bool: True if between sunrise and sunset, False otherwise
    """
    sunrise, sunset = _get_sunrise_sunset()

    if sunrise is None or sunset is None:
        # Fallback: assume daylight between 6:00 and 21:00
        current_hour = datetime.now().hour
        return 6 <= current_hour < 21

    now = datetime.now().astimezone()
    return sunrise <= now <= sunset


def _get_solar_forecast_for_slot(slot_start):
    """Estimate solar production (kW) for a 15-minute slot.

    Uses daily forecast from Solcast distributed across daylight hours via solar curve.
    Applies confidence factor to be conservative with forecasts.

    The key insight: for a 1-hour period, kWh = kW average. So if a slot has
    2 kWh of production in 1 hour, that means 2 kW average power during that hour.

    Args:
        slot_start: datetime of slot start (timezone-aware)

    Returns:
        float: Estimated average solar power in kW for the slot (after baseload)
    """
    try:
        # Check if slot is during daylight hours - no solar outside sunrise/sunset
        sunrise, sunset = _get_sunrise_sunset()
        if sunrise and sunset:
            if slot_start < sunrise or slot_start >= sunset:
                return 0.0

        # Get the hour for the slot
        slot_hour = slot_start.hour

        # Check if this hour is in the solar production curve
        if slot_hour not in SOLAR_CURVE:
            return 0.0

        # Determine which day's forecast to use
        now = datetime.now().astimezone()
        today = now.date()
        slot_date = slot_start.date()

        if slot_date == today:
            # Use remaining production for today
            daily_forecast_kwh = float(state.get(SOLAR_REMAINING_TODAY) or 0)
        elif slot_date == today + timedelta(days=1):
            # Use tomorrow's full forecast
            daily_forecast_kwh = float(state.get(SOLAR_PRODUCTION_TOMORROW) or 0)
        else:
            # No forecast available for this date
            return 0.0

        # Get the fraction of daily production for this hour
        hour_fraction = SOLAR_CURVE.get(slot_hour, 0.0)

        # Calculate kWh for this hour
        # For a 1-hour period: kWh produced = kW average power
        # (e.g., 2 kWh in 1 hour = 2 kW average during that hour)
        hour_kwh = daily_forecast_kwh * hour_fraction * SOLAR_FORECAST_CONFIDENCE

        # Subtract baseload - what's left is available for charging
        # hour_kwh IS the average kW for the hour (energy per 1 hour = average power)
        available_kw = max(0.0, hour_kwh - BASELOAD_ESTIMATE_KW)

        return available_kw

    except Exception as e:
        log.warning(f"Error getting solar forecast for slot: {e}")
        return 0.0


def _calculate_effective_price(slot_start, buy_price, sell_price):
    """Calculate effective charging cost accounting for solar availability.

    When solar is available, charging can use free solar power. When solar is
    insufficient, we use grid power at buy price. When charging during solar
    production, we also account for opportunity cost (could have sold that solar).

    The effective price blends:
    - Grid portion: charged at buy_price
    - Solar portion: effectively free (could have sold at sell_price, but
      using for charging means we're not buying at buy_price - net benefit)

    Args:
        slot_start: datetime of slot start (timezone-aware)
        buy_price: Grid electricity buy price (EUR/kWh)
        sell_price: Solar export/sell price (EUR/kWh)

    Returns:
        tuple: (effective_price_per_kwh, solar_energy_kwh, grid_energy_kwh)
            - effective_price_per_kwh: Blended cost per kWh for this slot
            - solar_energy_kwh: kWh that can be covered by solar
            - grid_energy_kwh: kWh that must come from grid
    """
    # Energy charged in a 15-min slot at max rate
    slot_duration_hours = 0.25
    max_slot_energy_kwh = MAX_CHARGE_RATE_KW * slot_duration_hours

    # Get available solar power for this slot
    solar_available_kw = _get_solar_forecast_for_slot(slot_start)

    # Calculate how much of the charging can be covered by solar
    # Solar available in kW, slot is 0.25 hours, so solar energy = kW * 0.25
    solar_energy_kwh = min(solar_available_kw * slot_duration_hours, max_slot_energy_kwh)
    grid_energy_kwh = max_slot_energy_kwh - solar_energy_kwh

    # Calculate effective price
    # Solar portion: We save buy_price by using solar instead of grid
    # The "cost" of using solar is the opportunity cost (sell_price we don't get)
    # But since buy_price > sell_price typically, using solar for charging is beneficial
    # Effective cost for solar portion = sell_price (opportunity cost)
    # Effective cost for grid portion = buy_price

    if max_slot_energy_kwh > 0:
        solar_fraction = solar_energy_kwh / max_slot_energy_kwh
        grid_fraction = grid_energy_kwh / max_slot_energy_kwh

        # Blended effective price
        # Solar part: opportunity cost is sell_price (what we give up by not exporting)
        # Grid part: actual cost is buy_price
        effective_price = (solar_fraction * sell_price) + (grid_fraction * buy_price)
    else:
        effective_price = buy_price

    return (effective_price, solar_energy_kwh, grid_energy_kwh)


def _build_slot_list_with_effective_prices():
    """Build list of all available charging slots with effective prices.

    Combines Nordpool price data for today and tomorrow (if available) with
    solar forecasts to calculate effective charging costs for each 15-minute slot.

    Returns:
        list: List of slot dictionaries, sorted by effective_price (cheapest first).
              Each slot contains:
              - start: datetime of slot start
              - end: datetime of slot end
              - buy_price: Grid buy price (EUR/kWh)
              - sell_price: Solar export price (EUR/kWh)
              - effective_price: Blended cost accounting for solar (EUR/kWh)
              - solar_energy: kWh from solar in this slot
              - grid_energy: kWh from grid in this slot
              - energy: Total kWh charged in this slot (at max rate)
              Returns empty list if price data unavailable.
    """
    slots = []

    try:
        # Get Nordpool price data using state.getattr (safe pattern, no eval)
        nordpool_attrs = state.getattr(NORDPOOL_SENSOR) or {}

        raw_today = nordpool_attrs.get('raw_today', [])
        raw_tomorrow = nordpool_attrs.get('raw_tomorrow', [])
        tomorrow_valid = nordpool_attrs.get('tomorrow_valid', False)

        if not raw_today:
            log.warning("No raw_today price data available")
            return []

        # Normalize price data to 15-minute intervals
        normalized_today = _normalize_price_data(raw_today)

        all_prices = list(normalized_today)
        if tomorrow_valid and raw_tomorrow:
            normalized_tomorrow = _normalize_price_data(raw_tomorrow)
            all_prices.extend(normalized_tomorrow)

        # Get sell price data (may be time-varying or flat rate)
        sell_attrs = state.getattr(SELL_PRICE_SENSOR) or {}
        sell_raw_today = sell_attrs.get('raw_today', [])
        sell_raw_tomorrow = sell_attrs.get('raw_tomorrow', [])
        sell_tomorrow_valid = sell_attrs.get('tomorrow_valid', False)

        # Build sell price lookup (keyed by ISO timestamp)
        sell_lookup = {}
        if sell_raw_today:
            sell_normalized = _normalize_price_data(sell_raw_today)
            if sell_tomorrow_valid and sell_raw_tomorrow:
                sell_normalized.extend(_normalize_price_data(sell_raw_tomorrow))
            for entry in sell_normalized:
                start = entry.get('start')
                if start and 'value' in entry:
                    if isinstance(start, str):
                        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    else:
                        start_dt = start
                    sell_lookup[start_dt.isoformat()] = entry['value']

        # Flat rate fallback for sell price
        sell_price_flat = float(state.get(SELL_PRICE_SENSOR) or 0)

        # Current time for filtering past slots
        now = datetime.now().astimezone()

        # Build slot list
        slot_duration_hours = 0.25
        max_slot_energy = MAX_CHARGE_RATE_KW * slot_duration_hours

        for price_entry in all_prices:
            if 'start' not in price_entry or 'value' not in price_entry:
                continue

            # Parse start time
            start = price_entry['start']
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace('Z', '+00:00'))
            elif not hasattr(start, 'tzinfo') or start.tzinfo is None:
                # Make naive datetime timezone-aware (assume local)
                start = start.astimezone()

            # Parse end time
            end = price_entry.get('end')
            if end:
                if isinstance(end, str):
                    end = datetime.fromisoformat(end.replace('Z', '+00:00'))
                elif not hasattr(end, 'tzinfo') or end.tzinfo is None:
                    end = end.astimezone()
            else:
                end = start + timedelta(minutes=15)

            # Skip slots in the past
            if end <= now:
                continue

            buy_price = float(price_entry['value'])

            # Look up sell price for this slot (time-varying), fall back to flat rate
            sell_price = sell_lookup.get(start.isoformat())
            if sell_price is None:
                # Fall back to flat rate, or if no flat rate, estimate from buy price
                if sell_price_flat > 0:
                    sell_price = sell_price_flat
                else:
                    # Conservative estimate: sell price typically ~50% of buy price
                    sell_price = buy_price * 0.5

            # Calculate effective price with solar
            effective_price, solar_energy, grid_energy = _calculate_effective_price(
                start, buy_price, sell_price
            )

            slots.append({
                'start': start,
                'end': end,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'effective_price': effective_price,
                'solar_energy': solar_energy,
                'grid_energy': grid_energy,
                'energy': max_slot_energy,
            })

        # Sort by effective price (cheapest first)
        slots.sort(key=lambda s: s['effective_price'])

        log.info(f"Built {len(slots)} charging slots with effective prices")
        if slots:
            cheapest = slots[0]
            most_expensive = slots[-1]
            log.info(f"Price range: {cheapest['effective_price']:.4f} - {most_expensive['effective_price']:.4f} EUR/kWh")

        return slots

    except Exception as e:
        log.warning(f"Error building slot list: {e}")
        return []


def _calculate_kwh_needed(current_soc, target_soc):
    """Calculate kWh needed to charge from current to target SOC.

    Args:
        current_soc: Current state of charge (%)
        target_soc: Target state of charge (%)

    Returns:
        float: kWh needed (accounting for charging efficiency)
    """
    if current_soc >= target_soc:
        return 0.0

    soc_delta = target_soc - current_soc
    kwh_to_battery = (soc_delta / 100.0) * BATTERY_CAPACITY_KWH
    # Account for charging efficiency (need more from wall than goes to battery)
    kwh_from_wall = kwh_to_battery / CHARGING_EFFICIENCY

    return kwh_from_wall


def _calculate_charging_hours_needed(kwh_needed):
    """Calculate hours needed to charge given kWh at max rate.

    Args:
        kwh_needed: Energy needed from wall in kWh

    Returns:
        float: Hours needed for charging
    """
    if kwh_needed <= 0:
        return 0.0

    return kwh_needed / MAX_CHARGE_RATE_KW


def _get_next_deadline():
    """Calculate the next charging deadline datetime.

    If current time is past today's deadline, returns tomorrow's deadline.

    Returns:
        datetime: Next deadline as timezone-aware datetime
    """
    now = datetime.now().astimezone()
    deadline_today = now.replace(
        hour=CHARGE_DEADLINE_HOUR,
        minute=CHARGE_DEADLINE_MINUTE,
        second=0,
        microsecond=0
    )

    if now >= deadline_today:
        # Past today's deadline, use tomorrow
        return deadline_today + timedelta(days=1)

    return deadline_today


def _select_slots_for_energy(slots, energy_needed):
    """Select slots from a sorted list until energy need is met.

    Args:
        slots: List of slot dictionaries (should already be sorted by price)
        energy_needed: kWh of energy needed

    Returns:
        list: Selected slots that together provide at least energy_needed kWh
    """
    if energy_needed <= 0:
        return []

    selected = []
    energy_accumulated = 0.0

    for slot in slots:
        if energy_accumulated >= energy_needed:
            break
        selected.append(slot)
        energy_accumulated += slot['energy']

    return selected


def _store_schedule(schedule_slots, mode="scheduled"):
    """Store the charging schedule to Home Assistant entities.

    Stores the schedule as JSON in the input_text entity, with metadata
    stored as attributes on the input_number status entity.

    Args:
        schedule_slots: List of selected slot dictionaries
        mode: Charging mode string (e.g., "scheduled", "immediate", "solar")
    """
    try:
        now = datetime.now().astimezone()

        # Convert slots to JSON-serializable format
        slots_json = []
        total_cost = 0.0
        solar_slots_count = 0

        for slot in schedule_slots:
            # Convert datetime to ISO strings
            slot_data = {
                'start': slot['start'].isoformat(),
                'end': slot['end'].isoformat(),
                'buy_price': slot['buy_price'],
                'sell_price': slot['sell_price'],
                'effective_price': slot['effective_price'],
                'solar_energy': slot['solar_energy'],
                'grid_energy': slot['grid_energy'],
                'energy': slot['energy'],
            }
            slots_json.append(slot_data)
            total_cost += slot['effective_price'] * slot['energy']

            # Count slots with significant solar contribution
            if slot['solar_energy'] > 0.1:
                solar_slots_count += 1

        # Sort by start time for storage (even though we selected by price)
        slots_json.sort(key=lambda s: s['start'])

        # Build schedule object
        schedule_data = {
            'slots': slots_json,
            'slot_count': len(slots_json),
            'solar_slots_count': solar_slots_count,
            'estimated_cost_eur': round(total_cost, 4),
            'total_energy_kwh': round(sum([s['energy'] for s in schedule_slots]), 2),
            'mode': mode,
            'last_calculated': now.isoformat(),
        }

        # Find next slot start time
        if slots_json:
            # slots_json is now sorted by start time
            next_slot_start = slots_json[0]['start']
            schedule_data['next_slot_start'] = next_slot_start

        # Convert to JSON string
        schedule_json = json.dumps(schedule_data)

        # Store in input_text (truncate if needed - HA limit is 255 chars)
        # For the full schedule, we store it in attributes
        truncated_json = schedule_json[:255] if len(schedule_json) > 255 else schedule_json

        try:
            input_text.tesla_charging_schedule = truncated_json
        except Exception as e:
            log.warning(f"Error storing truncated schedule to input_text: {e}")

        # Store full schedule and metadata as attributes on status sensor
        try:
            state.setattr(OUTPUT_CHARGING_STATUS + ".schedule_json", schedule_json)
            state.setattr(OUTPUT_CHARGING_STATUS + ".slot_count", len(slots_json))
            state.setattr(OUTPUT_CHARGING_STATUS + ".solar_slots_count", solar_slots_count)
            state.setattr(OUTPUT_CHARGING_STATUS + ".estimated_cost_eur", round(total_cost, 4))
            state.setattr(OUTPUT_CHARGING_STATUS + ".last_calculated", now.isoformat())
            state.setattr(OUTPUT_CHARGING_STATUS + ".mode", mode)

            if slots_json:
                state.setattr(OUTPUT_CHARGING_STATUS + ".next_slot_start", slots_json[0]['start'])
                state.setattr(OUTPUT_CHARGING_STATUS + ".schedule_end", slots_json[-1]['end'])

            log.info(f"Stored schedule: {len(slots_json)} slots, {solar_slots_count} solar, "
                     f"estimated cost: {total_cost:.4f} EUR, mode: {mode}")
        except Exception as e:
            log.warning(f"Error storing schedule attributes: {e}")

    except Exception as e:
        log.warning(f"Error in _store_schedule: {e}")


def _calculate_and_store_schedule():
    """Calculate optimal charging schedule using two-pass greedy selection.

    Two-Pass Algorithm:
    1. Pass 1 (Mandatory): If current SOC < MIN_SOC_GUARANTEE, select cheapest
       slots BEFORE the deadline until we have enough energy to reach MIN_SOC_GUARANTEE.
    2. Pass 2 (Optional): From remaining slots (any time), select cheapest until
       we have enough energy to reach the charge_limit.

    The schedule is stored to HA entities for the executor to use.

    Returns:
        dict: Schedule result with keys:
            - success: bool
            - mandatory_slots: list of slots for mandatory charging
            - optional_slots: list of slots for optional charging
            - message: status message
    """
    try:
        # Get current Tesla state
        current_soc = _get_current_soc()
        charge_limit = _get_charge_limit()

        if current_soc is None:
            log.warning("Cannot calculate schedule: Current SOC unavailable")
            _update_charging_status(-1, "SOC unavailable")
            return {'success': False, 'message': "SOC unavailable"}

        if charge_limit is None:
            log.warning("Cannot calculate schedule: Charge limit unavailable")
            _update_charging_status(-1, "Charge limit unavailable")
            return {'success': False, 'message': "Charge limit unavailable"}

        log.info(f"Calculating schedule: SOC={current_soc}%, limit={charge_limit}%, "
                 f"min_guarantee={MIN_SOC_GUARANTEE}%")

        # Check if already at or above charge limit
        if current_soc >= charge_limit:
            log.info("Already at or above charge limit, no charging needed")
            _store_schedule([], mode="complete")
            _update_charging_status(5, "Target SOC reached")
            return {
                'success': True,
                'mandatory_slots': [],
                'optional_slots': [],
                'message': "Already at charge limit"
            }

        # Get next deadline
        deadline = _get_next_deadline()
        now = datetime.now().astimezone()

        log.info(f"Next deadline: {deadline.strftime('%Y-%m-%d %H:%M')}")

        # Build slot list with effective prices
        all_slots = _build_slot_list_with_effective_prices()

        if not all_slots:
            log.warning("No price slots available, cannot calculate schedule")
            _update_charging_status(-1, "No price data")
            return {'success': False, 'message': "No price data available"}

        # =====================================================================
        # PASS 1: Mandatory Charging (if needed)
        # Select cheapest slots BEFORE deadline to reach MIN_SOC_GUARANTEE
        # =====================================================================
        mandatory_slots = []
        mandatory_energy_needed = 0.0

        if current_soc < MIN_SOC_GUARANTEE:
            mandatory_energy_needed = _calculate_kwh_needed(current_soc, MIN_SOC_GUARANTEE)
            log.info(f"Pass 1: Need {mandatory_energy_needed:.2f} kWh for mandatory charging "
                     f"({current_soc}% -> {MIN_SOC_GUARANTEE}%)")

            # Filter slots that are BEFORE the deadline
            slots_before_deadline = [s for s in all_slots if s['start'] < deadline]

            if not slots_before_deadline:
                log.warning("No slots available before deadline for mandatory charging!")
                _update_charging_status(-1, "No slots before deadline")
                return {
                    'success': False,
                    'message': "No charging slots available before deadline"
                }

            # Sort by effective price (should already be sorted, but ensure)
            slots_before_deadline.sort(key=lambda s: s['effective_price'])

            # Select cheapest slots until we have enough energy
            mandatory_slots = _select_slots_for_energy(
                slots_before_deadline,
                mandatory_energy_needed
            )

            mandatory_energy_selected = sum([s['energy'] for s in mandatory_slots])
            log.info(f"Pass 1: Selected {len(mandatory_slots)} mandatory slots "
                     f"({mandatory_energy_selected:.2f} kWh)")

            # Check if we have enough slots
            if mandatory_energy_selected < mandatory_energy_needed:
                log.warning(f"Insufficient slots for mandatory charging! "
                            f"Need {mandatory_energy_needed:.2f} kWh, "
                            f"only {mandatory_energy_selected:.2f} kWh available")
                # Continue anyway - charge as much as possible
        else:
            log.info(f"Pass 1: Skipped - SOC {current_soc}% >= minimum {MIN_SOC_GUARANTEE}%")

        # =====================================================================
        # PASS 2: Optional Charging
        # From REMAINING slots (any time), select cheapest to reach charge_limit
        # =====================================================================
        optional_slots = []

        # Calculate remaining energy needed to reach charge_limit
        soc_after_mandatory = current_soc
        if mandatory_slots:
            # Estimate SOC after mandatory charging
            mandatory_energy = sum([s['energy'] for s in mandatory_slots])
            soc_delta = (mandatory_energy * CHARGING_EFFICIENCY / BATTERY_CAPACITY_KWH) * 100
            soc_after_mandatory = min(current_soc + soc_delta, charge_limit)

        optional_energy_needed = _calculate_kwh_needed(soc_after_mandatory, charge_limit)

        if optional_energy_needed > 0:
            log.info(f"Pass 2: Need {optional_energy_needed:.2f} kWh for optional charging "
                     f"({soc_after_mandatory:.1f}% -> {charge_limit}%)")

            # Get slot start times that were selected in mandatory pass
            mandatory_starts = {s['start'] for s in mandatory_slots}

            # Filter out slots already selected in mandatory pass
            remaining_slots = [s for s in all_slots if s['start'] not in mandatory_starts]

            # Sort by effective price
            remaining_slots.sort(key=lambda s: s['effective_price'])

            # Select cheapest remaining slots
            optional_slots = _select_slots_for_energy(
                remaining_slots,
                optional_energy_needed
            )

            optional_energy_selected = sum([s['energy'] for s in optional_slots])
            log.info(f"Pass 2: Selected {len(optional_slots)} optional slots "
                     f"({optional_energy_selected:.2f} kWh)")
        else:
            log.info("Pass 2: Skipped - no optional charging needed")

        # =====================================================================
        # Combine and store schedule
        # =====================================================================
        all_selected_slots = mandatory_slots + optional_slots

        if not all_selected_slots:
            log.info("No charging slots selected")
            _store_schedule([], mode="idle")
            _update_charging_status(0, "No charging needed")
            return {
                'success': True,
                'mandatory_slots': [],
                'optional_slots': [],
                'message': "No charging needed"
            }

        # Determine mode
        if mandatory_slots and optional_slots:
            mode = "scheduled_mandatory_optional"
        elif mandatory_slots:
            mode = "scheduled_mandatory"
        else:
            mode = "scheduled_optional"

        # Store the schedule
        _store_schedule(all_selected_slots, mode=mode)

        # Update status
        total_slots = len(all_selected_slots)
        total_energy = sum([s['energy'] for s in all_selected_slots])
        _update_charging_status(1, f"Scheduled: {total_slots} slots, {total_energy:.1f} kWh")

        # Log summary
        log.info(f"Schedule complete: {len(mandatory_slots)} mandatory + "
                 f"{len(optional_slots)} optional = {total_slots} total slots")

        return {
            'success': True,
            'mandatory_slots': mandatory_slots,
            'optional_slots': optional_slots,
            'message': f"Scheduled {total_slots} slots"
        }

    except Exception as e:
        log.warning(f"Error calculating schedule: {e}")
        _update_charging_status(-1, f"Error: {str(e)[:50]}")
        return {'success': False, 'message': str(e)}


def _update_charging_status(status_code, message=""):
    """Update the charging status output sensor.

    Status codes:
        0 = Idle/Not charging
        1 = Scheduled charging (waiting)
        2 = Active charging (grid)
        3 = Active charging (solar surplus)
        4 = Paused (waiting for cheaper rate)
        5 = Complete (target reached)
        -1 = Error/Unavailable

    Args:
        status_code: Integer status code
        message: Optional status message for attributes
    """
    try:
        input_number.tesla_charging_status = status_code
        if message:
            state.setattr(OUTPUT_CHARGING_STATUS + ".message", message)
        state.setattr(OUTPUT_CHARGING_STATUS + ".last_updated", datetime.now().isoformat())
    except Exception as e:
        log.warning(f"Error updating charging status: {e}")


# =============================================================================
# CHARGING CONTROL FUNCTIONS
# =============================================================================

def _start_charging(amps):
    """Start Tesla charging at specified amperage.

    Sets the charging amperage first, waits for it to be applied, then
    enables the charging switch.

    Args:
        amps: Charging amperage (will be clamped to MIN_CHARGE_AMPS..MAX_CHARGE_AMPS)

    Returns:
        bool: True if commands succeeded, False otherwise
    """
    try:
        # Clamp amperage to valid range
        amps = max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, int(amps)))

        log.info(f"Starting charging at {amps}A")

        # Set the charging current first
        number.set_value(entity_id=TESLA_CHARGE_CURRENT, value=amps)

        # Wait for the command to be applied
        task.sleep(2)

        # Enable charging
        switch.turn_on(entity_id=TESLA_CHARGE_SWITCH)

        # Record that we started charging (for tracking)
        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_started_by", "smart_charging")
        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_started_at", datetime.now().isoformat())
        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_amps", amps)

        return True

    except Exception as e:
        log.warning(f"Error starting charging: {e}")
        return False


def _stop_charging():
    """Stop Tesla charging.

    Turns off the charging switch. Does not modify the amperage setting.

    Returns:
        bool: True if command succeeded, False otherwise
    """
    try:
        log.info("Stopping charging")

        switch.turn_off(entity_id=TESLA_CHARGE_SWITCH)

        # Record that we stopped charging
        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_stopped_at", datetime.now().isoformat())

        return True

    except Exception as e:
        log.warning(f"Error stopping charging: {e}")
        return False


def _adjust_charging_amps(amps):
    """Adjust charging amperage without stopping.

    Only sends the command if the new value differs from current by at least 1A.
    This prevents unnecessary commands and API calls.

    Args:
        amps: New charging amperage (will be clamped to MIN_CHARGE_AMPS..MAX_CHARGE_AMPS)

    Returns:
        bool: True if adjustment was made or not needed, False on error
    """
    try:
        # Clamp amperage to valid range
        amps = max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, int(amps)))

        # Get current amperage
        current_amps = int(float(state.get(TESLA_CHARGE_CURRENT) or 0))

        # Only adjust if difference is at least 1A
        if abs(current_amps - amps) >= 1:
            log.info(f"Adjusting charging from {current_amps}A to {amps}A")
            number.set_value(entity_id=TESLA_CHARGE_CURRENT, value=amps)
            state.setattr(OUTPUT_CHARGING_STATUS + ".charging_amps", amps)
            return True
        else:
            log.debug(f"Charging amps already at {current_amps}A, no adjustment needed")
            return True

    except Exception as e:
        log.warning(f"Error adjusting charging amps: {e}")
        return False


def _is_currently_charging():
    """Check if the Tesla is currently charging.

    Returns:
        bool: True if actively charging, False otherwise
    """
    try:
        charging_state = state.get(TESLA_CHARGING_STATE)
        return charging_state == "Charging"
    except Exception as e:
        log.warning(f"Error checking charging state: {e}")
        return False


def _was_charging_started_by_us():
    """Check if the current charging session was started by smart charging.

    This prevents stopping charging that was manually started by the user.

    Returns:
        bool: True if we started the charging, False otherwise
    """
    try:
        attrs = state.getattr(OUTPUT_CHARGING_STATUS) or {}
        started_by = attrs.get("charging_started_by")
        return started_by == "smart_charging"
    except Exception as e:
        log.warning(f"Error checking who started charging: {e}")
        return False


def _get_stored_schedule():
    """Retrieve the stored charging schedule.

    Returns:
        dict: Schedule data with 'slots' list, or None if unavailable
    """
    try:
        attrs = state.getattr(OUTPUT_CHARGING_STATUS) or {}
        schedule_json = attrs.get("schedule_json")

        if not schedule_json:
            return None

        try:
            schedule = json.loads(schedule_json)
            return schedule
        except json.JSONDecodeError as e:
            log.warning(f"Error parsing stored schedule JSON: {e}")
            return None

    except Exception as e:
        log.warning(f"Error retrieving stored schedule: {e}")
        return None


def _is_current_time_in_scheduled_slot(schedule):
    """Check if current time falls within a scheduled charging slot.

    Args:
        schedule: Schedule dict with 'slots' list

    Returns:
        tuple: (is_in_slot, current_slot) where current_slot is the matching slot dict or None
    """
    try:
        if not schedule or 'slots' not in schedule:
            return False, None

        now = datetime.now().astimezone()

        for slot in schedule['slots']:
            # Parse slot times
            slot_start = slot['start']
            slot_end = slot['end']

            if isinstance(slot_start, str):
                slot_start = datetime.fromisoformat(slot_start.replace('Z', '+00:00'))
            if isinstance(slot_end, str):
                slot_end = datetime.fromisoformat(slot_end.replace('Z', '+00:00'))

            # Check if current time is within this slot
            if slot_start <= now < slot_end:
                return True, slot

        return False, None

    except Exception as e:
        log.warning(f"Error checking scheduled slot: {e}")
        return False, None


def _is_in_scheduled_slot():
    """Check if we are currently in a scheduled charging slot.

    Convenience wrapper that retrieves the schedule and checks slot.

    Returns:
        bool: True if current time is in a scheduled charging slot
    """
    schedule = _get_stored_schedule()
    if not schedule:
        return False

    is_in_slot, _ = _is_current_time_in_scheduled_slot(schedule)
    return is_in_slot


def _get_last_solar_change_time():
    """Get the timestamp of the last solar opportunistic charging change.

    Returns:
        datetime: Last change time or None if not set
    """
    try:
        attrs = state.getattr(OUTPUT_CHARGING_STATUS) or {}
        last_change = attrs.get("solar_last_change")
        if last_change:
            return datetime.fromisoformat(last_change)
        return None
    except Exception as e:
        log.warning(f"Error getting last solar change time: {e}")
        return None


def _set_last_solar_change_time():
    """Record the current time as the last solar opportunistic charging change."""
    try:
        state.setattr(OUTPUT_CHARGING_STATUS + ".solar_last_change", datetime.now().isoformat())
    except Exception as e:
        log.warning(f"Error setting last solar change time: {e}")


def _is_solar_change_allowed():
    """Check if enough time has passed since last solar charge adjustment.

    Enforces MIN_CHANGE_INTERVAL_SECONDS between solar charging changes
    to prevent rapid on/off cycling.

    Returns:
        bool: True if a change is allowed, False if still in cooldown
    """
    last_change = _get_last_solar_change_time()
    if last_change is None:
        return True

    now = datetime.now().astimezone()
    elapsed = (now - last_change.astimezone()).total_seconds()
    return elapsed >= MIN_CHANGE_INTERVAL_SECONDS


# Debounce interval for solar opportunity function calls (seconds)
SOLAR_OPPORTUNITY_DEBOUNCE_SECONDS = 5


def _get_last_solar_opportunity_call_time():
    """Get the timestamp of the last solar opportunity function call.

    Returns:
        datetime: Last call time or None if not set
    """
    try:
        attrs = state.getattr(OUTPUT_CHARGING_STATUS) or {}
        last_call = attrs.get("solar_opportunity_last_call")
        if last_call:
            return datetime.fromisoformat(last_call)
        return None
    except Exception as e:
        log.warning(f"Error getting last solar opportunity call time: {e}")
        return None


def _set_last_solar_opportunity_call_time():
    """Record the current time as the last solar opportunity function call."""
    try:
        state.setattr(OUTPUT_CHARGING_STATUS + ".solar_opportunity_last_call", datetime.now().isoformat())
    except Exception as e:
        log.warning(f"Error setting last solar opportunity call time: {e}")


def _is_solar_opportunity_debounce_elapsed():
    """Check if enough time has passed since last solar opportunity function call.

    This is separate from the charging change throttle - it prevents the function
    from doing work too frequently when grid power sensor updates rapidly.

    Returns:
        bool: True if debounce period has elapsed, False if still in debounce
    """
    last_call = _get_last_solar_opportunity_call_time()
    if last_call is None:
        return True

    now = datetime.now().astimezone()
    elapsed = (now - last_call.astimezone()).total_seconds()
    return elapsed >= SOLAR_OPPORTUNITY_DEBOUNCE_SECONDS


def _is_in_opportunistic_solar_mode():
    """Check if currently in opportunistic solar charging mode.

    Returns:
        bool: True if charging was started for solar opportunism
    """
    try:
        attrs = state.getattr(OUTPUT_CHARGING_STATUS) or {}
        started_by = attrs.get("charging_started_by")
        return started_by == "solar_opportunistic"
    except Exception as e:
        log.warning(f"Error checking solar mode: {e}")
        return False


def _start_solar_opportunistic_charging(amps):
    """Start solar opportunistic charging.

    Similar to _start_charging but marks the session as solar opportunistic.

    Args:
        amps: Charging amperage

    Returns:
        bool: True if successful
    """
    try:
        amps = max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, int(amps)))
        log.info(f"Starting solar opportunistic charging at {amps}A")

        number.set_value(entity_id=TESLA_CHARGE_CURRENT, value=amps)
        task.sleep(2)
        switch.turn_on(entity_id=TESLA_CHARGE_SWITCH)

        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_started_by", "solar_opportunistic")
        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_started_at", datetime.now().isoformat())
        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_amps", amps)
        _set_last_solar_change_time()

        return True
    except Exception as e:
        log.warning(f"Error starting solar opportunistic charging: {e}")
        return False


def _stop_solar_opportunistic_charging():
    """Stop solar opportunistic charging.

    Only stops if we started it for solar opportunism.

    Returns:
        bool: True if stopped, False otherwise
    """
    try:
        if not _is_in_opportunistic_solar_mode():
            log.debug("Not in solar opportunistic mode, not stopping")
            return False

        log.info("Stopping solar opportunistic charging")
        switch.turn_off(entity_id=TESLA_CHARGE_SWITCH)

        state.setattr(OUTPUT_CHARGING_STATUS + ".charging_stopped_at", datetime.now().isoformat())
        _set_last_solar_change_time()

        return True
    except Exception as e:
        log.warning(f"Error stopping solar opportunistic charging: {e}")
        return False


def _calculate_target_amps_from_power(excess_watts):
    """Calculate target charging amps from excess power.

    For 3-phase at 230V: P = 3 x V x I = 690 x I
    Therefore: I = P / 690

    Args:
        excess_watts: Excess power available in Watts

    Returns:
        int: Target amps clamped to valid range
    """
    power_per_amp = VOLTAGE * PHASES  # 230V x 3 = 690W per amp
    target_amps = int(excess_watts / power_per_amp)
    return max(MIN_CHARGE_AMPS, min(MAX_CHARGE_AMPS, target_amps))


def _update_charging_schedule(schedule_data):
    """Update the charging schedule output sensor.

    Args:
        schedule_data: Dictionary containing schedule information
    """
    try:
        schedule_json = json.dumps(schedule_data)
        input_text.tesla_charging_schedule = schedule_json
        input_text.tesla_charging_schedule.last_updated = datetime.now().isoformat()
    except Exception as e:
        log.warning(f"Error updating charging schedule: {e}")


# =============================================================================
# SCHEDULED TRIGGERS
# =============================================================================

@time_trigger("cron(0 15 * * *)")
@service
def calculateTeslaChargingSchedule():
    """Daily trigger to calculate optimal Tesla charging schedule.

    Runs at 15:00 daily (when tomorrow's Nordpool prices are available).
    Calculates the optimal charging schedule for the next 24-36 hours.

    The schedule is calculated regardless of car location so it's ready
    when the car arrives home. If SOC is unavailable (car not reachable),
    the calculation will fail gracefully and be retried when the car
    arrives home or cable is connected (via state triggers).

    Preconditions:
    - Smart charging must be enabled

    The schedule is stored to HA entities and executed by executeTeslaChargingSchedule().
    """
    log.info("Daily Tesla charging schedule calculation triggered")

    # Check if smart charging is enabled
    if not _is_smart_charging_enabled():
        log.info("Smart charging is disabled, skipping schedule calculation")
        _update_charging_status(0, "Smart charging disabled")
        return

    # Calculate schedule - will fail gracefully if car not reachable (SOC unavailable)
    # Schedule will be recalculated when car arrives home or cable is connected
    log.info("Calculating charging schedule...")

    result = _calculate_and_store_schedule()

    if result['success']:
        log.info(f"Schedule calculation complete: {result['message']}")
    else:
        log.warning(f"Schedule calculation failed: {result['message']}")


@time_trigger("cron(2,17,32,47 * * * *)")
def executeTeslaChargingSchedule():
    """Execute the Tesla charging schedule every 15 minutes.

    Runs at minutes 2, 17, 32, 47 of each hour (offset from slot boundaries).
    Reads the stored schedule and starts/stops charging based on whether
    the current time falls within a scheduled slot.

    Important behaviors:
    - Does NOT check SOC to avoid waking the car unnecessarily
    - Only stops charging if it was started by smart charging (not manual)
    - Tracks charging state to handle transitions properly
    """
    log.debug("Executing Tesla charging schedule check")

    # Check if smart charging is enabled
    if not _is_smart_charging_enabled():
        log.debug("Smart charging is disabled")
        return

    # Check if car is at home (uses cached state, doesn't wake car)
    if not _is_car_at_home():
        log.debug("Car is not at home")
        return

    # Check if cable is connected (uses cached state, doesn't wake car)
    if not _is_cable_connected():
        log.debug("Charging cable not connected")
        return

    # Get the stored schedule
    schedule = _get_stored_schedule()

    if not schedule:
        log.debug("No charging schedule stored")
        return

    # Check schedule mode - if complete, nothing to do
    mode = schedule.get('mode', '')
    if mode == 'complete' or mode == 'idle':
        log.debug(f"Schedule mode is '{mode}', no action needed")
        return

    # Check if current time is within a scheduled slot
    is_in_slot, current_slot = _is_current_time_in_scheduled_slot(schedule)
    is_charging = _is_currently_charging()
    we_started_it = _was_charging_started_by_us()

    log.debug(f"Slot check: in_slot={is_in_slot}, charging={is_charging}, we_started={we_started_it}")

    if is_in_slot:
        # We SHOULD be charging
        if not is_charging:
            # Need to start charging
            log.info(f"Starting scheduled charging for slot: {current_slot['start']}")
            if _start_charging(MAX_CHARGE_AMPS):
                # Determine if this is solar or grid charging
                solar_energy = current_slot.get('solar_energy', 0)
                if solar_energy > 0.5:  # More than 0.5 kWh from solar
                    _update_charging_status(3, "Charging (solar)")
                else:
                    _update_charging_status(2, "Charging (grid)")
            else:
                log.warning("Failed to start charging")
                _update_charging_status(-1, "Failed to start charging")
        else:
            # Already charging, just update status if needed
            solar_energy = current_slot.get('solar_energy', 0)
            if solar_energy > 0.5:
                _update_charging_status(3, "Charging (solar)")
            else:
                _update_charging_status(2, "Charging (grid)")
    else:
        # We should NOT be charging (outside scheduled slots)
        if is_charging and we_started_it:
            # We started it and slot is over - stop charging
            log.info("Stopping charging - outside scheduled slot")
            if _stop_charging():
                _update_charging_status(4, "Paused - waiting for next slot")
            else:
                log.warning("Failed to stop charging")
        elif is_charging and not we_started_it:
            # User started charging manually - don't interfere
            log.debug("Charging in progress but not started by us - not stopping")
        else:
            # Not charging and not supposed to be - update status
            # Check if there are future slots
            if schedule.get('slots'):
                _update_charging_status(1, "Waiting for scheduled slot")
            else:
                _update_charging_status(0, "No charging scheduled")


# =============================================================================
# STATE TRIGGERS - Replanning and Solar Opportunism
# =============================================================================

@state_trigger(f"{OUTPUT_SMART_CHARGING_ENABLED} == 'on'")
def on_smart_charging_enabled():
    """Trigger when smart charging is enabled.

    Calculates a new charging schedule immediately so the user doesn't
    have to wait until the next scheduled calculation (15:00).
    """
    log.info("Smart charging enabled - calculating schedule")

    # Brief delay to ensure state is stable
    task.sleep(2)

    result = _calculate_and_store_schedule()

    if result['success']:
        log.info(f"Schedule calculated on enable: {result['message']}")
    else:
        log.warning(f"Schedule calculation on enable failed: {result['message']}")


@state_trigger(f"{TESLA_LOCATION} == '{HOME_LOCATION}'")
def on_car_arrives_home():
    """Trigger when car arrives at home location.

    Waits briefly for sensors to stabilize, then calculates a new
    charging schedule if preconditions are met.
    """
    log.info("Tesla arrived home - checking for schedule calculation")

    # Wait for sensors to update/stabilize after arrival
    task.sleep(60)

    # Check if smart charging is enabled
    if not _is_smart_charging_enabled():
        log.debug("Smart charging disabled, skipping schedule on arrival")
        return

    # Check if cable is connected
    if not _is_cable_connected():
        log.info("Cable not connected after arrival, skipping schedule")
        return

    # Calculate and store a new schedule
    log.info("Calculating new charging schedule after home arrival")
    result = _calculate_and_store_schedule()

    if result['success']:
        log.info(f"Schedule calculated on arrival: {result['message']}")
    else:
        log.warning(f"Schedule calculation on arrival failed: {result['message']}")


@state_trigger(f"{TESLA_CHARGE_CABLE} == 'on'")
def on_cable_connected():
    """Trigger when charging cable is connected.

    Calculates a new charging schedule and stops any auto-started
    charging if we're not in a scheduled slot (Tesla sometimes
    auto-starts charging when cable is connected).
    """
    log.info("Charging cable connected - checking for schedule calculation")

    # Wait briefly for state to stabilize
    task.sleep(5)

    # Check if smart charging is enabled
    if not _is_smart_charging_enabled():
        log.debug("Smart charging disabled, skipping schedule on cable connect")
        return

    # Check if car is at home
    if not _is_car_at_home():
        log.info("Car not at home when cable connected, skipping schedule")
        return

    # Calculate and store a new schedule
    log.info("Calculating new charging schedule after cable connection")
    result = _calculate_and_store_schedule()

    if result['success']:
        log.info(f"Schedule calculated on cable connect: {result['message']}")
    else:
        log.warning(f"Schedule calculation on cable connect failed: {result['message']}")

    # Check if Tesla auto-started charging
    task.sleep(10)  # Wait for charging state to update

    if _is_currently_charging():
        # Check if we're in a scheduled slot
        if not _is_in_scheduled_slot():
            # Only stop if WE started the charging (not user/Tesla app)
            if _was_charging_started_by_us():
                log.info("Stopping our auto-started charging - not in scheduled slot")
                _stop_charging()
                _update_charging_status(4, "Auto-charge stopped - waiting for slot")
            else:
                log.info("Charging started by user/Tesla - not interfering")
        else:
            log.info("Tesla started charging and we're in a scheduled slot - allowing")
            # Mark that smart charging is managing this
            state.setattr(OUTPUT_CHARGING_STATUS + ".charging_started_by", "smart_charging")


@state_trigger(f"{GRID_POWER_CURRENT}")
def handle_solar_opportunity():
    """Handle solar opportunistic charging based on grid power.

    Monitors grid power export and starts/adjusts charging when there's
    excess solar. Three-tier decision logic:
    - Tier 1 PURE: surplus >= SOLAR_START_THRESHOLD_W (4500W) - charge at calculated amps
    - Tier 2 BLENDED: surplus >= SOLAR_BLENDED_MIN_W (1500W) - charge at min amps if price cheap
    - Tier 3 STOP: surplus < SOLAR_BLENDED_MIN_W - stop opportunistic charging

    Grid power convention: negative = exporting, positive = importing.
    So we look for grid_power < -4500 to start pure solar charging.

    Performance: This function is triggered on every grid power sensor update,
    which can be very frequent. Quick precondition checks are done first to
    minimize work, and a 5-second debounce prevents excessive processing.
    """
    # =======================================================================
    # QUICK PRECONDITION CHECKS - do these first to minimize work
    # These use cached state and are very fast
    # =======================================================================

    # Skip if smart charging is disabled
    if not _is_smart_charging_enabled():
        return

    # Skip if not during daylight hours (uses cached sun state)
    if not _is_during_daylight():
        return

    # Skip if car not at home or cable not connected (uses cached state)
    if not _is_car_at_home() or not _is_cable_connected():
        return

    # =======================================================================
    # DEBOUNCE CHECK - prevent function from running too frequently
    # This is separate from the charging change throttle
    # =======================================================================
    if not _is_solar_opportunity_debounce_elapsed():
        return

    # Record this function call time for debouncing
    _set_last_solar_opportunity_call_time()

    # =======================================================================
    # SLOWER CHECKS - after debounce
    # =======================================================================

    # Skip if in a scheduled charging slot (scheduled charging takes priority)
    if _is_in_scheduled_slot():
        return

    # Throttle actual charging changes to prevent rapid cycling
    # (this is the 5-minute interval between start/stop commands)
    if not _is_solar_change_allowed():
        return

    # Get current grid power (negative = exporting)
    try:
        grid_power = float(state.get(GRID_POWER_CURRENT) or 0)
    except (ValueError, TypeError):
        return

    # Calculate surplus: positive when exporting (grid_power is negative)
    surplus_watts = -grid_power

    is_charging = _is_currently_charging()
    is_solar_mode = _is_in_opportunistic_solar_mode()

    # Check current SOC - don't solar charge if already at limit
    current_soc = _get_current_soc()
    charge_limit = _get_charge_limit()
    if current_soc is not None and charge_limit is not None:
        if current_soc >= charge_limit:
            # Already at limit - stop if in solar mode
            if is_charging and is_solar_mode:
                log.info("SOC at limit, stopping solar opportunistic charging")
                _stop_solar_opportunistic_charging()
                _update_charging_status(5, "Target SOC reached")
            return

    # =======================================================================
    # THREE-TIER DECISION LOGIC
    # Tier 1: PURE SOLAR (surplus >= 4500W) - charge at calculated amps
    # Tier 2: BLENDED (1500W <= surplus < 4500W) - charge at min if price cheap
    # Tier 3: STOP (surplus < 1500W AND in solar mode) - stop charging
    # =======================================================================

    # --- TIER 1: PURE SOLAR ---
    # Enough excess solar to fully cover minimum charge power
    if surplus_watts >= SOLAR_START_THRESHOLD_W:
        target_amps = _calculate_target_amps_from_power(surplus_watts)

        if not is_charging:
            # Not charging - start pure solar opportunistic charging
            log.info(f"PURE SOLAR: Starting charging at {target_amps}A ({surplus_watts:.0f}W surplus)")
            if _start_solar_opportunistic_charging(target_amps):
                _update_charging_status(3, f"Solar charging: {surplus_watts:.0f}W surplus")
        elif is_solar_mode:
            # Already in solar mode - adjust amps if beneficial
            current_amps = int(float(state.get(TESLA_CHARGE_CURRENT) or 0))
            if abs(target_amps - current_amps) >= 1:
                log.info(f"PURE SOLAR: Adjusting {current_amps}A -> {target_amps}A ({surplus_watts:.0f}W surplus)")
                _adjust_charging_amps(target_amps)
                _set_last_solar_change_time()

    # --- TIER 2: BLENDED (partial solar + grid) ---
    # Some solar available but not enough for full minimum charge power
    # Only charge if grid price makes the blended price attractive
    elif surplus_watts >= SOLAR_BLENDED_MIN_W:
        # Get current prices for blended calculation
        buy_price, sell_price = _get_current_prices()

        if buy_price is not None:
            # Calculate effective blended price
            effective_price = _calculate_blended_effective_price(surplus_watts, buy_price, sell_price)
            is_cheap = _is_price_cheap(effective_price)

            log.debug(f"BLENDED: surplus={surplus_watts:.0f}W, buy={buy_price:.4f}, "
                      f"eff={effective_price:.4f}, cheap={is_cheap}")

            if is_cheap:
                # Blended price is attractive - charge at minimum amps
                if not is_charging:
                    log.info(f"BLENDED: Starting charging at {MIN_CHARGE_AMPS}A "
                             f"({surplus_watts:.0f}W surplus, eff_price={effective_price:.4f})")
                    if _start_solar_opportunistic_charging(MIN_CHARGE_AMPS):
                        _update_charging_status(3, f"Blended charging: {surplus_watts:.0f}W + grid")
                elif is_solar_mode:
                    # Already in solar mode from pure solar - reduce to minimum amps
                    current_amps = int(float(state.get(TESLA_CHARGE_CURRENT) or 0))
                    if current_amps > MIN_CHARGE_AMPS:
                        log.info(f"BLENDED: Reducing {current_amps}A -> {MIN_CHARGE_AMPS}A (entering blended zone)")
                        _adjust_charging_amps(MIN_CHARGE_AMPS)
                        _set_last_solar_change_time()
            else:
                # Blended price not attractive - stop if we're in solar mode
                if is_charging and is_solar_mode:
                    log.info(f"BLENDED: Stopping - price not cheap enough "
                             f"(eff={effective_price:.4f}, surplus={surplus_watts:.0f}W)")
                    if _stop_solar_opportunistic_charging():
                        _update_charging_status(0, "Solar paused - blended price too high")
        else:
            # No price data available - be conservative, stop if in solar mode
            if is_charging and is_solar_mode:
                log.info("BLENDED: Stopping - no price data available")
                if _stop_solar_opportunistic_charging():
                    _update_charging_status(0, "Solar paused - no price data")

    # --- TIER 3: STOP ---
    # Below SOLAR_BLENDED_MIN_W - insufficient surplus for any solar charging
    elif is_charging and is_solar_mode:
        log.info(f"STOP: Stopping solar charging ({surplus_watts:.0f}W surplus below {SOLAR_BLENDED_MIN_W}W threshold)")
        if _stop_solar_opportunistic_charging():
            _update_charging_status(0, "Solar charging paused - insufficient surplus")


# =============================================================================
# TEST SERVICE
# =============================================================================

@service
def teslaSmartChargingTestService(action=None, id=None):
    """Service to manually trigger Tesla smart charging calculations.

    Use this for testing without waiting for the cron trigger.
    Call from HA Developer Tools > Services > pyscript.tesla_smart_charging_test_service
    """
    log.warning("Manually triggering Tesla Smart Charging test service")

    # Log current state for debugging
    log.info(f"Smart charging enabled: {_is_smart_charging_enabled()}")
    log.info(f"Car at home: {_is_car_at_home()}")
    log.info(f"Cable connected: {_is_cable_connected()}")
    log.info(f"Current SOC: {_get_current_soc()}%")
    log.info(f"Charge limit: {_get_charge_limit()}%")
    log.info(f"Is daylight: {_is_during_daylight()}")
    log.info(f"Solar surplus: {_get_current_solar_surplus()}W")

    sunrise, sunset = _get_sunrise_sunset()
    log.info(f"Sunrise: {sunrise}, Sunset: {sunset}")

    deadline = _get_next_deadline()
    log.info(f"Next deadline: {deadline}")

    current_soc = _get_current_soc()
    charge_limit = _get_charge_limit()
    if current_soc is not None and charge_limit is not None:
        kwh_needed = _calculate_kwh_needed(current_soc, charge_limit)
        hours_needed = _calculate_charging_hours_needed(kwh_needed)
        log.info(f"kWh needed: {kwh_needed:.2f}, Hours needed: {hours_needed:.2f}")

    # Test price normalization
    try:
        nordpool_attrs = state.getattr(NORDPOOL_SENSOR) or {}
        raw_today = nordpool_attrs.get('raw_today', [])
        if raw_today:
            normalized = _normalize_price_data(raw_today)
            log.info(f"Normalized {len(raw_today)} raw entries to {len(normalized)} 15-min intervals")
            if normalized:
                prices = [d['value'] for d in normalized if 'value' in d]
                log.info(f"Price range today: {min(prices):.4f} - {max(prices):.4f} EUR/kWh")
    except Exception as e:
        log.warning(f"Error testing price normalization: {e}")

    # Test solar forecast functions
    try:
        now = datetime.now().astimezone()
        # Test solar forecast for current slot
        solar_kw = _get_solar_forecast_for_slot(now)
        log.info(f"Solar forecast for current slot: {solar_kw:.2f} kW")

        # Test effective price calculation
        buy_price = float(state.get(NORDPOOL_SENSOR) or 0.10)
        sell_price = float(state.get(SELL_PRICE_SENSOR) or 0.05)
        eff_price, solar_e, grid_e = _calculate_effective_price(now, buy_price, sell_price)
        log.info(f"Effective price: {eff_price:.4f} EUR/kWh (solar: {solar_e:.2f} kWh, grid: {grid_e:.2f} kWh)")

        # Test slot list building
        slots = _build_slot_list_with_effective_prices()
        log.info(f"Total available slots: {len(slots)}")
        if slots:
            # Show top 5 cheapest slots
            log.info("Top 5 cheapest slots:")
            for i, slot in enumerate(slots[:5]):
                log.info(f"  {i+1}. {slot['start'].strftime('%H:%M')} - eff: {slot['effective_price']:.4f}, buy: {slot['buy_price']:.4f}, solar: {slot['solar_energy']:.2f} kWh")
    except Exception as e:
        log.warning(f"Error testing solar-aware pricing: {e}")

    # Test the scheduling algorithm
    try:
        log.info("Testing scheduling algorithm...")
        result = _calculate_and_store_schedule()
        log.info(f"Schedule result: {result}")

        if result['success']:
            mandatory = result.get('mandatory_slots', [])
            optional = result.get('optional_slots', [])
            log.info(f"Mandatory slots: {len(mandatory)}, Optional slots: {len(optional)}")

            if mandatory:
                log.info("Mandatory slots (before deadline):")
                for i, slot in enumerate(mandatory[:5]):  # Show first 5
                    log.info(f"  {i+1}. {slot['start'].strftime('%Y-%m-%d %H:%M')} - "
                             f"eff: {slot['effective_price']:.4f} EUR/kWh")

            if optional:
                log.info("Optional slots (any time):")
                for i, slot in enumerate(optional[:5]):  # Show first 5
                    log.info(f"  {i+1}. {slot['start'].strftime('%Y-%m-%d %H:%M')} - "
                             f"eff: {slot['effective_price']:.4f} EUR/kWh")
    except Exception as e:
        log.warning(f"Error testing scheduling algorithm: {e}")
        # Fall back to simple status update
        _update_charging_status(0, "Test service executed")
        _update_charging_schedule({
            "test": True,
            "timestamp": datetime.now().isoformat(),
            "car_available": _is_car_at_home() and _is_cable_connected()
        })

    # Test blended pricing functions
    try:
        log.info("Testing blended pricing functions...")

        # Test _get_current_prices()
        buy_price, sell_price = _get_current_prices()
        log.info(f"Current prices - Buy: {buy_price}, Sell: {sell_price}")

        if buy_price is not None and sell_price is not None:
            # Test _calculate_blended_effective_price() with various surplus values
            test_surpluses = [5000, 3000, 2000, 1000, 500]
            log.info("Blended effective prices for different surplus values:")
            for surplus in test_surpluses:
                eff = _calculate_blended_effective_price(surplus, buy_price, sell_price)
                solar_frac = min(surplus / MIN_CHARGE_POWER_W, 1.0) * 100
                log.info(f"  {surplus}W surplus: {eff:.4f} EUR/kWh ({solar_frac:.0f}% solar)")

            # Test _is_price_cheap() with sample prices
            current_surplus = _get_current_solar_surplus()
            if current_surplus > 0:
                current_eff = _calculate_blended_effective_price(current_surplus, buy_price, sell_price)
                is_cheap = _is_price_cheap(current_eff)
                log.info(f"Current surplus {current_surplus}W -> effective {current_eff:.4f} EUR/kWh -> cheap: {is_cheap}")

            # Test with known prices
            log.info(f"_is_price_cheap() tests:")
            log.info(f"  Price 0.01: cheap = {_is_price_cheap(0.01)}")
            log.info(f"  Price 0.05: cheap = {_is_price_cheap(0.05)}")
            log.info(f"  Price 0.10: cheap = {_is_price_cheap(0.10)}")
            log.info(f"  Price 0.20: cheap = {_is_price_cheap(0.20)}")
    except Exception as e:
        log.warning(f"Error testing blended pricing functions: {e}")

    log.warning("Tesla Smart Charging test service completed")
