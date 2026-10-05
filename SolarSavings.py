from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.statistics import statistics_during_period
from datetime import datetime, timezone, timedelta
import asyncio

def _get_statistic(
    start_time,
    end_time,
    statistic_ids,
    period,
    types):

    counter = 0
    task = get_instance(hass).async_add_executor_job(statistics_during_period, hass, start_time, end_time, statistic_ids, period, None, types)
    while not task.done():
        asyncio.sleep(1)
        counter = counter + 1
        if counter > 10:
            break
    if not task.done():
        log.warning("Recorder statistics query timed out")
        return None
    return task.result()


async def _get_history(
        start_time,
        end_time,
        entity_ids,
        include_start_time_state,
        significant_changes_only,
        minimal_response,
        no_attributes):

    start_time = start_time.astimezone(timezone.utc)
    end_time = end_time.astimezone(timezone.utc)

    counter = 0
    task = get_instance(hass).async_add_executor_job(get_significant_states, hass, start_time, end_time, entity_ids, None, include_start_time_state, significant_changes_only, minimal_response, no_attributes, False)
    while not task.done():
        asyncio.sleep(1)
        counter = counter + 1
        if counter > 10:
            break
    if not task.done():
        log.warning("Recorder history query timed out")
        return None
    return task.result()


def _delta_from_history(rows):
    """Return last - first of numeric history states, ignoring unavailable/unknown.

    Filters out rows whose state is 'unavailable'/'unknown' or non-float-parseable.
    Returns 0.0 when fewer than 2 valid points remain. Warns only when 2+ rows
    were present but got filtered out as invalid (a genuine data-quality issue);
    an idle sensor that produced 0-1 rows this hour is normal and stays silent.
    """
    valid = []
    for row in rows:
        if row.state in ('unavailable', 'unknown'):
            continue
        try:
            valid.append(float(row.state))
        except (ValueError, TypeError):
            continue
    if len(valid) < 2:
        if len(rows) >= 2:
            log.warning("History delta has fewer than 2 valid points, using 0.0 delta")
        return 0.0
    return valid[-1] - valid[0]


def _share_of_purchase(consumer_kwh, purchased_kwh, produced_kwh):
    """Consumer's proportional share of purchased energy (0.0 when nothing purchased)."""
    if purchased_kwh > 0.001:
        return consumer_kwh / (purchased_kwh + produced_kwh)
    return 0.0


def _sum_value_to_sensor(value, sensor_id):
    try:
        current_value = float(state.get(sensor_id))
    except (ValueError, TypeError):
        current_value = 0.0
        log.warning(f"{sensor_id} non-numeric, starting from 0")
    attributes = state.getattr(sensor_id)
    if attributes is None or 'device_class' not in attributes.keys():
        state.setattr(sensor_id+'.state_class', 'total')
        state.setattr(sensor_id+'.device_class', 'monetary')
        state.setattr(sensor_id+'.unit_of_measurement', '€')
    state.set(sensor_id, current_value+value)


