"""Unit tests for the pure control-decision functions in TeslaSmartCharging.py.

`_compute_desired_action` returns a dict; assertions target 'type', 'amps',
'status_code'. Pure-solar branches require is_daylight=True since the
`not is_daylight` branch returns first. All access via the `tesla` fixture.
"""


def test_scheduled_slot_preempts(tesla):
    # is_in_slot=True preempts regardless of other args
    action = tesla._compute_desired_action(
        is_in_slot=True,
        is_charging=False,
        is_daylight=False,
        surplus_watts=-9999,
        buy_price=None,
        sell_price=None,
        price_threshold=None,
    )
    assert action['type'] == 'charge'
    assert action['amps'] == tesla.MAX_CHARGE_AMPS
    assert action['status_code'] == 2


def test_no_daylight_stops_or_noops(tesla):
    charging = tesla._compute_desired_action(
        False, True, False, 9999, 5, 2, None
    )
    assert charging['type'] == 'stop'
    assert charging['status_code'] == 1

    idle = tesla._compute_desired_action(
        False, False, False, 9999, 5, 2, None
    )
    assert idle['type'] == 'noop'
    assert idle['status_code'] == 1


def test_pure_solar_sustain(tesla):
    surplus = tesla.MIN_CHARGE_POWER_W + 500
    action = tesla._compute_desired_action(
        is_in_slot=False,
        is_charging=True,
        is_daylight=True,
        surplus_watts=surplus,
        buy_price=5,
        sell_price=2,
        price_threshold=None,
    )
    assert action['type'] == 'charge'
    assert action['status_code'] == 3
    assert action['amps'] == tesla._calculate_target_amps_from_power(surplus)


def test_pure_solar_start_threshold(tesla):
    # At/above start threshold, not charging -> pure solar start (code 3)
    at = tesla._compute_desired_action(
        False, False, True, tesla.SOLAR_START_THRESHOLD_W, 5, 2, None
    )
    assert at['type'] == 'charge'
    assert at['status_code'] == 3

    # Just below start threshold, not charging -> NOT pure-solar-start.
    # With no price_threshold, blended is never chosen -> noop, code 0.
    below = tesla._compute_desired_action(
        False, False, True, tesla.SOLAR_START_THRESHOLD_W - 1, 5, 2, None
    )
    assert below['status_code'] == 0


def test_blended_requires_threshold(tesla):
    surplus = tesla.SOLAR_BLENDED_MIN_W + 100
    # price_threshold=None -> never blended
    no_thresh = tesla._compute_desired_action(
        False, False, True, surplus, 10, 2, None
    )
    assert no_thresh['status_code'] == 0

    # threshold above the effective price -> blended charge at MIN amps, code 3
    effective = tesla._calculate_blended_effective_price(surplus, 10, 2)
    with_thresh = tesla._compute_desired_action(
        False, False, True, surplus, 10, 2, effective + 1
    )
    assert with_thresh['type'] == 'charge'
    assert with_thresh['amps'] == tesla.MIN_CHARGE_AMPS
    assert with_thresh['status_code'] == 3


def test_insufficient_surplus(tesla):
    low = tesla.SOLAR_BLENDED_MIN_W - 100
    charging = tesla._compute_desired_action(
        False, True, True, low, 5, 2, None
    )
    assert charging['type'] == 'stop'
    assert charging['status_code'] == 0

    idle = tesla._compute_desired_action(
        False, False, True, low, 5, 2, None
    )
    assert idle['type'] == 'noop'
    assert idle['status_code'] == 0


def test_effective_surplus_asymmetry(tesla):
    # charging -> instantaneous grid + tesla draw
    assert tesla._get_effective_surplus(
        is_charging=True, grid_power_instant=1000, grid_power_avg=500, tesla_power_w=300
    ) == 1300
    # not charging -> 15-min averaged grid power
    assert tesla._get_effective_surplus(
        is_charging=False, grid_power_instant=1000, grid_power_avg=500, tesla_power_w=300
    ) == 500


def test_grid_sensor_unavailable_stops_when_charging(tesla):
    """M3: charging + None surplus in daylight -> stop 'Grid sensor unavailable'.

    Before the fix, a coerced-0 grid read let _get_effective_surplus return the
    (0 + tesla_draw) pure-solar path; a None surplus now short-circuits to stop.
    """
    action = tesla._compute_desired_action(
        is_in_slot=False,
        is_charging=True,
        is_daylight=True,
        surplus_watts=None,
        buy_price=10,
        sell_price=2,
        price_threshold=5,
    )
    assert action['type'] == 'stop'
    assert action['status_msg'] == 'Grid sensor unavailable'


