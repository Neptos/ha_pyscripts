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
def runHeatPumpCostHistoricalData(action=None, id=None):
    """Service to manually run heat pump cost calculation for historical data"""
    log.warning(f"This currently does nothing")


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