def _calculate_weighted_average_price(start_time, end_time, price_entity_id, consumption_entity_id, consumption_history=None):
    """Calculate consumption-weighted average price for a time period.

    Uses 15-minute intervals to match Nordpool's pricing structure. If insufficient
    data is available for weighted calculation, falls back to simple average.

    When `consumption_history` (a per-entity row list) is passed, it is used
    directly and no history query is issued (the caller can share a single
    combined fetch). When None, this function fetches the history itself.

    Returns weighted average price in same units as price_entity_id.
    """
    # Fetch 15-minute price statistics (using 5minute period as HA doesn't have 15minute)
    try:
        period_minutes = 5
        price_stats = _get_statistic(start_time, end_time, [price_entity_id], "5minute", ['state'])
        if not price_stats or price_entity_id not in price_stats:
            # Fallback to hourly if 5minute fails
            period_minutes = 60
            price_stats = _get_statistic(start_time, end_time, [price_entity_id], "hour", ['state'])
            if not price_stats or price_entity_id not in price_stats:
                return None

        prices = price_stats[price_entity_id]

        # If we only got one price point, return it directly
        if len(prices) == 1:
            return float(prices[0]['state'])

        # Consumption history: use the caller-supplied row list, else fetch.
        if consumption_history is None:
            fetched = _get_history(start_time, end_time, [consumption_entity_id], True, False, False, True)
            history = fetched.get(consumption_entity_id) if fetched else None
        else:
            history = consumption_history

        if not history or len(history) < 2:
            # Fallback to simple average if consumption history unavailable
            return sum([float(p['state']) for p in prices]) / len(prices)

        # Single-pass consumption-weighted average (L15).
        # Precompute positive consumption deltas ONCE (each state parsed once),
        # keyed by the delta's start timestamp. Both lists are time-ordered, so a
        # forward-moving index attributes each delta to exactly one price interval.
        deltas = []
        for j in range(len(history) - 1):
            try:
                prev_val = float(history[j].state)
                next_val = float(history[j + 1].state)
            except (ValueError, TypeError):
                continue
            consumption_delta = next_val - prev_val
            if consumption_delta > 0:
                deltas.append((history[j].last_updated, consumption_delta))

        total_weighted_price = 0.0
        total_consumption = 0.0
        delta_idx = 0

        for price_record in prices:
            price_value = float(price_record['state'])
            price_start = price_record.get('start')
            if not price_start:
                continue

            # Handle string, float (Unix timestamp), and datetime objects
            if isinstance(price_start, str):
                interval_start = datetime.fromisoformat(price_start.replace('Z', '+00:00'))
            elif isinstance(price_start, (int, float)):
                interval_start = datetime.fromtimestamp(price_start, tz=timezone.utc)
            else:
                interval_start = price_start
            interval_end = interval_start + timedelta(minutes=period_minutes)

            # Skip deltas that fall before this interval (they belong to an
            # earlier interval that had no match, or were already consumed).
            while delta_idx < len(deltas) and deltas[delta_idx][0] < interval_start:
                delta_idx += 1

            # Accumulate deltas within [interval_start, interval_end).
            interval_consumption = 0.0
            while delta_idx < len(deltas) and deltas[delta_idx][0] < interval_end:
                interval_consumption += deltas[delta_idx][1]
                delta_idx += 1

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


def _calculate_overall_solar_savings_last_hour(solar_to_house_kwh, solar_to_grid_kwh, solar_to_battery_kwh, buy_price, sell_price):
    """EUR saved by the panels this hour: direct use avoids the buy price; the
    rest is valued at the sell price whether exported now or stored. The extra
    value of stored solar is booked in the battery metric, so nothing is counted twice."""
    return (buy_price * solar_to_house_kwh + sell_price * (solar_to_grid_kwh + solar_to_battery_kwh)) / 100.0


def _calculate_battery_savings_last_hour(battery_to_house_kwh, battery_to_grid_kwh, grid_to_battery_kwh, solar_to_battery_kwh, buy_price, sell_price):
    """EUR net effect of the battery this hour: discharge to the house avoids the
    buy price, discharge to grid earns the sell price, grid charging costs the
    buy price, and absorbed solar is charged at the sell price it displaced.
    Negative in charging hours, positive in discharging hours; the running sum
    is the battery's net benefit (Elisa's arbitrage plus stored-solar uplift)."""
    return (buy_price * battery_to_house_kwh + sell_price * battery_to_grid_kwh
            - buy_price * grid_to_battery_kwh - sell_price * solar_to_battery_kwh) / 100.0


def _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh):
    return (last_hour_buy_price * last_hour_charged_kwh)/100.0


def _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, car_share_of_purchase):
    return (last_hour_buy_price * last_hour_purchased_kwh * car_share_of_purchase)/100.0


def _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh):
    return (last_hour_buy_price * last_hour_heat_pump_used_kwh)/100.0


def _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, last_hour_purchased_kwh, heat_pump_share_of_purchase):
    return (last_hour_buy_price * last_hour_purchased_kwh * heat_pump_share_of_purchase)/100.0


def _energy_totals_available(*entity_ids):
    """True when every cumulative energy sensor currently has a numeric state.

    pyscript's state.get raises NameError for an entity that does not exist
    at all (e.g. its integration is disabled), so that counts as unavailable.
    """
    for entity_id in entity_ids:
        try:
            float(state.get(entity_id))
        except (ValueError, TypeError, NameError):
            return False
    return True


_WARNED = {}


def _helper_exists(entity_id):
    """True when the input_number helper exists; warns once per helper if not."""
    try:
        state.get(entity_id)
        return True
    except NameError:
        if not _WARNED.get(entity_id):
            log.warning(f"Helper {entity_id} missing, create it in HA to track this metric")
            _WARNED[entity_id] = True
        return False


