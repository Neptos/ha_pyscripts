@time_trigger("cron(0 * * * *)")
@time_trigger("startup")
async def update_solar_forecast():
    """Fetch solar forecast from forecast_solar integration and expose wh_hours as sensor attribute for ApexCharts."""
    from homeassistant.components.forecast_solar.energy import async_get_solar_forecast

    entries = hass.config_entries.async_entries("forecast_solar")
    if not entries:
        log.warning("update_solar_forecast: no forecast_solar config entries found")
        return

    forecast = await async_get_solar_forecast(hass, entries[0].entry_id)
    if not forecast or "wh_hours" not in forecast:
        log.warning("update_solar_forecast: no wh_hours data returned")
        return

    total_kwh = round(sum([v for v in forecast["wh_hours"].values()]) / 1000, 2)

    state.set(
        "sensor.solar_forecast_wh_hours",
        value=total_kwh,
        new_attributes={
            "wh_hours": forecast["wh_hours"],
            "unit_of_measurement": "kWh",
            "friendly_name": "Solar forecast today",
        }
    )
    log.info(f"update_solar_forecast: updated with {len(forecast['wh_hours'])} hourly entries, total {total_kwh} kWh")
