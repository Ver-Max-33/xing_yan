"""The random control draws from the market's stocks, as the rule's signals do (§7.1 stock pool: JP leaves out ETFs,
REITs, PRO Market … — `not_stocks`). 2026-10-13 on real data: 8.5% of 上品's JP control trades were ETFs, REITs and
出資証券, which no signal can be."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.data.store import Store
from wb.datasets import conform
from wb.engine.l3.rule import load_rule
from wb.validate.control import control_signals

STOCKS = [f"JP:{1000 + i}" for i in range(6)]
FUNDS = [f"JP:{1300 + i}" for i in range(6)]


@pytest.fixture(scope="module")
def jp(tmp_path_factory):
    from wb.config import get_paths
    paths = get_paths("test", tmp_path_factory.mktemp("jp_pool"))
    ds, d = [], date(2026, 9, 30)
    while len(ds) < 40:
        if d.weekday() < 5:
            ds.append(d)
        d -= timedelta(days=1)
    ds = ds[::-1]
    lids = STOCKS + FUNDS
    px = np.linspace(1000, 1100, len(ds))
    bars = conform(pl.DataFrame({
        "listing_id": np.repeat(lids, len(ds)), "date": ds * len(lids), "open": np.tile(px, len(lids)), "high": np.tile(px + 5, len(lids)),
        "low": np.tile(px - 5, len(lids)), "close": np.tile(px, len(lids)), "volume": 1e5, "adj_factor": 1.0, "suspended": False,
        "source": "t"}).with_columns(pl.col("date").cast(pl.Date)), "bars_1d")
    Store(paths, "JP").publish({
        "bars_1d": bars,
        "instruments": conform(pl.DataFrame({"listing_id": lids, "market": "JP", "code": [x[3:] for x in lids], "name": lids,
                                             "board": ["プライム"] * len(STOCKS) + ["ETF・ETN"] * len(FUNDS), "status": "L",
                                             "source": "t"}), "instruments"),
        "calendar": conform(pl.DataFrame({"date": ds, "is_open": True, "source": "t"}), "calendar"),
        "index_bars_1d": conform(pl.DataFrame({"index_id": "JP:IDX:TOPIX", "date": ds, "open": 2000.0, "high": 2000.0, "low": 2000.0,
                                               "close": 2000.0, "source": "t"}), "index_bars_1d"),
    }, as_of=ds[-1], note="t")
    return paths, ds


def test_controls_are_stocks_never_funds(jp):
    paths, ds = jp
    rule = load_rule('id: r\nname: r\nmarkets: [JP]\nstages: [{id: x, kind: event, when: "C > 0"}]\nsignal_at: x.at\n')
    trades = pl.DataFrame({"listing_id": [STOCKS[0]] * 10, "signal_date": ds[20:30], "entry_adj": 1000.0, "stop0": 950.0})
    c = control_signals(paths, "JP", rule, trades, per_day=3, seed=0)
    assert c.height > 0
    assert not c["listing_id"].is_in(FUNDS).any()
