from datetime import datetime, timedelta
from HassService import _get_history, _get_statistic


def _add_value_to_sensor(value, sensor_id):
    current_value = float(state.get(sensor_id))
    attributes = state.getattr(sensor_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(sensor_id+'.state_class', 'total')
        state.setattr(sensor_id+'.device_class', 'monetary')
        state.setattr(sensor_id+'.unit_of_measurement', '€')
    state.set(sensor_id, current_value+value)


def _calculate_overall_solar_savings_last_hour(last_hour_exported, last_hour_produced, last_hour_buy_price, last_hour_sell_price):
    overall_savings_last_hour = (last_hour_buy_price * (last_hour_produced - last_hour_exported) + last_hour_sell_price * last_hour_exported)/100.0
    return overall_savings_last_hour


def _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh):
    car_charge_cost_without_solar_last_hour = (last_hour_buy_price * last_hour_charged_kwh)/100.0
    return car_charge_cost_without_solar_last_hour


def _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh, last_hour_purchased_kwh):
    car_charge_cost_with_solar_last_hour = 0.0
    if last_hour_purchased_kwh < 0.001:
        if last_hour_purchased_kwh < last_hour_charged_kwh:
            car_charge_cost_with_solar_last_hour = (last_hour_buy_price * (last_hour_charged_kwh - last_hour_purchased_kwh))/100.0
        else:
            car_charge_cost_with_solar_last_hour = (last_hour_buy_price * last_hour_charged_kwh)/100.0
    return car_charge_cost_with_solar_last_hour


def _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh):
    return (last_hour_buy_price * last_hour_heat_pump_used_kwh)/100.0


def _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, last_hour_heat_pump_used_kwh):
    heat_pump_cost_with_solar_last_hour = 0.0
    if last_hour_purchased_kwh < 0.001:
        if last_hour_purchased_kwh < last_hour_heat_pump_used_kwh:
            heat_pump_cost_with_solar_last_hour = (last_hour_buy_price * (last_hour_heat_pump_used_kwh - last_hour_purchased_kwh))/100.0
        else:
            heat_pump_cost_with_solar_last_hour = (last_hour_buy_price * last_hour_heat_pump_used_kwh)/100.0
    return heat_pump_cost_with_solar_last_hour


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

    # Overall solar savings
    last_hour_exported = float(last_hour_history[exported_kwh_total_entity_id][-1].state) - float(last_hour_history[exported_kwh_total_entity_id][0].state)
    last_hour_produced = float(last_hour_history[inverter_yield_kwh_total_entity_id][-1].state) - float(last_hour_history[inverter_yield_kwh_total_entity_id][0].state)

    overall_savings_last_hour = _calculate_overall_solar_savings_last_hour(last_hour_exported, last_hour_produced, last_hour_buy_price, last_hour_sell_price)
    _add_value_to_sensor(overall_savings_last_hour, solar_savings_entity_id)

    # Car charge cost and savings
    last_hour_charged_kwh = (float(last_hour_history[tesla_wallconnector_energy_entity_id][-1].state) - float(last_hour_history[tesla_wallconnector_energy_entity_id][0].state))/1000.0
    last_hour_purchased_kwh = float(last_hour_history[purchased_kwh_total_entity_id][-1].state) - float(last_hour_history[purchased_kwh_total_entity_id][0].state)

    car_charge_cost_without_solar_last_hour = _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh)
    car_charge_cost_with_solar_last_hour = _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh, last_hour_purchased_kwh)
    _add_value_to_sensor(car_charge_cost_without_solar_last_hour, car_charge_cost_without_solar_entity_id)
    _add_value_to_sensor(car_charge_cost_with_solar_last_hour, car_charge_cost_with_solar_entity_id)

    # Heat pump cost and savings
    last_hour_heat_pump_used_kwh = float(state.get(nibe_energy_used_last_hour_kwh_total_entity_id))

    heat_pump_cost_without_solar_last_hour = _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh)

    heat_pump_cost_with_solar_last_hour = _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, last_hour_heat_pump_used_kwh)

    _add_value_to_sensor(heat_pump_cost_without_solar_last_hour, heat_pump_cost_without_solar_entity_id)
    _add_value_to_sensor(heat_pump_cost_with_solar_last_hour, heat_pump_cost_with_solar_entity_id)
