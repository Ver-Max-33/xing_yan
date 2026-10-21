"""No structure may depend on the future (spec §5.8 rule 2): for any t, what a primitive reports as
known by t from the first t bars equals what it reports as known by t from the full history."""
from datetime import date, timedelta

import numpy as np
import polars as pl
from hypothesis import given, settings
from hypothesis import strategies as st

from wb.engine.l2.box import SCALES, detect
from wb.engine.l2.pivots import fractal, zigzag

D0 = date(2020, 1, 1)


def series(seed: int, n: int) -> pl.DataFrame:
    """Random walk with flat stretches, so ranges (and boxes) actually form."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.012, n) * np.where(rng.random(n) < 0.6, 0.35, 1.0)
    c = 10 * np.exp(np.cumsum(steps))
    o = np.concatenate(([c[0]], c[:-1])) * (1 + rng.normal(0, 0.003, n))
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    lim = rng.random(n) < 0.02
    return pl.DataFrame({"listing_id": "X", "date": [D0 + timedelta(days=i) for i in range(n)], "open": o, "high": hi,
                         "low": lo, "close": c, "volume": rng.uniform(500, 1500, n), "limit_up_close": lim})


def known(boxes, touches, events, day):
    """What the output says is known on `day` (later facts blanked)."""
    later = lambda col: pl.when(pl.col(col) > day).then(None).otherwise(pl.col(col)).alias(col)
    b = (boxes.filter(pl.col("known_at") <= day)
         .with_columns(pl.when(pl.col("ended_at") > day).then(None).otherwise(pl.col("end")).alias("end"),
                       later("ended_at"),
                       pl.when(pl.col("ended_at") > day).then(None).otherwise(pl.col("end_reason")).alias("end_reason"),
                       pl.when(pl.col("ended_at") > day).then(None).otherwise(pl.col("length")).alias("length"),
                       pl.when(pl.col("ended_at") > day).then(None).otherwise(pl.col("end_i")).alias("end_i"),
                       later("superseded_at"))
         .drop("touches_top", "touches_bottom")  # running counts: compared through `touches`
         .sort("box_id"))
    t = touches.filter(pl.col("known_at") <= day).sort("box_id", "date", "side")
    e = events.filter(pl.col("known_at") <= day).sort("box_id", "date", "event")
    return b, t, e


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), n=st.integers(120, 360), frac=st.floats(0.3, 0.95))
def test_boxes_are_prefix_consistent(seed, n, frac):
    df = series(seed, n)
    t = int(n * frac)
    day = df["date"][t]
    full = known(*detect(df, SCALES), day)
    part = known(*detect(df.head(t + 1), SCALES), day)
    for a, b in zip(full, part, strict=True):
        assert a.equals(b)


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), n=st.integers(60, 300), frac=st.floats(0.2, 0.95))
def test_pivots_are_prefix_consistent(seed, n, frac):
    df = series(seed, n)
    t = int(n * frac)
    h, lo = df["high"].to_numpy(), df["low"].to_numpy()
    for f in (lambda a, b: fractal(a, b, 2), lambda a, b: zigzag(a, b, np.full(len(a), 0.04))):
        full = f(h, lo)
        part = f(h[: t + 1], lo[: t + 1])
        mask = full[3] <= t
        for x, y in zip(full, part, strict=True):
            assert np.array_equal(x[mask], y[part[3] <= t])
