"""Recorder-robustness tests for calculateSpotPriceAverages in UpdateSpotPriceSensors.py.

Covers M2 (December month-end rollover) and M19 (partial-write guards / None-state
filtering). ``_get_statistic`` is monkeypatched; the ``world`` fixture captures
``input_number`` writes.
"""

from freezegun import freeze_time


def _rows(*values):
    return [{'start': None, 'state': v} for v in values]


def test_december_rollover_no_crash(spot, world, monkeypatch):
    # M2 regression: in December, month+1 -> ValueError before the fix.
    w = world(spot)
    monkeypatch.setattr(
        spot, "_get_statistic",
        lambda *a, **k: {'sensor.nordpool_kwh_fi_eur_3_10_0': _rows(1.0, 2.0, 3.0)},
    )
    with freeze_time("2026-12-15 10:01:00"):
        spot.calculateSpotPriceAverages()

    assert w.input_number.writes["electricity_buy_price_monthly_average"] == 2.0
    assert w.input_number.writes["electricity_buy_price_yearly_average"] == 2.0


def test_empty_dict_skips_writes(spot, world, monkeypatch):
    w = world(spot)
    monkeypatch.setattr(spot, "_get_statistic", lambda *a, **k: {})
    with freeze_time("2026-07-03 10:01:00"):
        spot.calculateSpotPriceAverages()

    assert "electricity_buy_price_monthly_average" not in w.input_number.writes
    assert "electricity_buy_price_yearly_average" not in w.input_number.writes
    assert any("Monthly" in msg for level, msg in w.log.records)
    assert any("Yearly" in msg for level, msg in w.log.records)


def test_none_return_skips_writes(spot, world, monkeypatch):
    # M7: _get_statistic returns None on timeout.
    w = world(spot)
    monkeypatch.setattr(spot, "_get_statistic", lambda *a, **k: None)
    with freeze_time("2026-07-03 10:01:00"):
        spot.calculateSpotPriceAverages()

    assert "electricity_buy_price_monthly_average" not in w.input_number.writes
    assert "electricity_buy_price_yearly_average" not in w.input_number.writes


def test_none_states_are_skipped(spot, world, monkeypatch):
    # Rows with state=None dropped before float(); mean over valid rows only.
    w = world(spot)
    rows = [
        {'start': None, 'state': 2.0},
        {'start': None, 'state': None},
        {'start': None, 'state': 4.0},
    ]
    monkeypatch.setattr(
        spot, "_get_statistic",
        lambda *a, **k: {'sensor.nordpool_kwh_fi_eur_3_10_0': rows},
    )
    with freeze_time("2026-07-03 10:01:00"):
        spot.calculateSpotPriceAverages()

    assert w.input_number.writes["electricity_buy_price_monthly_average"] == 3.0
    assert w.input_number.writes["electricity_buy_price_yearly_average"] == 3.0
