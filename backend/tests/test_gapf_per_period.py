"""Fails on HEAD (no gapf setting: periods.settings has none, band() takes none), passes with patches/gapf_per_period.diff."""
import numpy as np
import pytest

from wb.engine.l2.box import band
from wb.engine.l2.periods import RANGE, settings


def test_the_dense_band_bridges_dips_by_the_periods_bar():
    # design 2026-10-12-structures-every-period: a price setting is scaled by the period's bar — the dips the dense band
    # steps over (1% of the price on a day) too; a 1-minute box is ~0.3% tall, and 1% steps over every dip in it
    assert settings("m1")["BOX"]["gapf"] == pytest.approx(0.01 * RANGE["m1"])
    assert settings("week")["BOX"]["gapf"] == pytest.approx(0.01 * RANGE["week"])
    # two clusters of minutes 0.3% apart: one band on a day's setting, two on a minute's
    lo = np.array([10.00] * 20 + [10.05] * 20)
    hi = np.array([10.02] * 20 + [10.07] * 20)
    assert band(lo, hi, 0, 39, 0.5, 100) == pytest.approx((10.00, 10.07), abs=1e-3)
    L, U = band(lo, hi, 0, 39, 0.5, 100, 0.01 * RANGE["m1"])
    assert U - L < 0.03   # one cluster only
