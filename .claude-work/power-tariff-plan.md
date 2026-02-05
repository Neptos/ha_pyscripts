# Power Tariff Management Plan

**Status**: Future implementation (not yet needed)

## Problem

Power rate tariffs may require staying under 8kW average hourly consumption. Current charging logic runs at full 9kW during scheduled slots, which could exceed this threshold when combined with other household loads.

## Required Sensors

1. **Mains power sensor** (exists): Measures active power in W, negative = buying, positive = selling
2. **Hourly energy meter** (to create): Utility meter that resets each hour, tracking total kWh consumed from mains since hour start
3. **Recent average power** (to create): 15-minute moving average of mains power to estimate current "other load" baseline
4. **Monthly peak tracker** (to create): Tracks highest hourly average this month, resets on 1st

Example HA configuration:
```yaml
utility_meter:
  mains_energy_hourly:
    source: sensor.mains_energy  # Integrate power sensor to energy
    cycle: hourly

sensor:
  - platform: statistics
    name: "Mains Power 15min Average"
    entity_id: sensor.power_meter_active_power
    state_characteristic: average_linear
    max_age:
      minutes: 15

  - platform: statistics
    name: "Mains Power Hourly Average"
    entity_id: sensor.power_meter_active_power
    state_characteristic: average_linear
    max_age:
      minutes: 60

input_number:
  monthly_peak_power_kw:
    name: "Monthly Peak Power"
    min: 0
    max: 50
    step: 0.1
    unit_of_measurement: "kW"
    # Reset via automation on 1st of month

automation:
  # Update monthly peak when hourly average exceeds current peak
  - trigger:
      - platform: state
        entity_id: sensor.mains_power_hourly_average
    condition:
      - condition: template
        value_template: >
          {{ states('sensor.mains_power_hourly_average') | float / 1000
             > states('input_number.monthly_peak_power_kw') | float }}
    action:
      - service: input_number.set_value
        target:
          entity_id: input_number.monthly_peak_power_kw
        data:
          value: "{{ states('sensor.mains_power_hourly_average') | float / 1000 }}"

  # Reset monthly peak on 1st of each month
  - trigger:
      - platform: time
        at: "00:00:00"
    condition:
      - condition: template
        value_template: "{{ now().day == 1 }}"
    action:
      - service: input_number.set_value
        target:
          entity_id: input_number.monthly_peak_power_kw
        data:
          value: 0
```

## Load Estimation

Other household loads (heat pump, appliances, etc.) vary significantly:
- Heat pump: 0-2kW typical, spikes to 5kW occasionally
- Base load: ~500W (fridge, standby, etc.)
- Variable loads: cooking, laundry, etc.

The 15-min average helps estimate current "other load" so charging can adapt:
- If heat pump is running at 2kW, charging budget = 8kW - 2kW = 6kW
- If other loads spike, reduce charging amps dynamically

## Implementation Approach

### Core Logic

Calculate remaining power budget for the current hour, accounting for other loads:
```python
POWER_TARIFF_LIMIT_KW = 8.0  # Configurable threshold
POWER_TARIFF_MARGIN_KW = 0.5  # Safety margin
MAINS_POWER_AVG_SENSOR = "sensor.mains_power_15min_average"
HOURLY_ENERGY_SENSOR = "sensor.mains_energy_hourly"
TESLA_POWER_SENSOR = "sensor.tesla_wall_connector_power"  # To subtract from "other load"

def _get_max_charging_amps():
    """Calculate maximum charging amps to stay under hourly tariff limit."""
    # Get energy consumed this hour
    energy_used_kwh = abs(float(state.get(HOURLY_ENERGY_SENSOR) or 0))
    minutes_left = 60 - datetime.now().minute

    if minutes_left <= 1:
        return MAX_CHARGE_AMPS  # Hour boundary, full budget available soon

    # Calculate max average power for rest of hour
    effective_limit = POWER_TARIFF_LIMIT_KW - POWER_TARIFF_MARGIN_KW  # 7.5kW
    budget_kwh = effective_limit - energy_used_kwh

    if budget_kwh <= 0:
        return 0  # Budget exhausted, don't charge

    max_avg_power_kw = budget_kwh / (minutes_left / 60)

    # Estimate current "other load" (total load minus Tesla charging)
    total_load_w = abs(float(state.get(MAINS_POWER_AVG_SENSOR) or 0))
    tesla_load_w = float(state.get(TESLA_POWER_SENSOR) or 0)
    other_load_w = max(0, total_load_w - tesla_load_w)
    other_load_kw = other_load_w / 1000

    # Available for charging = max power - other loads
    available_for_charging_kw = max(0, max_avg_power_kw - other_load_kw)

    # Convert to amps
    available_amps = int(available_for_charging_kw * 1000 / (VOLTAGE * PHASES))

    return max(0, min(MAX_CHARGE_AMPS, available_amps))
```