@time_trigger("cron(2 * * * *)")
def calculateSolarSavingsLastHour():
    """Book last hour's solar and battery value from the Elisa Kotiakku flow totals."""
    # Read entities
    buy_price_entity_id = 'sensor.nordpool_kwh_fi_eur_3_10_0'
    sell_price_entity_id = 'sensor.electricity_sell_price'
    tesla_wallconnector_energy_entity_id = 'sensor.tesla_wall_connector_energy'
    nibe_energy_used_last_hour_kwh_total_entity_id = 'sensor.nibe_energy_used_last_hour'
    # Elisa Kotiakku integration flow totals (kWh, total_increasing, 5-min updates)
    grid_import_kwh_total_entity_id = 'sensor.kotiakku_total_grid_import_kwh'
    grid_export_kwh_total_entity_id = 'sensor.kotiakku_total_grid_export_kwh'
    solar_to_house_kwh_total_entity_id = 'sensor.kotiakku_solar_to_house_kwh'
    solar_to_grid_kwh_total_entity_id = 'sensor.kotiakku_solar_to_grid_kwh'
    solar_to_battery_kwh_total_entity_id = 'sensor.kotiakku_solar_to_battery_kwh'
    grid_to_house_kwh_total_entity_id = 'sensor.kotiakku_grid_to_house_kwh'
    grid_to_battery_kwh_total_entity_id = 'sensor.kotiakku_grid_to_battery_kwh'
    battery_to_house_kwh_total_entity_id = 'sensor.kotiakku_battery_to_house_kwh'
    battery_to_grid_kwh_total_entity_id = 'sensor.kotiakku_battery_to_grid_kwh'
    flow_entity_ids = [
        solar_to_house_kwh_total_entity_id, solar_to_grid_kwh_total_entity_id, solar_to_battery_kwh_total_entity_id,
        grid_to_house_kwh_total_entity_id, grid_to_battery_kwh_total_entity_id,
        battery_to_house_kwh_total_entity_id, battery_to_grid_kwh_total_entity_id,
    ]

    # Write entities
    # These have to be created as helpers in HA
    solar_savings_entity_id = 'input_number.solar_savings'
    battery_savings_entity_id = 'input_number.battery_savings'
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

    # Fetch all energy history once; the weighted-price calls reuse the
    # grid import/export row lists instead of issuing more recorder queries.
    history_entity_ids = flow_entity_ids + [grid_import_kwh_total_entity_id, grid_export_kwh_total_entity_id, tesla_wallconnector_energy_entity_id]
    last_hour_history = _get_history(last_hour_start, last_hour_end, history_entity_ids, True, False, False, True)
    if not last_hour_history:
        log.warning("No history available for last hour, skipping")
        return

    # Consumption-weighted spot prices: grid import weights the buy price,
    # grid export weights the sell price.
    last_hour_buy_price = _calculate_weighted_average_price(
        last_hour_start, last_hour_end, buy_price_entity_id, grid_import_kwh_total_entity_id,
        consumption_history=last_hour_history.get(grid_import_kwh_total_entity_id))
    last_hour_sell_price = _calculate_weighted_average_price(
        last_hour_start, last_hour_end, sell_price_entity_id, grid_export_kwh_total_entity_id,
        consumption_history=last_hour_history.get(grid_export_kwh_total_entity_id))

    # Fallback to simple average if weighted calculation failed
    if last_hour_buy_price is None or last_hour_sell_price is None:
        last_hour_prices = _get_statistic(last_hour_start, last_hour_end, [buy_price_entity_id, sell_price_entity_id], "hour", ['state'])
        if last_hour_buy_price is None:
            if not last_hour_prices or not last_hour_prices.get(buy_price_entity_id):
                log.warning("No buy price available for last hour, skipping")
                return
            last_hour_buy_price = float(last_hour_prices[buy_price_entity_id][0]['state'])
        if last_hour_sell_price is None:
            if not last_hour_prices or not last_hour_prices.get(sell_price_entity_id):
                log.warning("No sell price available for last hour, skipping")
                return
            last_hour_sell_price = float(last_hour_prices[sell_price_entity_id][0]['state'])

    last_hour_heat_pump_used_kwh = float(state.get(nibe_energy_used_last_hour_kwh_total_entity_id))
    last_hour_charged_kwh = _delta_from_history(last_hour_history.get(tesla_wallconnector_energy_entity_id, []))/1000.0

    # Without the flow totals the solar share is unknown: claim no solar
    # benefit (cost with solar equals cost without), leave the savings
    # metrics untouched, keep tracking heat pump kWh.
    if not _energy_totals_available(*flow_entity_ids):
        log.info("Kotiakku energy flow totals unavailable, booking costs without solar benefit")
        car_cost = _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh)
        heat_pump_cost = _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh)
        _sum_value_to_sensor(car_cost, car_charge_cost_without_solar_entity_id)
        _sum_value_to_sensor(car_cost, car_charge_cost_with_solar_entity_id)
        _sum_value_to_sensor(heat_pump_cost, heat_pump_cost_without_solar_entity_id)
        _sum_value_to_sensor(heat_pump_cost, heat_pump_cost_with_solar_entity_id)
        _sum_value_to_sensor(last_hour_heat_pump_used_kwh, heat_pump_consumed_kwh_entity_id)
        return

    # Energy flows last hour (kWh)
    solar_to_house_kwh = _delta_from_history(last_hour_history.get(solar_to_house_kwh_total_entity_id, []))
    solar_to_grid_kwh = _delta_from_history(last_hour_history.get(solar_to_grid_kwh_total_entity_id, []))
    solar_to_battery_kwh = _delta_from_history(last_hour_history.get(solar_to_battery_kwh_total_entity_id, []))
    grid_to_house_kwh = _delta_from_history(last_hour_history.get(grid_to_house_kwh_total_entity_id, []))
    grid_to_battery_kwh = _delta_from_history(last_hour_history.get(grid_to_battery_kwh_total_entity_id, []))
    battery_to_house_kwh = _delta_from_history(last_hour_history.get(battery_to_house_kwh_total_entity_id, []))
    battery_to_grid_kwh = _delta_from_history(last_hour_history.get(battery_to_grid_kwh_total_entity_id, []))

    # Consumers share the non-solar (grid + battery) part of house consumption
    # in proportion to their usage; battery energy is priced as grid energy
    # here so the battery's benefit shows up only in the battery metric.
    non_solar_house_kwh = grid_to_house_kwh + battery_to_house_kwh
    car_share_of_purchase = _share_of_purchase(last_hour_charged_kwh, non_solar_house_kwh, solar_to_house_kwh)
    heat_pump_share_of_purchase = _share_of_purchase(last_hour_heat_pump_used_kwh, non_solar_house_kwh, solar_to_house_kwh)

    # Overall solar savings
    overall_savings_last_hour = _calculate_overall_solar_savings_last_hour(
        solar_to_house_kwh, solar_to_grid_kwh, solar_to_battery_kwh, last_hour_buy_price, last_hour_sell_price)
    _sum_value_to_sensor(overall_savings_last_hour, solar_savings_entity_id)

    # Battery net benefit (helper is new; skip until created)
    if _helper_exists(battery_savings_entity_id):
        battery_savings_last_hour = _calculate_battery_savings_last_hour(
            battery_to_house_kwh, battery_to_grid_kwh, grid_to_battery_kwh, solar_to_battery_kwh,
            last_hour_buy_price, last_hour_sell_price)
        _sum_value_to_sensor(battery_savings_last_hour, battery_savings_entity_id)

    # Car charge cost and savings
    car_charge_cost_without_solar_last_hour = _calculate_car_charge_cost_without_solar_last_hour(last_hour_buy_price, last_hour_charged_kwh)
    car_charge_cost_with_solar_last_hour = _calculate_car_charge_cost_with_solar_last_hour(last_hour_buy_price, non_solar_house_kwh, car_share_of_purchase)
    _sum_value_to_sensor(car_charge_cost_without_solar_last_hour, car_charge_cost_without_solar_entity_id)
    _sum_value_to_sensor(car_charge_cost_with_solar_last_hour, car_charge_cost_with_solar_entity_id)

    # Heat pump cost and savings
    heat_pump_cost_without_solar_last_hour = _calculate_heat_pump_cost_without_solar_last_hour(last_hour_buy_price, last_hour_heat_pump_used_kwh)
    heat_pump_cost_with_solar_last_hour = _calculate_heat_pump_cost_with_solar_last_hour(last_hour_buy_price, non_solar_house_kwh, heat_pump_share_of_purchase)
    _sum_value_to_sensor(heat_pump_cost_without_solar_last_hour, heat_pump_cost_without_solar_entity_id)
    _sum_value_to_sensor(heat_pump_cost_with_solar_last_hour, heat_pump_cost_with_solar_entity_id)

    # Heat pump all time consumption
    _sum_value_to_sensor(last_hour_heat_pump_used_kwh, heat_pump_consumed_kwh_entity_id)
