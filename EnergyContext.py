from datetime import datetime

BATTERY_CAPACITY_KWH = 21.0
BATTERY_FULL_SOC = 95.0        # Elisa stops charging from solar around here
BATTERY_NEAR_FULL_SOC = 94.0
FORECAST_CONFIDENCE = 0.8
FILL_MARGIN_KWH = 2.0
HOUSE_BASE_LOAD_KW = 0.7       # house use without the flexible loads

FLOW_SENSORS = {
    'solar_to_grid': 'sensor.kotiakku_solar_to_grid_kw',
    'solar_to_battery': 'sensor.kotiakku_solar_to_battery_kw',
    'battery_to_house': 'sensor.kotiakku_battery_to_house_kw',
    'grid_to_house': 'sensor.kotiakku_grid_to_house_kw',
}
SOC_SENSOR = 'sensor.kotiakku_state_of_charge_percent'
FORECAST_REMAINING_TODAY = 'sensor.energy_production_today_remaining'
OUTPUT = 'input_number.net_solar_surplus'


def _battery_fills_anyway(soc, forecast_remaining_kwh, hours_to_sunset):
    """True when the battery reaches full today even if loads take solar now.

    The forecast is discounted and a margin kept because on dull days it has
    overestimated production several times over.
    """
    if soc >= BATTERY_NEAR_FULL_SOC:
        return True
    headroom_kwh = BATTERY_CAPACITY_KWH * (BATTERY_FULL_SOC - soc) / 100
    house_kwh = HOUSE_BASE_LOAD_KW * hours_to_sunset
    spare_kwh = forecast_remaining_kwh * FORECAST_CONFIDENCE - house_kwh - headroom_kwh
    return spare_kwh >= FILL_MARGIN_KWH


def _net_surplus(flows, battery_fills):
    """Solar power loads can take without costing battery or grid energy, in W.

    Negative when the house is already drawing from the battery or the grid.
    Solar going into the battery only counts when the battery fills anyway.
    """
    surplus = flows['solar_to_grid'] - flows['battery_to_house'] - flows['grid_to_house']
    if battery_fills:
        surplus += flows['solar_to_battery']
    return surplus


def _read_float(entity_id):
    """Numeric state, or None when unavailable or the entity does not exist."""
    try:
        return float(state.get(entity_id))
    except (ValueError, TypeError, NameError):
        return None


def _hours_to_sunset(now):
    if state.get('sun.sun') != 'above_horizon':
        return 0.0
    next_setting = datetime.fromisoformat(state.getattr('sun.sun')['next_setting'])
    return max((next_setting - now).total_seconds() / 3600, 0.0)


@time_trigger("startup", "cron(*/5 * * * *)")
def updateNetSolarSurplus():
    """Write the net solar surplus shared by the solar-aware consumers.

    Missing Kotiakku data writes 0 with data_available false, so consumers
    see no surplus and fall back to their non-solar behaviour.
    """
    now = datetime.now().astimezone()

    flows = {}
    for key, entity_id in FLOW_SENSORS.items():
        flows[key] = _read_float(entity_id)
    soc = _read_float(SOC_SENSOR)

    if None in flows.values() or soc is None:
        state.set(OUTPUT, 0, data_available=False, last_calculated=now.isoformat())
        return

    forecast_remaining_kwh = _read_float(FORECAST_REMAINING_TODAY) or 0.0
    hours_to_sunset = _hours_to_sunset(now)
    battery_fills = _battery_fills_anyway(soc, forecast_remaining_kwh, hours_to_sunset)
    surplus = _net_surplus(flows, battery_fills)

    state.set(
        OUTPUT,
        round(surplus),
        data_available=True,
        battery_fills_anyway=battery_fills,
        soc=soc,
        forecast_remaining_kwh=forecast_remaining_kwh,
        hours_to_sunset=round(hours_to_sunset, 2),
        solar_to_grid_w=flows['solar_to_grid'],
        solar_to_battery_w=flows['solar_to_battery'],
        battery_to_house_w=flows['battery_to_house'],
        grid_to_house_w=flows['grid_to_house'],
        last_calculated=now.isoformat(),
    )
