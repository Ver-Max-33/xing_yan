"""交易计划 on a market's stored data (design 2026-10-09 §5): the signals carry their plan's lines read on the signal bar,
the bars their 卖出条件 and limits, a chunk of listings at a time — on a small synthetic A-share store, one trade checked
by hand against the store's own bars."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.data.store import Store
from wb.datasets import conform
from wb.engine.l3.rule import load_rule
from wb.validate.engine import Variant, market_signals
from wb.validate.plantrades import plan_rule, plan_trades

L, N = 12, 260
RULE = """
id: up
name: 站上 20 日线
markets: [CN]
stages:
  - {id: today, kind: event, when: "CROSS(C, MA(C, 20))"}
signal_at: today.at
plan:
  entry: close
  stop: [{line: "MA(C, 20) * 0.95", label: 20 日线下 5%}]
  exit: [{when: "C < MA(C, 5)", label: 跌破 5 日线}]
  days: 10
"""


def days(n, end=date(2026, 9, 30)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    from wb.config import get_paths
    paths = get_paths("test", tmp_path_factory.mktemp("plantrades"))
    rng = np.random.default_rng(3)
    ds = days(N)
    close = 10 * np.exp(np.cumsum(rng.normal(0.0, 0.02, (L, N)), axis=1))
    opn = np.concatenate([close[:, :1], close[:, :-1]], axis=1) * (1 + rng.normal(0, 0.003, (L, N)))
    prev = np.concatenate([close[:, :1], close[:, :-1]], axis=1)
    lids = [f"CN:{600000 + i}" for i in range(L)]
    idx = 3000 * (close / close[:, :1]).mean(axis=0)
    bars = conform(pl.DataFrame({
        "listing_id": np.repeat(lids, N), "date": ds * L, "open": opn.ravel(), "high": (np.maximum(opn, close) * 1.01).ravel(),
        "low": (np.minimum(opn, close) * 0.99).ravel(), "close": close.ravel(), "volume": 1e6, "adj_factor": 1.0,
        "up_limit": (prev * 1.1).round(2).ravel(), "down_limit": (prev * 0.9).round(2).ravel(), "suspended": False,
        "source": "t"}).with_columns(pl.col("date").cast(pl.Date)), "bars_1d")
    Store(paths, "CN").publish({
        "bars_1d": bars,
        "instruments": conform(pl.DataFrame({"listing_id": lids, "market": "CN", "code": [x[3:] for x in lids], "name": lids,
                                             "industry": "电子", "status": "L", "source": "t"}), "instruments"),
        "calendar": conform(pl.DataFrame({"date": ds, "is_open": True, "source": "t"}), "calendar"),
        "index_bars_1d": conform(pl.DataFrame({"index_id": "CN:IDX:000300", "date": ds, "open": np.concatenate([idx[:1], idx[:-1]]),
                                               "high": idx, "low": idx, "close": idx, "source": "t"}), "index_bars_1d"),
    }, as_of=ds[-1], note="t")
    return paths, ds


def test_each_signal_is_traded_by_its_plan_on_the_stored_bars(market):
    paths, ds = market
    rule = load_rule(RULE)
    sig = market_signals(paths, "CN", [Variant(plan_rule(rule))], ds[30], ds[-1], lines=True)[0]
    t = plan_trades(paths, "CN", rule, sig)
    assert t.height == sig.height and t.height > 20
    bought = t.filter(pl.col("status") == "done")
    assert bought.height > 10
    for r in bought.iter_rows(named=True):
        assert all(f["date"] >= r["signal_date"] for f in r["fills"]) and r["held"] <= 10
        assert r["reason"] in ("stop0", "exit0", "days")
    # one trade by hand: from the signal close, the first close under MA(C, 20) × 0.95 (read on the signal day) or
    # under MA(C, 5), else the 10th day
    store = Store(paths, "CN")
    b = (store.read("bars_1d").filter(pl.col("listing_id") == bought["listing_id"][0]).sort("date")
         .with_columns(pl.col("close").rolling_mean(20).alias("ma20"), pl.col("close").rolling_mean(5).alias("ma5")))
    r = bought.row(0, named=True)
    i0 = b["date"].to_list().index(r["signal_date"])
    line = b["ma20"][i0] * 0.95
    k = next((i for i in range(i0 + 1, min(i0 + 11, b.height)) if b["close"][i] < line or b["close"][i] < b["ma5"][i]), i0 + 10)
    assert r["entry_price"] == pytest.approx(b["close"][i0]) and r["fills"][-1]["date"] == b["date"][k]
    buy, sell = 0.00025 + 0.00001 + 0.001, 0.00025 + 0.00001 + 0.0005 + 0.001
    assert r["ret"] == pytest.approx(b["close"][k] * (1 - sell) / (b["close"][i0] * (1 + buy)) - 1)
