from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timedelta, timezone

async def _get_statistic(
    start_time: datetime,
    end_time: datetime | None,
    statistic_ids: list[str] | None,
    period: Literal["5minute", "day", "hour", "week", "month"],
    types: set[Literal["last_reset", "max", "mean", "min", "state", "sum"]]):

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

@service
def runCarChargingCostHistoricalData(action=None, id=None):
    """Service to manually run car charing cost calculation for historical data"""
    log.warning(f"This currently does nothing")


@time_trigger("cron(1 * * * *)")
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
