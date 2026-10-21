import polars as pl

from wb.charts.series import build
from wb.data.store import Store
from wb.devdata import write


def test_devdata_publishes_both_markets_with_adjustments(paths):
    write(paths, n=260)
    jp = Store(paths, "JP").read("bars_1d")
    cn = Store(paths, "CN").read("bars_1d")
    assert jp["listing_id"].n_unique() == 3 and cn["listing_id"].n_unique() == 2
    toyota = jp.filter(pl.col("listing_id") == "JP:7203").sort("date")
    assert toyota["adj_factor"].unique().sort().to_list() == [1.0, 5.0]  # the 1:5 split
    # backward-adjusted prices stay continuous across the split
    s = build(toyota, "JP:7203", "JP", "day", "hfq").frame
    jumps = (s["close"] / s["close"].shift(1) - 1).abs().drop_nulls()
    assert jumps.max() < 0.15
    moutai = cn.filter(pl.col("listing_id") == "CN:600519").sort("date")
    assert moutai["adj_factor"].unique().sort().to_list() == [1.0, 1.3]
    assert Store(paths, "JP").read("instruments").filter(pl.col("code") == "130A").height == 1


def test_devdata_names_are_in_the_form_the_pipeline_stores(paths):
    # the pipeline folds JPX's full-width letters (NFKC); demo data shown as the app would show real data
    import unicodedata
    write(paths, n=260)
    names = Store(paths, "JP").read("instruments")["name"].to_list()
    assert names and all(n == unicodedata.normalize("NFKC", n) for n in names)
