from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timedelta
import asyncio

def _get_statistic(
    start_time: datetime,
    end_time: datetime | None,
    statistic_ids: list[str] | None,
    period: Literal["5minute", "day", "hour", "week", "month"],
    types: set[Literal["last_reset", "max", "mean", "min", "state", "sum"]]):

    # This is probably not needed so leaving it commented out
    #start_time = start_time.astimezone(timezone.utc)
    #end_time = end_time.astimezone(timezone.utc)
    counter = 0
    task = get_instance(hass).async_add_executor_job(statistics_during_period, hass, start_time, end_time, statistic_ids, period, None, types)
    while not task.done():
        asyncio.sleep(1)
        counter = counter + 1
        if counter > 10:
            break
    return task.result()


def _get_long_term_prices():

    start_date = datetime.now() - timedelta(days=10)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    end_date = datetime.now()
    end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

    # Buy price sensor
    sensor = 'sensor.nordpool_kwh_fi_eur_3_10_0'

    stats = _get_statistic(start_date, end_date, [sensor], "hour", ['state'])
    stat = [{'start': d.get('start'), 'value': float(d.get('state'))} for d in stats.get(sensor)]

    return stat


def _normalize_price_data(price_dictionaries):
    """Normalize price data to 15-minute intervals.

    Handles mixed format where some entries may span a full hour (due to timezone
    differences) by splitting hourly entries into 4 equal 15-minute prices.
    """
    normalized = []

    for entry in price_dictionaries:
        if 'start' not in entry or 'end' not in entry or 'value' not in entry:
            # Keep entries without proper start/end/value as-is
            normalized.append(entry)
            continue

        start = datetime.fromisoformat(entry['start'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(entry['end'].replace('Z', '+00:00'))
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

    monthly_start_date = datetime.now()
    monthly_start_date = monthly_start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_end_date = datetime.now()
    monthly_end_date = monthly_end_date.replace(month=monthly_end_date.month+1, day=1, hour=0, minute=0, second=0, microsecond=0)

    monthly_raw = _get_statistic(monthly_start_date, monthly_end_date, [buy_price_entity_id], "hour", ['state'])
    monthly_floats = [{'start': d.get('start'), 'value': float(d.get('state'))} for d in monthly_raw.get(buy_price_entity_id)]
    monthly_prices = [d['value'] for d in monthly_floats if 'value' in d]
    monthly_avg = sum(monthly_prices) / len(monthly_prices)

    input_number.electricity_buy_price_monthly_average = monthly_avg

    yearly_start_date = datetime.now()
    yearly_start_date = yearly_start_date.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    yearly_end_date = datetime.now()
    yearly_end_date = yearly_end_date.replace(year=yearly_end_date.year+1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    yearly_raw = _get_statistic(yearly_start_date, yearly_end_date, [buy_price_entity_id], "hour", ['state'])
    yearly_floats = [{'start': d.get('start'), 'value': float(d.get('state'))} for d in yearly_raw.get(buy_price_entity_id)]
    yearly_prices = [d['value'] for d in yearly_floats if 'value' in d]
    yearly_avg = sum(yearly_prices) / len(yearly_prices)

    input_number.electricity_buy_price_yearly_average = yearly_avg


@time_trigger("cron(2,17,32,47 * * * *)")
def updateSpotPriceSensors():
    """Update spot price sensors based on future spot prices"""

    # Spot price sensor
    spot_price_sensor = sensor.nordpool_kwh_fi_eur_3_10_0

    # Normalize to handle mixed hourly/15-min format
    price_dictionaries = _normalize_price_data(spot_price_sensor.raw_today)

    prices = [d['value'] for d in price_dictionaries if 'value' in d]
    price_average_today = sum(prices) / len(prices)
    price_min_today = min(prices)
    price_max_today = max(prices)
    price_25_percent_today = (price_average_today + price_min_today) / 2
    price_75_percent_today = (price_average_today + price_max_today) / 2

    if spot_price_sensor.tomorrow_valid:
        price_dictionaries += _normalize_price_data(spot_price_sensor.raw_tomorrow)

    prices = [d['value'] for d in price_dictionaries if 'value' in d]
    price_average_short = sum(prices) / len(prices)
    price_min_short = min(prices)
    price_max_short = max(prices)
    price_25_percent_short = (price_average_short + price_min_short) / 2
    price_75_percent_short = (price_average_short + price_max_short) / 2

    price_dictionaries += _get_long_term_prices()
    prices = [d['value'] for d in price_dictionaries if 'value' in d]
    price_average_long = sum(prices) / len(prices)
    price_min_long = min(prices)
    price_max_long = max(prices)
    price_25_percent_long = (price_average_long + price_min_long) / 2
    price_75_percent_long = (price_average_long + price_max_long) / 2

    price_current = spot_price_sensor.current_price

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
