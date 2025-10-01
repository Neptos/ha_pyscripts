from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timezone, timedelta
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


async def _get_history(
        start_time: dt,
        end_time: dt,
        entity_ids: list[str],
        include_start_time_state: bool,
        significant_changes_only: bool,
        minimal_response: bool,
        no_attributes: bool):

    start_time = start_time.astimezone(timezone.utc)
    end_time = end_time.astimezone(timezone.utc)

    counter = 0
    task = get_instance(hass).async_add_executor_job(get_significant_states, hass, start_time, end_time, entity_ids, None, include_start_time_state, significant_changes_only, minimal_response, no_attributes, False)
    while not task.done():
        asyncio.sleep(1)
        counter = counter + 1
        if counter > 10:
            break
    return task.result()

#    return (await get_instance(hass).async_add_executor_job(get_significant_states, hass, start_time, end_time, entity_ids, None, include_start_time_state, significant_changes_only, minimal_response, no_attributes, False))


def _sum_value_to_sensor(value, sensor_id):
    current_value = float(state.get(sensor_id))
    attributes = state.getattr(sensor_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(sensor_id+'.state_class', 'total')
        state.setattr(sensor_id+'.device_class', 'monetary')
        state.setattr(sensor_id+'.unit_of_measurement', '€')
    state.set(sensor_id, current_value+value)


def _calculate_weighted_average_price(start_time, end_time, price_entity_id, consumption_entity_id):
    """Calculate consumption-weighted average price for a time period.

    Uses 15-minute intervals to match Nordpool's pricing structure. If insufficient
    data is available for weighted calculation, falls back to simple average.

    Returns weighted average price in same units as price_entity_id.
    """
    # Fetch 15-minute price statistics (using 5minute period as HA doesn't have 15minute)
    try:
        price_stats = _get_statistic(start_time, end_time, [price_entity_id], "5minute", ['state'])
        if not price_stats or price_entity_id not in price_stats:
            # Fallback to hourly if 5minute fails
            price_stats = _get_statistic(start_time, end_time, [price_entity_id], "hour", ['state'])
            if not price_stats or price_entity_id not in price_stats:
                return None

        prices = price_stats[price_entity_id]

        # If we only got one price point, return it directly
        if len(prices) == 1:
            return float(prices[0]['state'])

        # Fetch consumption history at high frequency for granular calculation
        consumption_history = _get_history(start_time, end_time, [consumption_entity_id], True, False, False, True)

        if not consumption_history or consumption_entity_id not in consumption_history or len(consumption_history[consumption_entity_id]) < 2:
            # Fallback to simple average if consumption history unavailable
            return sum([float(p['state']) for p in prices]) / len(prices)

        history = consumption_history[consumption_entity_id]

        # Calculate consumption-weighted average
        total_weighted_price = 0.0
        total_consumption = 0.0

        # Group history points into 15-minute buckets aligned with price intervals
        for i, price_record in enumerate(prices):
            price_value = float(price_record['state'])
            price_start = price_record.get('start')

            if price_start:
                # Find consumption during this price interval
                # Handle string, float (Unix timestamp), and datetime objects
                if isinstance(price_start, str):
                    interval_start = datetime.fromisoformat(price_start.replace('Z', '+00:00'))
                elif isinstance(price_start, (int, float)):
                    interval_start = datetime.fromtimestamp(price_start, tz=timezone.utc)
                else:
                    interval_start = price_start
                interval_end = interval_start + timedelta(minutes=15)

                # Find history points within this interval
                interval_consumption = 0.0
                for j in range(len(history) - 1):
                    point_time = history[j].last_updated
                    if interval_start <= point_time < interval_end:
                        # Calculate consumption between this point and next
                        consumption_delta = float(history[j+1].state) - float(history[j].state)
                        if consumption_delta > 0:  # Only count positive consumption
                            interval_consumption += consumption_delta

                if interval_consumption > 0:
                    total_weighted_price += price_value * interval_consumption
                    total_consumption += interval_consumption

        # Return weighted average, or simple average as fallback
        if total_consumption > 0:
            return total_weighted_price / total_consumption
        else:
            # No consumption detected, return simple average of prices
            return sum([float(p['state']) for p in prices]) / len(prices)

    except Exception as e:
        log.warning(f"Error calculating weighted price, using simple average: {e}")
        # Final fallback: try to get any price data and average it
        try:
            price_stats = _get_statistic(start_time, end_time, [price_entity_id], "hour", ['state'])
            if price_stats and price_entity_id in price_stats:
                prices = price_stats[price_entity_id]
                return sum([float(p['state']) for p in prices]) / len(prices)
        except:
            pass
        return None


def _calculate_overall_solar_savings_last_hour(last_hour_exported_kwh, last_hour_produced_kwh, last_hour_buy_price, last_hour_sell_price):
    return (last_hour_buy_price * (last_hour_produced_kwh - last_hour_exported_kwh) + last_hour_sell_price * last_hour_exported_kwh)/100.0


def _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh):
    return (last_hour_buy_price * last_hour_charged_kwh)/100.0


