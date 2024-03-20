from datetime import datetime, timedelta
from HassService import _get_history, _get_statistic


@service
def runSolarSavingsHistoricalData(action=None, id=None):
    """Service to manually run solar savings calculation for historical data"""
    log.warning(f"This currently does nothing")


@time_trigger("cron(2 * * * *)")
def calculateSolarSavingsLastHour():
    """Calculate the savings from solar panels"""
    buy_price_entity_id = 'sensor.electricity_buy_price'
    sell_price_entity_id = 'sensor.electricity_sell_price'
    exported_kwh_total_entity_id = 'sensor.power_meter_exported'
    inverter_yield_kwh_total_entity_id = 'sensor.inverter_total_yield'
    solar_savings_entity_id = 'input_number.solar_savings'

    last_hour_start = datetime.now() - timedelta(hours=1)
    last_hour_start = last_hour_start.replace(minute=0, second=0, microsecond=0)

    last_hour_end = datetime.now()
    last_hour_end = last_hour_end.replace(minute=0, second=0, microsecond=0)

    last_hour_prices = _get_statistic(last_hour_start, last_hour_end, [buy_price_entity_id, sell_price_entity_id], "hour", ['state'])

    last_hour_buy_price = float(last_hour_prices[buy_price_entity_id][0]['state'])
    last_hour_sell_price = float(last_hour_prices[sell_price_entity_id][0]['state'])

    last_hour_energy = _get_history(last_hour_start, last_hour_end, [exported_kwh_total_entity_id, inverter_yield_kwh_total_entity_id], True, False, False, True)

    last_hour_exported = float(last_hour_energy[exported_kwh_total_entity_id][-1].state) - float(last_hour_energy[exported_kwh_total_entity_id][0].state)
    last_hour_produced = float(last_hour_energy[inverter_yield_kwh_total_entity_id][-1].state) - float(last_hour_energy[inverter_yield_kwh_total_entity_id][0].state)

    savings_last_hour = (last_hour_buy_price * (last_hour_produced - last_hour_exported) + last_hour_sell_price * last_hour_exported)/100.0

    current_solar_savings = float(state.get(solar_savings_entity_id))
    attributes = state.getattr(solar_savings_entity_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(solar_savings_entity_id+'.state_class', 'total')
        state.setattr(solar_savings_entity_id+'.device_class', 'monetary')
        state.setattr(solar_savings_entity_id+'.unit_of_measurement', '€')
    state.set(solar_savings_entity_id, current_solar_savings+savings_last_hour)


@time_trigger("cron(2 * * * *)")
def calculateCarChargingCostLastHour():
    """Calculate the car charging costs"""
    buy_price_entity_id = 'sensor.electricity_buy_price'
    tesla_wallconnector_energy_entity_id = 'sensor.tesla_wall_connector_energy'
    purchased_kwh_total_entity_id = 'sensor.power_meter_consumption'
    car_charge_cost_without_solar_entity_id = 'input_number.car_charge_without_solar'
    car_charge_cost_with_solar_entity_id = 'input_number.car_charge_with_solar'

    last_hour_start = datetime.now() - timedelta(hours=1)
    last_hour_start = last_hour_start.replace(minute=0, second=0, microsecond=0)

    last_hour_end = datetime.now()
    last_hour_end = last_hour_end.replace(minute=0, second=0, microsecond=0)

    last_hour_prices = _get_statistic(last_hour_start, last_hour_end, [buy_price_entity_id], "hour", ['state'])

    last_hour_buy_price = float(last_hour_prices[buy_price_entity_id][0]['state'])

    last_hour_history = _get_history(last_hour_start, last_hour_end, [tesla_wallconnector_energy_entity_id, purchased_kwh_total_entity_id], True, False, False, True)

    last_hour_charged_kwh = (float(last_hour_history[tesla_wallconnector_energy_entity_id][-1].state) - float(last_hour_history[tesla_wallconnector_energy_entity_id][0].state))/1000.0
    last_hour_purchased_kwh = float(last_hour_history[purchased_kwh_total_entity_id][-1].state) - float(last_hour_history[purchased_kwh_total_entity_id][0].state)

    car_charge_cost_without_solar_last_hour = (last_hour_buy_price * last_hour_charged_kwh)/100.0

    car_charge_cost_with_solar_last_hour = 0.0
    if last_hour_purchased_kwh < 0.001:
        if last_hour_purchased_kwh < last_hour_charged_kwh:
            car_charge_cost_with_solar_last_hour = (last_hour_buy_price * (last_hour_charged_kwh - last_hour_purchased_kwh))/100.0
        else:
            car_charge_cost_with_solar_last_hour = (last_hour_buy_price * last_hour_charged_kwh)/100.0

    current_car_charge_cost_without_solar = float(state.get(car_charge_cost_without_solar_entity_id))
    attributes = state.getattr(car_charge_cost_without_solar_entity_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(car_charge_cost_without_solar_entity_id+'.state_class', 'total')
        state.setattr(car_charge_cost_without_solar_entity_id+'.device_class', 'monetary')
        state.setattr(car_charge_cost_without_solar_entity_id+'.unit_of_measurement', '€')
    state.set(car_charge_cost_without_solar_entity_id, current_car_charge_cost_without_solar+car_charge_cost_without_solar_last_hour)

    current_car_charge_cost_with_solar = float(state.get(car_charge_cost_with_solar_entity_id))
    attributes = state.getattr(car_charge_cost_with_solar_entity_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(car_charge_cost_with_solar_entity_id+'.state_class', 'total')
        state.setattr(car_charge_cost_with_solar_entity_id+'.device_class', 'monetary')
        state.setattr(car_charge_cost_with_solar_entity_id+'.unit_of_measurement', '€')
    state.set(car_charge_cost_with_solar_entity_id, current_car_charge_cost_with_solar+car_charge_cost_with_solar_last_hour)


@time_trigger("cron(2 * * * *)")
def calculateHeatPumpCostLastHour():
    """Calculate the heat pump costs"""
    buy_price_entity_id = 'sensor.electricity_buy_price'
    purchased_kwh_total_entity_id = 'sensor.power_meter_consumption'
    nibe_energy_used_last_hour_kwh_total_entity_id = 'sensor.nibe_energy_used_last_hour'
    heat_pump_cost_without_solar_entity_id = 'input_number.heat_pump_cost_without_solar'
    heat_pump_cost_with_solar_entity_id = 'input_number.heat_pump_cost_with_solar'

    last_hour_start = datetime.now() - timedelta(hours=1)
    last_hour_start = last_hour_start.replace(minute=0, second=0, microsecond=0)

    last_hour_end = datetime.now()
    last_hour_end = last_hour_end.replace(minute=0, second=0, microsecond=0)

    last_hour_prices = _get_statistic(last_hour_start, last_hour_end, [buy_price_entity_id], "hour", ['state'])

    last_hour_buy_price = float(last_hour_prices[buy_price_entity_id][0]['state'])

    last_hour_history = _get_history(last_hour_start, last_hour_end, [purchased_kwh_total_entity_id], True, False, False, True)

    last_hour_purchased = float(last_hour_history[purchased_kwh_total_entity_id][-1].state) - float(last_hour_history[purchased_kwh_total_entity_id][0].state)
    last_hour_heat_pump_used_kwh = float(state.get(nibe_energy_used_last_hour_kwh_total_entity_id))

    heat_pump_cost_without_solar_last_hour = (last_hour_buy_price * last_hour_heat_pump_used_kwh)/100.0

    heat_pump_cost_with_solar_last_hour = 0.0
    if last_hour_purchased < 0.001:
        if last_hour_purchased < last_hour_heat_pump_used_kwh:
            heat_pump_cost_with_solar_last_hour = (last_hour_buy_price * (last_hour_heat_pump_used_kwh - last_hour_purchased))/100.0
        else:
            heat_pump_cost_with_solar_last_hour = (last_hour_buy_price * last_hour_heat_pump_used_kwh)/100.0

    current_heat_pump_cost_without_solar = float(state.get(heat_pump_cost_without_solar_entity_id))
    attributes = state.getattr(heat_pump_cost_without_solar_entity_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(heat_pump_cost_without_solar_entity_id+'.state_class', 'total')
        state.setattr(heat_pump_cost_without_solar_entity_id+'.device_class', 'monetary')
        state.setattr(heat_pump_cost_without_solar_entity_id+'.unit_of_measurement', '€')
    state.set(heat_pump_cost_without_solar_entity_id, current_heat_pump_cost_without_solar+heat_pump_cost_without_solar_last_hour)

    current_heat_pump_cost_with_solar = float(state.get(heat_pump_cost_with_solar_entity_id))
    attributes = state.getattr(heat_pump_cost_with_solar_entity_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(heat_pump_cost_with_solar_entity_id+'.state_class', 'total')
        state.setattr(heat_pump_cost_with_solar_entity_id+'.device_class', 'monetary')
        state.setattr(heat_pump_cost_with_solar_entity_id+'.unit_of_measurement', '€')
    state.set(heat_pump_cost_with_solar_entity_id, current_heat_pump_cost_with_solar+heat_pump_cost_with_solar_last_hour)
