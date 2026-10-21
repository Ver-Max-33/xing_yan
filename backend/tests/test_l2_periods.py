"""Each period's structure settings (design 2026-10-12-structures-every-period): bar counts as the day's, price-relative
settings by the period's typical bar against a day's, the year in the period's bars, a minute band never under two ticks."""
import pytest

from wb.engine.l2.periods import RANGE, settings
from wb.registry import PRIMITIVES, autoload


def test_a_day_keeps_every_default():
    assert settings("day") == {}


def test_a_week_scales_the_price_settings_and_keeps_the_bar_counts():
    s = settings("week")
    assert s["BOX"]["tol"] == pytest.approx(0.015 * RANGE["week"])          # 3.5%: a week's bar is 2.34 days'
    assert s["BOX"]["touch_tol"] == pytest.approx(0.025 * RANGE["week"])
    assert s["BOX"]["short.max_width"] == pytest.approx(0.12 * RANGE["week"])
    assert s["BOX"]["long.max_width"] == 0.6                                  # 82% would be no box: at most 60%
    assert s["BOX"]["year"] == 52 and s["BOX_DERIVED"]["tol"] == pytest.approx(0.02 * RANGE["week"])
    assert "long.min_len" not in s["BOX"] and "gap" not in s["BOX"]           # 大箱 60 bars and on, on any period


def test_a_minute_band_is_never_under_two_ticks_and_the_rest_keeps_the_days_proportions():
    """A 1-minute bar of a 10-yuan stock is 0.07%, a tick 0.1%: the band is two ticks (0.2%), and every price setting rises
    with it — an edge moving one tick is no new version, a box's widest stays eight bands."""
    cheap, dear = settings("m1", price=10.0, tick=0.01), settings("m1", price=50.0, tick=0.01)
    k = 0.002 / 0.015
    assert cheap["BOX"]["tol"] == pytest.approx(0.002)                                                    # 2 × 0.01 ÷ 10
    assert cheap["BOX"]["touch_tol"] == pytest.approx(0.025 * k) and cheap["BOX"]["rev"] == pytest.approx(0.01 * k)
    assert cheap["BOX"]["short.max_width"] == pytest.approx(0.12 * k) and cheap["SWING"]["min_change"] == pytest.approx(0.05 * k)
    assert dear["BOX"]["tol"] == pytest.approx(0.015 * RANGE["m1"])                                       # 0.075% > 0.04%
    assert settings("m60", price=10.0)["BOX"]["tol"] == pytest.approx(0.015 * RANGE["m60"])


def test_every_setting_is_one_the_primitive_declares():
    autoload("wb.engine.l2")
    for period in RANGE:
        for name, values in settings(period, price=10.0).items():
            declared = {p.name for p in PRIMITIVES[name].params}
            assert set(values) <= declared, (period, name, set(values) - declared)
