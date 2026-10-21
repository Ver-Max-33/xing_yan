import polars as pl

from tests.bars import bars
from tests.test_l2_prefix import known, series
from wb.engine.l2.slant import SlantParams, detect_slant


def test_rising_channel_then_a_break_below_the_sloping_bottom():
    # a tight rising channel: +0.5% a bar with swings of about ±1.5%, then a drop through its bottom
    path, base = [], 10.0
    for i in range(48):
        base *= 1.005
        path.append(base * (1 + 0.015 * [0, 0.6, 1, 0.6, 0, -0.6, -1, -0.6][i % 8]))
    path += [path[-1] * 0.97, path[-1] * 0.94, path[-1] * 0.92, path[-1] * 0.90, path[-1] * 0.89, path[-1] * 0.88, path[-1] * 0.87]
    b, e = detect_slant(bars(path))
    assert b.height >= 1
    first = b.row(0, named=True)
    assert first["top_slope"] > 0.002 and first["bottom_slope"] > 0.002
    assert first["end_reason"] == "下破离箱"
    assert "break_down" in e.filter(pl.col("box_id") == first["box_id"])["event"].to_list()


def test_slant_boxes_are_prefix_consistent():
    for seed in range(12):
        df = series(seed, 240)
        t = 160
        day = df["date"][t]
        fb, fe = detect_slant(df)
        pb, pe = detect_slant(df.head(t + 1))
        empty_touches = pl.DataFrame(schema={"box_id": pl.Utf8, "date": pl.Date, "side": pl.Utf8, "known_at": pl.Date})
        cols = ["touches_top", "touches_bottom", "end_i", "superseded_at"]
        add = [pl.lit(None).alias(c) for c in cols]
        a = known(fb.with_columns(add), empty_touches, fe, day)
        b = known(pb.with_columns(add), empty_touches, pe, day)
        for x, y in zip(a, b, strict=True):
            assert x.equals(y)


def test_a_bottom_line_falling_to_zero_is_no_channel():
    # lows 10 → 8 over 5 bars put the lower line at exactly 0 on bar 25 (real case: CN:600226);
    # the search must reject it, not divide by it
    import numpy as np

    from wb.engine.l2.slant import _search

    n = 26
    h, lo = np.full(n, 20.0), np.full(n, 15.0)
    h[1], h[6], lo[0], lo[5] = 11.6, 9.28, 10.0, 8.0
    c = (h + lo) / 2
    p_idx, p_kind = np.array([0, 1, 5, 6]), np.array([-1, 1, -1, 1], dtype=np.int8)
    p = SlantParams()
    prm = (p.min_len, p.max_len, p.tol, p.gap, p.min_slope, p.slope_tol, p.max_width, p.inside, p.spike_max)
    assert _search(25, 0, c, h, lo, c, p_idx, p_kind, 4, prm)[0] == 0
