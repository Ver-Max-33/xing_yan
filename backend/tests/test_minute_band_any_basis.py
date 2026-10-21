"""Fails on HEAD (后复权 reads the tick against a price × 1.3: 0.17% instead of 0.22%), passes with
patches/minute_tick_floor_raw_price.diff."""
import math
from datetime import datetime, timedelta

import polars as pl
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import write
from wb.engine.l2.periods import settings
from wb.realtime.providers.yfinance_rt import MINUTES

H = {"X-WB-Token": "t"}


def test_a_minute_k_lines_band_is_two_ticks_of_the_traded_price_in_any_basis(tmp_path):
    # design 2026-10-12-structures-every-period: on minutes a band is never under two ticks of the stock's price — the
    # price it trades at (9 yuan: 0.22%), not the chart's 后复权 one (600519's factor is 1.3 in the demo data)
    paths = get_paths("test", tmp_path)
    write(paths, 320)
    folder = paths.market_dir("CN") / "intraday"
    folder.mkdir(parents=True)
    px = lambda k: 9.0 + 0.03 * math.sin(k / 4.0)   # noqa: E731
    ats = [datetime(2026, 9, 30, 9, 30) + timedelta(minutes=k) for k in range(120)] + \
          [datetime(2026, 9, 30, 13, 0) + timedelta(minutes=k) for k in range(120)]
    pl.DataFrame([("CN:600519", a, px(k), px(k) + 0.01, px(k) - 0.01, px(k), 1000.0) for k, a in enumerate(ats)],
                 schema=MINUTES, orient="row").write_parquet(folder / "date=2026-09-30.parquet")
    c = TestClient(create_app(tmp_path, token="t"))
    tol = {b: c.get("/api/structures", params={"listing_id": "CN:600519", "basis": b, "period": "m5"}, headers=H).json()["tol"]
           for b in ("raw", "qfq", "hfq")}
    want = settings("m5", price=9.0, tick=0.01)["BOX"]["tol"]
    assert tol["raw"] == pytest.approx(want, rel=0.02)
    assert tol["hfq"] == pytest.approx(tol["raw"]) and tol["qfq"] == pytest.approx(tol["raw"])
