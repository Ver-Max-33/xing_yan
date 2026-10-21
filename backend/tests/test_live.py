"""Real-network smoke tests: uv run pytest -m live
Tushare / J-Quants tests run only when their keys are available (env, .env or keychain)."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.datasets import conform
from wb.secrets import get_secret

pytestmark = pytest.mark.live


def test_jpx_list_live():
    from wb.data.providers.jpx_list import JpxList

    df = conform(JpxList().fetch("instruments", "all", date.today(), date.today())["instruments"], "instruments")
    assert df.height > 3000 and "JP:130A" in df["listing_id"].to_list()


def test_exchange_list_cn_live():
    from wb.data.providers.exchange_list_cn import ExchangeListCN

    df = conform(ExchangeListCN().fetch("instruments", "all", date.today(), date.today())["instruments"], "instruments")
    ids = set(df["listing_id"].to_list())
    assert df.height > 4000 and {"CN:600519", "CN:000001", "CN:688001", "CN:300750"} <= ids


def test_yfinance_toyota_split_live():
    from wb.data.providers.yfinance_test import YFinance

    out = YFinance("JP").fetch("bars_1d", "7203.T", date(2021, 9, 27), date(2021, 10, 1))
    bars = conform(out["bars_1d"], "bars_1d").sort("date")
    assert bars.filter(pl.col("date") == date(2021, 9, 28))["close"][0] == pytest.approx(10385.0)
    assert out["corp_actions"]["ratio"].to_list() == [5.0]


@pytest.mark.skipif(not get_secret("tushare_token"), reason="no Tushare token")
def test_tushare_daily_live():
    from wb.data.providers.tushare import Tushare

    t = Tushare()
    day = date.today() - timedelta(days=1)
    for _ in range(15):  # walk back past weekends and holidays to the latest day with data
        df = t.fetch("bars_1d", day.isoformat(), day, day)["bars_1d"]
        if df.height:
            break
        day -= timedelta(days=1)
    bars = conform(df, "bars_1d")
    assert bars.height > 4000
    assert bars["adj_factor"].null_count() < bars.height * 0.01  # 2000+ points: factors present


@pytest.mark.skipif(not get_secret("jquants_api_key"), reason="no J-Quants key")
def test_jquants_probe_live():
    from wb.data.providers.jquants import JQuants

    probes = {p.dataset: p for p in JQuants().probe()}
    assert probes["instruments"].available


@pytest.mark.skipif(not get_secret("jquants_api_key"), reason="no J-Quants key")
def test_jquants_bars_day_live():
    # The prod JP path on real data: one full day inside the subscription window, mostly traded rows,
    # units right (turnover / volume inside the day's range).
    from wb.data.providers.jquants import JQuants

    jq = JQuants()
    window = {p.dataset: p for p in jq.probe()}["bars_1d"]
    day = window.latest_date - timedelta(days=7)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    bars = conform(jq.fetch("bars_1d", day.isoformat(), day, day)["bars_1d"], "bars_1d")
    traded = bars.filter(~pl.col("suspended").fill_null(False))
    assert bars.height > 3500 and traded.height > 0.9 * bars.height
    vwap = traded.filter(pl.col("volume") > 0).select(
        ((pl.col("amount") / pl.col("volume")).is_between(pl.col("low") * 0.98, pl.col("high") * 1.02)).mean())
    assert vwap.item() > 0.99


def test_exchange_reference_live():
    from wb.data.verify import sse_szse_reference

    end = date.today()
    df = sse_szse_reference()(["CN:600519", "CN:000001", "CN:688001"], end - timedelta(days=20), end)
    assert set(df["listing_id"]) == {"CN:600519", "CN:000001", "CN:688001"}
    v = df.filter(pl.col("listing_id") == "CN:000001")
    assert ((v["amount"] / v["volume"]).is_between(v["low"] * 0.98, v["high"] * 1.02)).all()  # units right
