import polars as pl
import pytest

from wb.engine.l1.functions.course import AMPLITUDE, GROUND_VOL, LIMIT_PCT_CN, MA_DIST, NEW_HIGH, SMALL_YANG_RUN, VOL_MULT
from wb.markets import limit_width, load_market


def test_amplitude():
    df = pl.DataFrame({"h": [11.0, 13.0, 12.0], "l": [10.0, 10.5, 9.0]})
    assert df.select(AMPLITUDE(pl.col("h"), pl.col("l"), 2))["h"].to_list() == [None, 0.3, 13 / 9 - 1]


def test_vol_mult_and_ground():
    df = pl.DataFrame({"v": [100.0, 250.0, 50.0, 60.0]})
    assert df.select(VOL_MULT(pl.col("v")))["v"].to_list() == [None, 2.5, 0.2, 1.2]
    assert df.select(GROUND_VOL(pl.col("v"), 3))["v"].to_list() == [None, None, True, False]


def test_small_yang_run():
    df = pl.DataFrame({"o": [10.0, 10.0, 10.1, 10.2, 10.3], "c": [10.0, 10.1, 10.2, 10.3, 11.5],
                       "pc": [10.0, 10.0, 10.1, 10.2, 10.3]})
    got = df.select(SMALL_YANG_RUN(pl.col("o"), pl.col("c"), pl.col("pc"), 3, 0.03).alias("y"))["y"].to_list()
    assert got == [None, None, False, True, False]  # last day is a big yang (+11.7%)


def test_ma_dist_and_new_high():
    df = pl.DataFrame({"c": [10.0, 10.0, 12.0], "h": [10.5, 10.4, 12.2]})
    assert df.select(MA_DIST(pl.col("c"), 2))["c"][-1] == pytest.approx(12 / 11 - 1)
    # not enough history yet -> unknown (null), never False (spec §1.5 principle 5)
    assert df.select(NEW_HIGH(pl.col("c"), pl.col("h"), 2).alias("y"))["y"].to_list() == [None, None, True]
    # a close a rounding above the earlier high is equal to it (EQ), not above it: no new high
    df = pl.DataFrame({"c": [10.0, 10.0, 12.0 + 1e-12], "h": [10.5, 12.0, 12.2]})
    assert df.select(NEW_HIGH(pl.col("c"), pl.col("h"), 2).alias("y"))["y"].to_list() == [None, None, False]


def test_cn_limit_pct_follows_exchange_prices():
    df = pl.DataFrame({"ul": [11.0, 12.0, 10.5], "pc": [10.0, 10.0, 10.0]})  # 10%, 20%, ST 5%
    assert df.select(LIMIT_PCT_CN(pl.col("ul"), pl.col("pc")))["ul"].to_list() == pytest.approx([0.1, 0.2, 0.05])


@pytest.mark.parametrize("base,width", [(99, 30), (100, 50), (2845, 500), (9999, 1500), (10000, 3000),
                                        (60_000_000, 10_000_000)])
def test_tse_price_limits(base, width):
    assert limit_width(load_market("JP"), base) == width
