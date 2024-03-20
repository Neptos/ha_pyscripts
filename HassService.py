from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timezone


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
