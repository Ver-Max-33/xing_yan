from datetime import date

import polars as pl
import pytest

from wb.data.normalize import apply_factors, hfq, yahoo_to_raw
from wb.datasets import conform

D = date


def yahoo_toyota():
    # 7203.T as returned by yfinance (auto_adjust=False): already divided by the 1:5 split.
    return pl.DataFrame({
        "date": [D(2021, 9, 28), D(2021, 9, 29), D(2021, 9, 30)],
        "open": [2084.0, 2052.0, 2088.0], "high": [2092.0, 2080.5, 2088.0],
        "low": [2050.0, 2040.5, 2000.0], "close": [2077.0, 2073.0, 2000.0],
        "volume": [189085000.0, 34887300.0, 33149100.0],
    })


def test_yahoo_to_raw_restores_pre_split_prices():
    splits = pl.DataFrame({"ex_date": [D(2021, 9, 29)], "ratio": [5.0]})
    raw = yahoo_to_raw(yahoo_toyota(), splits)
    assert raw["close"].to_list() == [10385.0, 2073.0, 2000.0]
    assert raw["volume"].to_list() == [37817000.0, 34887300.0, 33149100.0]


def test_yahoo_to_raw_without_splits_is_identity():
    df = yahoo_toyota()
    assert yahoo_to_raw(df, pl.DataFrame(schema={"ex_date": pl.Date, "ratio": pl.Float64})).equals(df)


def test_apply_factors_makes_hfq_continuous():
    bars = conform(pl.DataFrame({
        "listing_id": ["JP:7203"] * 3 + ["JP:6758"],
        "date": [D(2021, 9, 28), D(2021, 9, 29), D(2021, 9, 30), D(2021, 9, 29)],
        "close": [10385.0, 2073.0, 2000.0, 12000.0],
        "volume": [37817000.0, 34887300.0, 33149100.0, 1000.0],
    }), "bars_1d")
    actions = pl.DataFrame({"listing_id": ["JP:7203"], "ex_date": [D(2021, 9, 29)], "ratio": [5.0]})
    out = hfq(apply_factors(bars, actions, set_price=True))
    toyota = out.filter(pl.col("listing_id") == "JP:7203")
    assert toyota["adj_factor"].to_list() == [1.0, 5.0, 5.0]
    assert toyota["close_hfq"].to_list() == [10385.0, 10365.0, 10000.0]
    assert toyota["volume_hfq"][1] == pytest.approx(6977460.0)
    sony = out.filter(pl.col("listing_id") == "JP:6758")
    assert sony["adj_factor"][0] == 1.0 and sony["share_factor"][0] == 1.0


def test_apply_factors_cn_only_sets_share_factor():
    bars = conform(pl.DataFrame({
        "listing_id": ["CN:600519"] * 2, "date": [D(2020, 6, 1), D(2020, 6, 2)],
        "close": [10.0, 7.7], "adj_factor": [1.0, 1.3], "volume": [100.0, 130.0],
    }), "bars_1d")
    actions = pl.DataFrame({"listing_id": ["CN:600519"], "ex_date": [D(2020, 6, 2)], "ratio": [1.3]})
    out = apply_factors(bars, actions, set_price=False)
    assert out["adj_factor"].to_list() == [1.0, 1.3]  # untouched (comes from Tushare)
    assert out["share_factor"].to_list() == [1.0, 1.3]


def test_two_events_same_day_multiply():
    bars = conform(pl.DataFrame({"listing_id": ["JP:1301"], "date": [D(2022, 1, 5)]}), "bars_1d")
    actions = pl.DataFrame({"listing_id": ["JP:1301", "JP:1301"], "ex_date": [D(2022, 1, 5)] * 2,
                            "ratio": [2.0, 1.5]})
    assert apply_factors(bars, actions, set_price=True)["adj_factor"][0] == 3.0
