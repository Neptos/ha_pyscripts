from datetime import datetime, timedelta, timezone
from HassService import _get_statistic


def _get_long_term_prices():

    start_date = datetime.now() - timedelta(days=10)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    end_date = datetime.now()
    end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

    sensor = 'sensor.nordpool_spotprice_no_transfer'

    stats = _get_statistic(start_date, end_date, [sensor], "hour", ['state'])
    stat = [{'start': d.get('start'), 'value': float(d.get('state'))} for d in stats.get(sensor)]

    return stat

@service
def manualUpdateSpotPriceSensors(action=None, id=None):
    """Service to manually run the spot price sensor updater"""
    log.warning(f"Manually triggering update spot price sensors")

    updateSpotPriceSensors()


@time_trigger("cron(2 * * * *)")
def updateSpotPriceSensors():
    """Update spot price sensors based on future spot prices"""

    price_dictionaries = sensor.nordpool_spotprice_no_transfer.raw_today

    prices = [d['value'] for d in price_dictionaries if 'value' in d]
    price_average_today = sum(prices) / len(prices)
    price_min_today = min(prices)
    price_max_today = max(prices)
    price_25_percent_today = (price_average_today + price_min_today) / 2
    price_75_percent_today = (price_average_today + price_max_today) / 2

    if sensor.nordpool_spotprice_no_transfer.tomorrow_valid:
        price_dictionaries += sensor.nordpool_spotprice_no_transfer.raw_tomorrow

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

    price_current = sensor.nordpool_spotprice_no_transfer.current_price

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