"""Primitive parameters that filter or switch methods (spec §5.4 V1 原语表)."""
import polars as pl

from tests.test_l2_contract import with_dif
from tests.test_l2_prefix import series
from wb.engine.l2.spec import compute


def test_swing_and_divergence_filters_and_the_atr_pivots():
    df = with_dif(series(3, 400))
    every = compute(df, ["SWING"], {"SWING": {"min_change": 0.05}})["SWING"]["swings"]
    ups = compute(df, ["SWING"], {"SWING": {"min_change": 0.05, "direction": "up", "min_bars": 5}})["SWING"]["swings"]
    assert ups.height > 0 and set(ups["direction"]) == {"up"} and ups["bars"].min() >= 5
    assert ups.height == every.filter((pl.col("direction") == "up") & (pl.col("bars") >= 5)).height
    bull = compute(df, ["MACD_DIV"], {"MACD_DIV": {"kind": "bull"}})["MACD_DIV"]["divergences"]
    assert set(bull["kind"]) <= {"bull"}
    atr = compute(df, ["PIVOTS"], {"PIVOTS": {"method": "atr", "atr_m": 2.0}})["PIVOTS"]["pivots"]
    pct = compute(df, ["PIVOTS"], {"PIVOTS": {"method": "pct"}})["PIVOTS"]["pivots"]
    assert atr.height > 0 and not atr.equals(pct)
