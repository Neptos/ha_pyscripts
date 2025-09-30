# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant pyscript collection for calculating solar panel savings and electricity cost tracking. Scripts run hourly via cron triggers and write data to input_number helpers in Home Assistant.

## Architecture

**Core Pattern**: Each script uses Home Assistant's recorder API to fetch historical data and statistics, performs calculations, and writes results to input_number helpers.

**Data Flow**:
1. Scripts triggered by `@time_trigger("cron(2 * * * *)")` (2 minutes past each hour)
2. Fetch data from HA recorder using `get_instance(hass).async_add_executor_job()`
3. Calculate metrics based on energy usage and spot prices
4. Write results to input_number helpers (must be pre-created in HA)
5. Users create template sensors from input_numbers for utility meter integration

**Key Utilities**:
- `_get_statistic()`: Fetches statistics from recorder with 10-second timeout
- `_get_history()`: Fetches historical states from recorder with 10-second timeout
- `_sum_value_to_sensor()`: Accumulates values into input_number helpers with automatic attribute setup
- `_calculate_weighted_average_price()`: Calculates consumption-weighted average price across 15-min intervals
- `_normalize_price_data()`: Normalizes mixed hourly/15-min price data to consistent 15-min intervals

**Nordpool 15-Minute Pricing**: As of October 2025, Nordpool provides electricity prices in 15-minute intervals (previously hourly). Scripts handle this by:
- Normalizing raw price data that may contain mixed hourly/15-min formats due to timezone differences
- Calculating consumption-weighted averages that account for varying prices within each hour
- Maintaining hourly execution schedule while leveraging 15-min price granularity

## Scripts

### SolarSavings.py
Calculates savings from solar panels by comparing actual costs vs theoretical costs without solar.

**Read Sensors** (update these for your installation):
- `sensor.nordpool_kwh_fi_eur_3_10_0` - Buy price
- `sensor.electricity_sell_price` - Sell price
- `sensor.tesla_wall_connector_energy` - Car charging energy
- `sensor.power_meter_consumption` - Purchased electricity
- `sensor.power_meter_exported` - Exported electricity
- `sensor.inverter_total_yield` - Solar production
- `sensor.nibe_energy_used_last_hour` - Heat pump usage

**Write Sensors** (create as input_number helpers):
- `input_number.solar_savings`
- `input_number.car_charge_without_solar`
- `input_number.car_charge_with_solar`
- `input_number.heat_pump_cost_without_solar`
- `input_number.heat_pump_cost_with_solar`
- `input_number.heat_pump_consumed_kwh`

**Logic**:
- Calculates consumption-weighted average prices using 15-min intervals (lines 190-201)
- Falls back to simple average if weighted calculation fails
- Handles bidirectional energy flow correction where purchased and exported kWh during same hour are netted against each other
- Distributes purchased energy costs to car/heat pump based on their proportional usage

### UpdateSpotPriceSensors.py
Creates electricity cost indicators (0-3 scale) based on short-term (today+tomorrow) and long-term (10 days) price trends.

**Read Sensors**:
- `sensor.nordpool_kwh_fi_eur_3_10_0255` - Historical prices
- `sensor.nordpool_kwh_fi_eur_3_10_0` - Current/future prices

**Write Sensors** (create as input_number helpers):
- `input_number.spot_price_cost` - Cost indicator (0-3)
- `input_number.electricity_buy_price_monthly_average`
- `input_number.electricity_buy_price_yearly_average`
- Plus detailed price statistics as attributes

**Logic**:
- Normalizes raw_today/raw_tomorrow data to handle mixed hourly/15-min formats (lines 123, 133)
- Splits hourly entries (>45 min duration) into four 15-min intervals with equal prices
- Cost indicator: 0=cheap, 3=expensive
- Short-term component (0-2): Based on position vs 25th percentile, average of today+tomorrow
- Long-term component (+0 or +1): Based on position vs 10-day average

## Development Notes

**Testing**: Use the service `spotPriceSensorsTestService` to manually trigger UpdateSpotPriceSensors calculations without waiting for cron.

**Sensor Configuration**: All sensor entity IDs are hardcoded in the scripts. When adapting for a different installation, update the entity ID variables at the top of each main function.

**Helper Creation**: Input_number helpers cannot be reliably persisted when created via pyscript. Create them manually in HA UI before deploying scripts.

**Deployment**: Copy `.py` files to Home Assistant's `config/pyscript/` directory.