Example scenarios:
- Hour start, no consumption yet, heat pump at 1.5kW:
  - Budget: 7.5kWh for 60 min = 7.5kW max
  - Available: 7.5 - 1.5 = 6kW = 8.7A → charge at 8A

- 30 min in, used 4kWh, heat pump at 2kW:
  - Budget: 3.5kWh for 30 min = 7kW max
  - Available: 7 - 2 = 5kW = 7.2A → charge at 7A

- 50 min in, used 7kWh, heat pump at 1kW:
  - Budget: 0.5kWh for 10 min = 3kW max
  - Available: 3 - 1 = 2kW = 2.9A → below min, skip charging

### Integration Points

1. **Scheduled charging** (`_start_charging()`):
   - Before starting, check `_get_max_charging_power_kw()`
   - Convert to amps: `amps = min(target_amps, max_power_kw * 1000 / (VOLTAGE * PHASES))`
   - If max amps < MIN_CHARGE_AMPS, skip this slot

2. **During charging** (in executor loop):
   - Periodically recalculate available budget
   - Reduce amps if approaching limit
   - Could check every 5 minutes

3. **Solar opportunistic charging**:
   - Already has dynamic amp adjustment
   - Add power tariff as additional constraint on top of solar surplus logic

### Configuration

```python
# --- Power Tariff Parameters ---
POWER_TARIFF_ENABLED = False  # Toggle when tariff becomes active
POWER_TARIFF_LIMIT_KW = 8.0   # Hourly average limit
POWER_TARIFF_MARGIN_KW = 0.5  # Safety margin (target 7.5kW to stay under 8kW)
HOURLY_ENERGY_SENSOR = "sensor.mains_energy_hourly"  # Utility meter
```

### Priority Override: 50% SOC Guarantee

The MIN_SOC_GUARANTEE (50%) takes precedence over power tariff limits. If throttling would prevent reaching 50% by deadline:

1. **Calculate if guarantee is at risk:**
   ```python
   def _is_soc_guarantee_at_risk():
       current_soc = _get_current_soc()
       if current_soc >= MIN_SOC_GUARANTEE:
           return False

       energy_needed = _calculate_kwh_needed(current_soc, MIN_SOC_GUARANTEE)
       hours_until_deadline = (deadline - now).total_seconds() / 3600

       # Estimate available energy with tariff-limited charging
       # Assume average ~5kW charging (accounting for other loads)
       estimated_energy = hours_until_deadline * 5.0 * CHARGING_EFFICIENCY

       return estimated_energy < energy_needed
   ```

2. **If at risk, calculate minimum required power:**
   - Spread the extra load across remaining hours to minimize peak
   - Only exceed by the minimum necessary each hour
   ```python
   def _get_minimum_required_power_kw():
       """Calculate minimum charging power to guarantee 50% SOC."""
       energy_needed = _calculate_kwh_needed(current_soc, MIN_SOC_GUARANTEE)
       hours_left = (deadline - now).total_seconds() / 3600

       if hours_left <= 0:
           return MAX_CHARGE_RATE_KW  # Emergency, charge full speed

       # Minimum average power needed (before efficiency loss)
       min_power = energy_needed / hours_left / CHARGING_EFFICIENCY
       return min(min_power, MAX_CHARGE_RATE_KW)
   ```

3. **Final charging power decision:**
   ```python
   def _get_charging_power_kw():
       tariff_limited = _get_max_charging_power_kw()  # From tariff logic

       if _is_soc_guarantee_at_risk():
           minimum_required = _get_minimum_required_power_kw()
           # Use higher of: tariff limit or minimum required
           # This exceeds tariff only when necessary
           return max(tariff_limited, minimum_required)

       return tariff_limited
   ```

### Dynamic Limit: Track Month's Peak

Since tariff is based on the month's highest peak, once we've exceeded 8kW, that becomes our new "free" limit:

