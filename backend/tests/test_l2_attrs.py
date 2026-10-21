"""Box context and position (spec §5.4 属性): known from bars up to the box's start."""
import polars as pl

from tests.bars import SWING, bars
from wb.engine.l2.attrs import AttrParams, box_attrs
from wb.engine.l2.box import detect


def mid_attrs(path, **kw):
    """The first big box's context and position."""
    df = bars(path)
    b, _, _ = detect(df)
    return box_attrs(b.filter(pl.col("scale") != "short").sort("known_at"), df, AttrParams(**kw)).row(0, named=True)


def test_context_is_the_run_into_the_box():
    up = [8.0, 8.3, 8.6, 8.9, 9.2, 9.5, 9.8, 9.95] + SWING * 4
    down = [12.0, 11.6, 11.2, 10.8, 10.4, 10.1, 9.8] + SWING[2:] * 4
    assert mid_attrs(up)["context"] == "上横"
    assert mid_attrs(down)["context"] == "下横"


def test_position_counts_the_waves_before_the_box():
    # three 10%+ legs up from the year's low with pullbacks between, then the range: 高位 by waves,
    # although the gain from the low stays under the 100% threshold
    legs = []
    price = 6.0
    for _ in range(3):
        legs += [price * (1 + 0.04 * k) for k in range(5)]          # +16%
        price = legs[-1]
        legs += [price * (1 - 0.03 * k) for k in range(1, 3)]       # -6%
        price = legs[-1]
    # the range sits 10% under the last pullback, so the box starts after the third top is confirmed
    path = [6.0] * 130 + legs + [price * 0.95, price * 0.92] + [p * price * 0.9 / 9.5 for p in SWING * 4]
    df = bars(path)
    box = pl.DataFrame({"box_id": ["b"], "listing_id": ["CN:600000"], "start_i": [130 + len(legs) + 4]})   # in the range
    fine = {"swing": 0.05, "wave": 0.10}  # the defaults count bigger waves; this path has 16% legs
    a = box_attrs(box, df, AttrParams(high_gain=5.0, mid_gain=4.0, **fine)).row(0, named=True)
    assert a["waves"] == 3 and a["position"] == "高位"
    a = box_attrs(box, df, AttrParams(high_gain=5.0, mid_gain=4.0, high_waves=4, **fine)).row(0, named=True)
    assert a["position"] == "中继"


def test_a_wave_must_set_a_higher_high():
    # four 10%+ legs up from the same floor that never top the first: a range, one wave; legs that
    # keep making higher highs: one wave each (走了几波)
    import numpy as np

    from wb.engine.l2.attrs import _waves

    lo = np.full(70, 10.1)
    lo[10] = 10.0  # the year's low
    idx = np.array([10, 15, 20, 25, 30, 35, 40, 45, 50])
    kind = np.array([-1, 1, -1, 1, -1, 1, -1, 1, -1], dtype=np.int8)
    known = idx + 2
    ranging = np.array([10.0, 11.6, 10.1, 11.5, 10.1, 11.4, 10.1, 11.3, 10.1])
    rising = np.array([10.0, 11.6, 10.9, 12.8, 12.0, 14.1, 13.3, 15.6, 14.6])
    starts = np.array([60])
    assert _waves(lo, idx, kind, ranging, known, starts, 250, 0.10)[0] == 1
    assert _waves(lo, idx, kind, rising, known, starts, 250, 0.10)[0] == 4
