"""A row the data check flags (adj_identity: its adjusted prices unknown, spec §4.6) inside a trade: the trade goes on
at the factor carried from the day before (「某天缺复权因子时沿用前一个」), never at a factor of 1 — which judged the raw
close against an adjusted line (a false stop) and booked the sale at a raw price (2026-10-13: 隆平高科 +1,133%, 000545
−75.8% on real data)."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.data.store import Store
from wb.datasets import conform
from wb.engine.l3.rule import load_rule
from wb.validate.plantrades import plan_trades

N, F, BAD = 60, 5.0, 40   # bars, the factor all along, the flagged bar


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
    paths = get_paths("test", tmp_path_factory.mktemp("flagged"))
    ds = days(N)
    close = np.linspace(10.0, 12.0, N)
    opn = np.concatenate([[10.0], close[:-1]]) + 0.01
    pre = np.concatenate([[10.0], close[:-1]])
    pre[BAD] = close[BAD - 1] * 0.9   # the exchange's 昨收 disagrees with the factor: the row is flagged adj_identity
    bars = conform(pl.DataFrame({
        "listing_id": "CN:600000", "date": ds, "open": opn, "high": np.maximum(opn, close) + 0.02, "low": np.minimum(opn, close) - 0.02,
        "close": close, "pre_close": pre, "volume": 1e6, "adj_factor": F, "up_limit": (pre * 1.1).round(2),
        "down_limit": (pre * 0.9).round(2), "suspended": False, "source": "t"}).with_columns(pl.col("date").cast(pl.Date)), "bars_1d")
    Store(paths, "CN").publish({
        "bars_1d": bars,
        "instruments": conform(pl.DataFrame({"listing_id": ["CN:600000"], "market": "CN", "code": "600000", "name": "x",
                                             "industry": "电子", "status": "L", "source": "t"}), "instruments"),
        "calendar": conform(pl.DataFrame({"date": ds, "is_open": True, "source": "t"}), "calendar"),
        "index_bars_1d": conform(pl.DataFrame({"index_id": "CN:IDX:000300", "date": ds, "open": 3000.0, "high": 3000.0,
                                               "low": 3000.0, "close": 3000.0, "source": "t"}), "index_bars_1d"),
    }, as_of=ds[-1], note="t")
    return paths, ds, close, opn


def rule(entry):
    return load_rule(f"""
id: hold
name: 拿 10 天
markets: [CN]
stages: [{{id: today, kind: event, when: "C > 0"}}]
signal_at: today.at
plan: {{entry: {entry}, stop: [{{line: "C * 0.9"}}], days: 10}}
""")


def test_a_flagged_row_inside_a_trade_is_an_ordinary_day(market):
    paths, ds, close, _ = market
    # bought at the close of bar 35, held over the flagged bar 40, out at the close of the 10th bar after the signal
    sig = pl.DataFrame({"listing_id": ["CN:600000"], "signal_date": [ds[35]], "rank0": [close[35] * F * 0.9]})
    t = plan_trades(paths, "CN", rule("close"), sig).row(0, named=True)
    assert t["reason"] == "days" and t["exit_date"] == ds[45]
    assert t["ret"] == pytest.approx(close[45] / close[35] - 1, abs=0.01)   # costs are well under a point


def test_a_flagged_entry_day_is_bought_at_its_factor(market):
    paths, ds, close, opn = market
    # signal on bar 39: bought at the open of the flagged bar 40
    sig = pl.DataFrame({"listing_id": ["CN:600000"], "signal_date": [ds[BAD - 1]], "rank0": [close[BAD - 1] * F * 0.9]})
    t = plan_trades(paths, "CN", rule("open"), sig).row(0, named=True)
    assert t["entry_adj"] == pytest.approx(opn[BAD] * F)
    assert t["reason"] == "days" and t["ret"] == pytest.approx(close[BAD - 1 + 10] / opn[BAD] - 1, abs=0.01)


def test_a_signal_on_the_data_s_last_day_is_not_bought_yet_never_a_halt(market):
    paths, ds, close, _ = market
    sig = pl.DataFrame({"listing_id": ["CN:600000"], "signal_date": [ds[-1]], "rank0": [close[-1] * F * 0.9]})
    assert plan_trades(paths, "CN", rule("open"), sig)["status"].to_list() == ["open"]