```python
POWER_TARIFF_BASE_LIMIT_KW = 8.0
MONTHLY_PEAK_SENSOR = "input_number.monthly_peak_power_kw"  # Reset on 1st of month

def _get_effective_power_limit_kw():
    """Get effective limit - base or month's existing peak, whichever is higher."""
    base_limit = POWER_TARIFF_BASE_LIMIT_KW
    monthly_peak = float(state.get(MONTHLY_PEAK_SENSOR) or 0)
    return max(base_limit, monthly_peak)
```

If heat pump spiked to 9kW on day 3, we can charge at 9kW "for free" the rest of the month.

### Cost Comparison: Extra Slots vs Power Overage

When 50% SOC is at risk, compare two options:

**Option A: Add more (expensive) slots**
- Stay under power limit
- Pay higher spot price for additional slots
- Cost = `(expensive_price - cheap_price) × extra_kWh`

**Option B: Exceed power limit**
- Charge faster in existing cheap slots
- Pay higher power tariff tier for entire month
- Cost = `monthly_tier_cost_increase`

```python
def _calculate_slot_expansion_cost(extra_kwh_needed, available_slots):
    """Cost of charging extra kWh in more expensive slots."""
    # Sort available slots by price (already selected cheapest)
    remaining_slots = sorted(available_slots, key=lambda s: s['effective_price'])

    cost = 0
    kwh_added = 0
    for slot in remaining_slots:
        if kwh_added >= extra_kwh_needed:
            break
        slot_kwh = min(slot['energy'], extra_kwh_needed - kwh_added)
        cost += slot_kwh * slot['effective_price'] / 100  # EUR
        kwh_added += slot_kwh

    return cost if kwh_added >= extra_kwh_needed else float('inf')  # Can't fulfill

def _calculate_power_overage_cost(current_limit, new_limit):
    """Cost of increasing monthly power tier."""
    # Example: 2 EUR/kW/month for power tariff
    POWER_TARIFF_EUR_PER_KW = 2.0  # Configure based on actual tariff
    return (new_limit - current_limit) * POWER_TARIFF_EUR_PER_KW

def _decide_soc_guarantee_strategy():
    """Choose cheapest strategy to guarantee 50% SOC."""
    extra_kwh_needed = ...  # kWh shortfall with current power-limited slots

    # Option A: Expand slots
    slot_cost = _calculate_slot_expansion_cost(extra_kwh_needed, remaining_slots)

    # Option B: Increase power (to minimum necessary)
    min_power_needed = _get_minimum_required_power_kw()
    current_limit = _get_effective_power_limit_kw()
    power_cost = _calculate_power_overage_cost(current_limit, min_power_needed)

    if slot_cost <= power_cost:
        return 'expand_slots'
    else:
        return 'increase_power'
```

**Example scenario:**
- Need 5 extra kWh to reach 50% SOC
- Cheap slots: 5 c/kWh, expensive available slot: 15 c/kWh
- Option A (slots): 5 kWh × 0.10 EUR = 0.50 EUR extra
- Option B (power): Jump from 8kW to 10kW tier = 2 × 2 EUR = 4 EUR extra
- **Decision: Add expensive slots** (saves 3.50 EUR)

**Example scenario:**
- Need 20 extra kWh, only 5 kWh available in expensive slots
- Option A: Can't fulfill with slots alone → infinite cost
- Option B: Increase power tier
- **Decision: Increase power** (only option)

### Edge Cases

1. **Hour boundary**: Budget resets at :00, can resume full charging
2. **Already over limit**: Set charging to minimum or skip slot entirely
3. **Other large loads**: Account for baseline household consumption
4. **Sensor unavailable**: Fall back to conservative limit (e.g., 6kW)
5. **SOC guarantee at risk**: Override tariff limit with minimum necessary power
6. **Late detection**: If guarantee risk detected late, may need higher spike - log warning

## Code Changes Summary

1. Add configuration constants (5 lines)
2. Add `_get_max_charging_power_kw()` function (~20 lines)
3. Modify `_start_charging()` to respect power limit (~10 lines)
4. Add periodic check in executor to reduce amps if needed (~15 lines)
5. Optionally extend solar charging logic (~10 lines)

**Estimated total: ~60 lines of code**

## Testing

1. Create test utility meter sensor
2. Simulate high consumption scenarios
3. Verify charging throttles appropriately
4. Test hour boundary reset behavior
5. Test sensor unavailable fallback
