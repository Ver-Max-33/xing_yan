"""Fails on HEAD (left_pressure keeps only the versions never superseded in the end), passes with the patch.
Run: cd backend && uv run --frozen pytest -q -p no:cacheprovider <this file> (with the patched more.py in place)."""
from datetime import date, timedelta

import polars as pl

from tests.bars import bars
from tests.test_l2_derived import boxes
from wb.engine.l2.more import left_pressure

D = lambda i: date(2024, 1, 1) + timedelta(days=i)


def test_pressure_reads_the_boxes_as_they_stood_that_day():
    # spec §5.8: a box that ended on bar 40 is pressure on bar 100 though an outer box takes it in on bar 150 (its
    # version's superseded_at, known only then); its version redrawn on bar 30, before it ended, never is
    b = boxes(("A.0", "mid", D(0), D(25), D(40), D(45), "下破离箱", 12.5, 11.5),
              ("A.1", "mid", D(0), D(30), D(40), D(45), "下破离箱", 12.0, 11.0)).with_columns(
        pl.Series("superseded_at", [D(30), D(150)], dtype=pl.Date))
    df = bars([10.0] * 300).with_columns(pl.Series("date", [D(i) for i in range(300)]))
    at = pl.DataFrame({"listing_id": ["CN:600000"] * 2, "date": [D(100), D(200)]})
    p = left_pressure(b, df, at).sort("date")
    assert p["pressure_box_id"].to_list() == ["A.1", None]