def test_grid_sensor_unavailable_noops_when_not_charging(tesla):
    """Not charging + None surplus in daylight -> noop 'Grid sensor unavailable'."""
    action = tesla._compute_desired_action(
        is_in_slot=False,
        is_charging=False,
        is_daylight=True,
        surplus_watts=None,
        buy_price=10,
        sell_price=2,
        price_threshold=5,
    )
    assert action['type'] == 'noop'
    assert action['status_msg'] == 'Grid sensor unavailable'


def test_scheduled_slot_unaffected_by_grid_outage(tesla):
    """Scheduled-slot charging (tier 1) ignores a None surplus."""
    action = tesla._compute_desired_action(
        is_in_slot=True,
        is_charging=False,
        is_daylight=True,
        surplus_watts=None,
        buy_price=None,
        sell_price=None,
        price_threshold=None,
    )
    assert action['type'] == 'charge'
    assert action['amps'] == tesla.MAX_CHARGE_AMPS


def test_effective_surplus_none_when_grid_unavailable(tesla):
    # charging + instant None -> None
    assert tesla._get_effective_surplus(True, None, 500, 9000) is None
    # not charging + avg None -> None
    assert tesla._get_effective_surplus(False, 1000, None, 300) is None


def test_solar_only_ignores_scheduled_slot(tesla):
    """Solar-only mode: in-slot at night -> wait for daylight, not scheduled charge."""
    action = tesla._compute_desired_action(
        is_in_slot=True,
        is_charging=False,
        is_daylight=False,
        surplus_watts=0,
        buy_price=5,
        sell_price=2,
        price_threshold=None,
        solar_only=True,
    )
    assert action['type'] == 'noop'
    assert action['status_code'] == 1
    assert action['status_msg'] == 'Solar-only: waiting for daylight'


def test_solar_only_stops_charging_in_slot_at_night(tesla):
    """Solar-only mode: charging in a scheduled slot at night -> stop."""
    action = tesla._compute_desired_action(
        is_in_slot=True,
        is_charging=True,
        is_daylight=False,
        surplus_watts=0,
        buy_price=5,
        sell_price=2,
        price_threshold=None,
        solar_only=True,
    )
    assert action['type'] == 'stop'
    assert action['status_msg'] == 'Solar-only: waiting for daylight'


def test_solar_only_allows_pure_solar(tesla):
    """Solar-only mode: pure solar tiers still charge; in-slot doesn't preempt."""
    surplus = tesla.SOLAR_START_THRESHOLD_W + 100
    action = tesla._compute_desired_action(
        is_in_slot=True,
        is_charging=False,
        is_daylight=True,
        surplus_watts=surplus,
        buy_price=5,
        sell_price=2,
        price_threshold=None,
        solar_only=True,
    )
    assert action['type'] == 'charge'
    assert action['status_code'] == 3
    assert action['amps'] == tesla._calculate_target_amps_from_power(surplus)


def test_solar_only_disables_blended(tesla):
    """Solar-only mode: blended conditions met (cheap price) -> still no charge."""
    surplus = tesla.SOLAR_BLENDED_MIN_W + 100
    effective = tesla._calculate_blended_effective_price(surplus, 10, 2)
    action = tesla._compute_desired_action(
        is_in_slot=False,
        is_charging=False,
        is_daylight=True,
        surplus_watts=surplus,
        buy_price=10,
        sell_price=2,
        price_threshold=effective + 1,
        solar_only=True,
    )
    assert action['type'] == 'noop'
    assert action['status_code'] == 0


def test_is_solar_only_mode_reads_helper(tesla, world):
    """_is_solar_only_mode: 'on' -> True; unavailable/missing -> False (normal mode)."""
    world(tesla, get={tesla.OUTPUT_SOLAR_ONLY_MODE: "on"})
    assert tesla._is_solar_only_mode() is True

    world(tesla, get={tesla.OUTPUT_SOLAR_ONLY_MODE: "unavailable"})
    assert tesla._is_solar_only_mode() is False

    world(tesla, get={})
    assert tesla._is_solar_only_mode() is False


def test_gather_controller_inputs_grid_unavailable(tesla, world):
    """_gather_controller_inputs: 'unavailable' grid -> None + warning logged."""
    get = {
        tesla.GRID_POWER_CURRENT: "unavailable",
        tesla.GRID_POWER_15MIN_AVG: "500",
        tesla.TESLA_CHARGER_POWER: "0",
    }
    w = world(tesla, get=get)

    inputs = tesla._gather_controller_inputs()

    assert inputs['grid_power_instant'] is None
    warnings = [msg for (lvl, msg) in w.log.records
                if lvl == "warning" and "Grid power sensor unavailable" in str(msg)]
    assert warnings
