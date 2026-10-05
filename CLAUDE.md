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
Books the hourly value of the solar panels and the Elisa-controlled home battery from the Elisa Kotiakku integration's energy flow totals, and splits non-solar cost onto the car and heat pump.

**Read Sensors** (update these for your installation):
- `sensor.nordpool_kwh_fi_eur_3_10_0` - Buy price
- `sensor.electricity_sell_price` - Sell price
- `sensor.tesla_wall_connector_energy` - Car charging energy
- `sensor.nibe_energy_used_last_hour` - Heat pump usage
- `sensor.kotiakku_total_grid_import_kwh` / `sensor.kotiakku_total_grid_export_kwh` - weights for the buy/sell price averages
- `sensor.kotiakku_solar_to_house_kwh`, `sensor.kotiakku_solar_to_grid_kwh`, `sensor.kotiakku_solar_to_battery_kwh`, `sensor.kotiakku_grid_to_house_kwh`, `sensor.kotiakku_grid_to_battery_kwh`, `sensor.kotiakku_battery_to_house_kwh`, `sensor.kotiakku_battery_to_grid_kwh` - per-flow kWh totals (Jarauvi/elisa_kotiakku integration, 5-minute updates)

**Write Sensors** (create as input_number helpers):
- `input_number.solar_savings`
- `input_number.battery_savings` (skipped with a one-time warning until created)
- `input_number.car_charge_without_solar`
- `input_number.car_charge_with_solar`
- `input_number.heat_pump_cost_without_solar`
- `input_number.heat_pump_cost_with_solar`
- `input_number.heat_pump_consumed_kwh`

**Logic**:
- Consumption-weighted buy/sell prices over 5-minute intervals (grid import weights buy, grid export weights sell); simple average fallback
- Solar savings = buy x solar_to_house + sell x (solar_to_grid + solar_to_battery): direct use avoids the buy price, everything else is valued at the sell price it would have earned
- Battery savings = buy x battery_to_house + sell x battery_to_grid - buy x grid_to_battery - sell x solar_to_battery: negative in charging hours, positive when discharging; the running sum is the battery's net benefit. Stored solar is charged here at the sell price, so the two metrics never double count
- Car/heat pump "with solar" cost = their share of the non-solar house consumption (grid_to_house + battery_to_house), priced at the buy price; battery energy is deliberately priced as grid energy so the battery's benefit appears only in the battery metric
- When the flow totals are unavailable, books cost-with-solar equal to cost-without-solar and leaves the savings metrics untouched

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
- Normalizes raw_today/raw_tomorrow data to handle mixed hourly/15-min formats
- Splits hourly entries (>45 min duration) into four 15-min intervals with equal prices
- Cost indicator: 0=cheap, 3=expensive
- Short-term component (0-2): Based on position vs the midpoint between average and minimum (labelled 25_percent), average of today+tomorrow
- Long-term component (+0 or +1): Based on position vs the 10-day historical hourly average

## Development Notes

**Testing**: Use the service `spotPriceSensorsTestService` to manually trigger UpdateSpotPriceSensors calculations without waiting for cron.

**Sensor Configuration**: All sensor entity IDs are hardcoded in the scripts. When adapting for a different installation, update the entity ID variables at the top of each main function.

**Helper Creation**: Input_number helpers cannot be reliably persisted when created via pyscript. Create them manually in HA UI before deploying scripts.

**Deployment**: Copy `.py` files to Home Assistant's `config/pyscript/` directory.

### TeslaSmartCharging.py
Optimizes Tesla charging based on Nordpool spot prices and solar production forecasts.

**Required Helpers** (create in HA UI before deploying):
- `input_number.tesla_charging_status` - Status code + schedule attributes
- `input_text.tesla_charging_schedule` - Human-readable schedule summary, e.g.: `"3 slots: 23:30-00:15, 02:00-02:30 | 6.75 kWh | €0.42 | 6.2 c/kWh"`
- `input_boolean.tesla_smart_charging_enabled` - Master on/off toggle
- `input_number.tesla_max_avg_price` - Price ceiling for average charging cost (c/kWh, 0-30, step 0.5). Set to 0 to disable. Only affects optional (Pass 2) slots — mandatory 50% guarantee always completes.
- `input_select.tesla_solar_charging_available` - Solar charging availability indicator. Options: `off`, `blended`, `pure`. Updated every 15 minutes during daylight. Reflects what would happen if the car were plugged in now (adds back Tesla draw if already charging).
- `input_boolean.tesla_solar_only_mode` - Solar-only mode toggle. When on, the schedule is still calculated and displayed but not executed; the car charges only from pure solar surplus (scheduled slots and blended tier are ignored, so the 50% morning guarantee is display-only). Missing/unavailable helper is treated as off (normal mode).

**Key Features**:
- Two-pass greedy algorithm: Pass 1 (mandatory) ensures 50% SOC by 7:00 AM, Pass 2 (optional) fills to charge limit
- Solar-aware effective pricing (opportunity cost model)
- Price ceiling control: limits optional charging slots to keep weighted average price below configured threshold
- Solar opportunistic charging during daylight hours
- 15-minute slot granularity matching Nordpool pricing

**Schedule Attributes** (on `input_number.tesla_charging_status`):
- `schedule_json` - Full JSON schedule with all slot details
- `avg_price_c_kwh` - Energy-weighted average effective price (c/kWh)
- `estimated_cost_eur` - Total estimated cost in EUR
- `slot_count`, `solar_slots_count`, `mode`, `next_slot_start`, `schedule_end`
