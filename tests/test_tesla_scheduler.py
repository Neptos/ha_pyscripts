"""Invariant/property tests for the Tesla scheduler primitives.

Covers the pure (or log-only) scheduling helpers:
``_select_slots_for_energy``, ``_apply_price_ceiling``, ``_consolidate_slots``.
Assertions target intent (energy>=need, weighted avg<=ceiling, count/start
preservation, cheapest-first order), never brittle golden values.
"""

import datetime


def _mk_slots(n, prices, start, slot_energy):
    """Build n consecutive 15-min slots with the given effective prices."""
    assert len(prices) == n
    slots = []
    for i in range(n):
        s = start + datetime.timedelta(minutes=15 * i)
        e = s + datetime.timedelta(minutes=15)
        slots.append({
            "start": s,
            "end": e,
            "effective_price": prices[i],
            "energy": slot_energy,
        })
    return slots


def _tz_start():
    tz = datetime.timezone(datetime.timedelta(hours=2))
    return datetime.datetime(2026, 7, 1, 0, 0, 0, tzinfo=tz)


def test_select_meets_energy_need(tesla):
    slot_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    slots = _mk_slots(5, [1.0, 2.0, 3.0, 4.0, 5.0], _tz_start(), slot_energy)

    need = 5.0  # requires 3 slots (2.25 * 2 = 4.5 < 5 <= 6.75)
    result = tesla._select_slots_for_energy(slots, need)
    assert sum([s["energy"] for s in result]) >= need

    # Non-positive need selects nothing.
    assert tesla._select_slots_for_energy(slots, 0) == []
    assert tesla._select_slots_for_energy(slots, -1) == []


def test_select_picks_in_given_order(tesla):
    # Pre-sorted ascending -> selection is a cheapest-first prefix.
    slot_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    prices = [1.0, 2.0, 3.0, 4.0, 5.0]
    slots = _mk_slots(5, prices, _tz_start(), slot_energy)

    result = tesla._select_slots_for_energy(slots, 6.0)  # needs 3 slots
    assert result == slots[: len(result)]
    assert [s["effective_price"] for s in result] == prices[: len(result)]


def test_price_ceiling_respects_max(tesla):
    slot_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    start = _tz_start()
    # CHEAP mandatory: alone its weighted avg (2.0) is <= max_avg.
    mandatory = _mk_slots(2, [2.0, 2.0], start, slot_energy)
    # Optional spans cheap-to-expensive.
    optional = _mk_slots(3, [3.0, 10.0, 30.0], start + datetime.timedelta(hours=1), slot_energy)
    max_avg = 5.0

    kept = tesla._apply_price_ceiling(mandatory, optional, max_avg)

    # Weighted avg computed over mandatory + kept optional must respect ceiling.
    combined = mandatory + kept
    total_cost = sum([s["effective_price"] * s["energy"] for s in combined])
    total_energy = sum([s["energy"] for s in combined])
    assert total_energy > 0
    assert total_cost / total_energy <= max_avg + 1e-9


def test_price_ceiling_keeps_cheap_optional_below_ceiling(tesla):
    """Cheap optional slots below the ceiling are NOT dropped by a high mandatory avg.

    Mandatory 8 slots @20 c/kWh, optional 4 @3 c/kWh, ceiling 8. The combined
    weighted avg starts above 8, but dropping a 3 c/kWh optional slot only raises
    the average further, so the break-before-drop guard keeps all 4. Before the
    guard the loop dropped every optional slot (0 kept).
    """
    slot_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    start = _tz_start()
    mandatory = _mk_slots(8, [20.0] * 8, start, slot_energy)
    optional = _mk_slots(4, [3.0] * 4, start + datetime.timedelta(hours=2), slot_energy)
    ceiling = 8.0

    kept = tesla._apply_price_ceiling(mandatory, optional, ceiling)

    assert len(kept) == 4


def test_price_ceiling_empty_optional(tesla):
    slot_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    mandatory = _mk_slots(2, [2.0, 2.0], _tz_start(), slot_energy)
    assert tesla._apply_price_ceiling(mandatory, [], 5.0) == []


def test_consolidate_relocates_and_preserves_count(tesla):
    slot_energy = tesla.MAX_CHARGE_RATE_KW * tesla.SLOT_DURATION_HOURS
    start = _tz_start()
    # Isolated single (index 0) plus a pair (indices 3-4). all_slots is the full
    # contiguous run so relocation targets exist. The consolidator relocates a
    # slot from a small group only when the cheapest unselected slot adjacent to
    # another group has effective_price <= slot_price * CONSOLIDATION_PRICE_TOLERANCE
    # (2.0). Here the single at index 0 (price 3.0) can move to index 2 (price 4.0)
    # since 4.0 <= 3.0 * 2.0, making 2-3-4 contiguous. That swap MUST fire.
    all_slots = _mk_slots(6, [3.0, 9.0, 4.0, 2.0, 2.0, 9.0], start, slot_energy)
    selected = [all_slots[0], all_slots[3], all_slots[4]]
    deadline = start + datetime.timedelta(hours=6)
    mandatory_starts = set()

    input_starts = {s["start"] for s in selected}
    result = tesla._consolidate_slots(selected, all_slots, deadline, mandatory_starts)
    result_starts = {s["start"] for s in result}

    # Proof the relocation path fired: the isolated single moved to a new start.
    assert result_starts != input_starts
    assert all_slots[2]["start"] in result_starts  # relocated-in slot
    assert all_slots[0]["start"] not in result_starts  # relocated-out slot

    # Count preserved on the non-trivial (swapped) result.
    assert len(result) == len(selected)

    # No duplicate start values.
    starts = [s["start"] for s in result]
    assert len(set(starts)) == len(starts)

    # Result is start-sorted and non-overlapping.
    assert all(result[i]["end"] <= result[i + 1]["start"] for i in range(len(result) - 1))
