from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timezone, timedelta


async def _get_statistic(
    start_time: datetime,
    end_time: datetime | None,
    statistic_ids: list[str] | None,
    period: Literal["5minute", "day", "hour", "week", "month"],
    types: set[Literal["last_reset", "max", "mean", "min", "state", "sum"]]):

    # This is probably not needed so leaving it commented out
    #start_time = start_time.astimezone(timezone.utc)
    #end_time = end_time.astimezone(timezone.utc)

    return(await get_instance(hass).async_add_executor_job(statistics_during_period, hass, start_time, end_time, statistic_ids, period, None, types))


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

    return (await get_instance(hass).async_add_executor_job(get_significant_states, hass, start_time, end_time, entity_ids, None, include_start_time_state, significant_changes_only, minimal_response, no_attributes, False))


def _add_value_to_sensor(value, sensor_id):
    current_value = float(state.get(sensor_id))
    attributes = state.getattr(sensor_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(sensor_id+'.state_class', 'total')
        state.setattr(sensor_id+'.device_class', 'monetary')
        state.setattr(sensor_id+'.unit_of_measurement', '€')
    state.set(sensor_id, current_value+value)


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
    buy_price_entity_id = 'sensor.electricity_buy_price'
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

    # Start and end of last hour
    last_hour_start = datetime.now() - timedelta(hours=1)
    last_hour_start = last_hour_start.replace(minute=0, second=0, microsecond=0)
    last_hour_end = datetime.now()
    last_hour_end = last_hour_end.replace(minute=0, second=0, microsecond=0)

    # Spot prices
    last_hour_prices = _get_statistic(last_hour_start, last_hour_end, [buy_price_entity_id, sell_price_entity_id], "hour", ['state'])
    last_hour_buy_price = float(last_hour_prices[buy_price_entity_id][0]['state'])
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
    _add_value_to_sensor(overall_savings_last_hour, solar_savings_entity_id)

    # Car charge cost and savings
    car_charge_cost_without_solar_last_hour = _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh)
    car_charge_cost_with_solar_last_hour = _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, car_share_of_purchase)
    _add_value_to_sensor(car_charge_cost_without_solar_last_hour, car_charge_cost_without_solar_entity_id)
    _add_value_to_sensor(car_charge_cost_with_solar_last_hour, car_charge_cost_with_solar_entity_id)

    # Heat pump cost and savings
    heat_pump_cost_without_solar_last_hour = _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh)
    heat_pump_cost_with_solar_last_hour = _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, heat_pump_share_of_purchase)
    _add_value_to_sensor(heat_pump_cost_without_solar_last_hour, heat_pump_cost_without_solar_entity_id)
    _add_value_to_sensor(heat_pump_cost_with_solar_last_hour, heat_pump_cost_with_solar_entity_id)
