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
def runSolarSavingsHistoricalData(action=None, id=None):
    """Service to manually run solar savings calculation for historical data"""
    log.warning(f"This currently does nothing")


@time_trigger("cron(1 * * * *)")
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