def _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, car_share_of_purchase):
    return (last_hour_buy_price * last_hour_purchased_kwh * car_share_of_purchase)/100.0


def _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh):
    return (last_hour_buy_price * last_hour_heat_pump_used_kwh)/100.0


def _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, heat_pump_share_of_purchase):
    return (last_hour_buy_price * last_hour_purchased_kwh * heat_pump_share_of_purchase)/100.0


@time_trigger("cron(2 * * * *)")
def calculateSolarSavingsLastHour():
    """Calculate the savings from solar panels during the previous hour"""
    # Read entities
    buy_price_entity_id = 'sensor.nordpool_kwh_fi_eur_3_10_0'
    sell_price_entity_id = 'sensor.electricity_sell_price'
    tesla_wallconnector_energy_entity_id = 'sensor.tesla_wall_connector_energy'
    purchased_kwh_total_entity_id = 'sensor.power_meter_consumption'
    exported_kwh_total_entity_id = 'sensor.power_meter_exported'
    inverter_yield_kwh_total_entity_id = 'sensor.inverter_total_yield'
    nibe_energy_used_last_hour_kwh_total_entity_id = 'sensor.nibe_energy_used_last_hour'

    # Write entities
    # These have to be created as helpers in HA
    solar_savings_entity_id = 'input_number.solar_savings'
    car_charge_cost_without_solar_entity_id = 'input_number.car_charge_without_solar'
    car_charge_cost_with_solar_entity_id = 'input_number.car_charge_with_solar'
    heat_pump_cost_without_solar_entity_id = 'input_number.heat_pump_cost_without_solar'
    heat_pump_cost_with_solar_entity_id = 'input_number.heat_pump_cost_with_solar'
    heat_pump_consumed_kwh_entity_id = 'input_number.heat_pump_consumed_kwh'

    # Start and end of last hour
    last_hour_start = datetime.now() - timedelta(hours=1)
    last_hour_start = last_hour_start.replace(minute=0, second=0, microsecond=0)
    last_hour_end = datetime.now()
    last_hour_end = last_hour_end.replace(minute=0, second=0, microsecond=0)

    # Calculate consumption-weighted spot prices for 15-minute intervals
    # Uses purchased electricity to weight buy price, exported to weight sell price
    last_hour_buy_price = _calculate_weighted_average_price(
        last_hour_start, last_hour_end, buy_price_entity_id, purchased_kwh_total_entity_id)
    last_hour_sell_price = _calculate_weighted_average_price(
        last_hour_start, last_hour_end, sell_price_entity_id, exported_kwh_total_entity_id)

    # Fallback to simple average if weighted calculation failed
    if last_hour_buy_price is None or last_hour_sell_price is None:
        last_hour_prices = _get_statistic(last_hour_start, last_hour_end, [buy_price_entity_id, sell_price_entity_id], "hour", ['state'])
        if last_hour_buy_price is None:
            last_hour_buy_price = float(last_hour_prices[buy_price_entity_id][0]['state'])
        if last_hour_sell_price is None:
            last_hour_sell_price = float(last_hour_prices[sell_price_entity_id][0]['state'])

    # Fetch all data from history
    last_hour_history = _get_history(last_hour_start, last_hour_end, [exported_kwh_total_entity_id, inverter_yield_kwh_total_entity_id, tesla_wallconnector_energy_entity_id, purchased_kwh_total_entity_id], True, False, False, True)

    # Calculate energy usages last hour
    last_hour_exported_kwh = float(last_hour_history[exported_kwh_total_entity_id][-1].state) - float(last_hour_history[exported_kwh_total_entity_id][0].state)
    last_hour_produced_kwh = float(last_hour_history[inverter_yield_kwh_total_entity_id][-1].state) - float(last_hour_history[inverter_yield_kwh_total_entity_id][0].state)
    last_hour_purchased_kwh = float(last_hour_history[purchased_kwh_total_entity_id][-1].state) - float(last_hour_history[purchased_kwh_total_entity_id][0].state)
    last_hour_charged_kwh = (float(last_hour_history[tesla_wallconnector_energy_entity_id][-1].state) - float(last_hour_history[tesla_wallconnector_energy_entity_id][0].state))/1000.0
    last_hour_heat_pump_used_kwh = float(state.get(nibe_energy_used_last_hour_kwh_total_entity_id))
    last_hour_consumed_solar = last_hour_produced_kwh - last_hour_exported_kwh

    # Correct for kWh purchased exchange for kWh exported during the hour
    if last_hour_exported_kwh > 0.001 and last_hour_purchased_kwh > 0.001:
        if last_hour_purchased_kwh >= last_hour_exported_kwh:
            last_hour_purchased_kwh = last_hour_purchased_kwh - last_hour_exported_kwh
            last_hour_consumed_solar = last_hour_consumed_solar + last_hour_exported_kwh
            last_hour_exported_kwh = 0.0
        else:
            last_hour_exported_kwh = last_hour_exported_kwh - last_hour_purchased_kwh
            last_hour_consumed_solar = last_hour_consumed_solar + last_hour_purchased_kwh
            last_hour_purchased_kwh = 0.0

    car_share_of_purchase = 0.0
    heat_pump_share_of_purchase = 0.0
    if last_hour_purchased_kwh > 0.001:
        # Consumers share purchased cost based on their % usage of total
        car_share_of_purchase = last_hour_charged_kwh / (last_hour_purchased_kwh + last_hour_produced_kwh)
        heat_pump_share_of_purchase = last_hour_heat_pump_used_kwh / (last_hour_purchased_kwh + last_hour_produced_kwh)

    # Overall solar savings
    overall_savings_last_hour = _calculate_overall_solar_savings_last_hour(last_hour_exported_kwh, last_hour_produced_kwh, last_hour_buy_price, last_hour_sell_price)
    _sum_value_to_sensor(overall_savings_last_hour, solar_savings_entity_id)

    # Car charge cost and savings
    car_charge_cost_without_solar_last_hour = _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh)
    car_charge_cost_with_solar_last_hour = _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, car_share_of_purchase)
    _sum_value_to_sensor(car_charge_cost_without_solar_last_hour, car_charge_cost_without_solar_entity_id)
    _sum_value_to_sensor(car_charge_cost_with_solar_last_hour, car_charge_cost_with_solar_entity_id)

    # Heat pump cost and savings
    heat_pump_cost_without_solar_last_hour = _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh)
    heat_pump_cost_with_solar_last_hour = _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, heat_pump_share_of_purchase)
    _sum_value_to_sensor(heat_pump_cost_without_solar_last_hour, heat_pump_cost_without_solar_entity_id)
    _sum_value_to_sensor(heat_pump_cost_with_solar_last_hour, heat_pump_cost_with_solar_entity_id)

    # Heat pump all time consumption
    _sum_value_to_sensor(last_hour_heat_pump_used_kwh, heat_pump_consumed_kwh_entity_id)
