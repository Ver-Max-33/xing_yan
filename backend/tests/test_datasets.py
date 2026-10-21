from datetime import date

import polars as pl
import pytest

from wb.datasets import DATASETS, SchemaError, conform, empty


def test_conform_orders_casts_and_fills_nulls():
    df = pl.DataFrame({"date": [date(2026, 10, 1)], "listing_id": ["JP:7203"], "close": [2800]})
    out = conform(df, "bars_1d")
    assert out.columns == list(DATASETS["bars_1d"].columns)
    assert out["close"].dtype == pl.Float64 and out["close"][0] == 2800.0
    assert out["volume"][0] is None and out["suspended"][0] is None


def test_conform_rejects_unknown_and_missing_key():
    with pytest.raises(SchemaError, match="unknown"):
        conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [date(2026, 1, 5)], "vol": [1]}), "bars_1d")
    with pytest.raises(SchemaError, match="missing key"):
        conform(pl.DataFrame({"listing_id": ["JP:7203"]}), "bars_1d")


def test_conform_rejects_uncastable():
    df = pl.DataFrame({"listing_id": ["JP:7203"], "date": [date(2026, 1, 5)], "close": ["abc"]})
    with pytest.raises(SchemaError):
        conform(df, "bars_1d")


def test_empty_has_schema():
    assert empty("calendar").schema == pl.Schema(DATASETS["calendar"].columns)


def test_conform_rejects_null_keys():
    with pytest.raises(SchemaError, match="null key"):
        conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [None]}, schema={"listing_id": pl.Utf8, "date": pl.Date}), "bars_1d")
