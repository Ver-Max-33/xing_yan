"""Data-layer review 2026-10-13: the K-line page's quality badge must keep showing a row the rules read as unknown after
the next daily snapshot (on the user's data: 力王股份 920627, 73 adj_identity rows 2017–2022 — the spec names it as shown
on the badge — showed nothing once the first daily update after the backfill was published)."""
import polars as pl

from tests.test_api import DAYS, H, client  # noqa: F401  (the fixture)
from wb.data.quality import Issue, check_bars
from wb.data.store import Store
from wb.datasets import conform


def test_a_bad_row_keeps_its_badge_after_the_next_daily_snapshot(client):  # noqa: F811
    c, paths = client
    st = Store(paths, "JP")
    bad = conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [DAYS[-5]], "open": [2800.0], "high": [2790.0],
                                "low": [2810.0], "close": [2805.0], "volume": [1e6], "amount": [2.8e9], "adj_factor": 1.0,
                                "share_factor": 1.0, "suspended": False}), "bars_1d")     # low above high: OHLC broken
    found = [i for i in check_bars(bad) if i.check == "ohlc"]
    st.publish({"bars_1d": bad}, as_of=DAYS[-1], issues={"bars_1d": found})          # the backfill: recorded with it
    nxt = conform(pl.DataFrame({"listing_id": ["JP:7203"], "date": [DAYS[-1]], "open": [2840.0], "high": [2850.0],
                                "low": [2830.0], "close": [2845.0], "volume": [1e6], "amount": [2.84e9], "adj_factor": 1.0,
                                "share_factor": 1.0, "suspended": False}), "bars_1d")
    st.publish({"bars_1d": nxt}, as_of=DAYS[-1], issues={"bars_1d": []})            # the next day's update: nothing new
    q = c.get("/api/instrument/JP:7203", headers=H).json()["quality"]
    assert {"check_name": "ohlc", "n": 1, "first": str(DAYS[-5]), "last": str(DAYS[-5])} in q
