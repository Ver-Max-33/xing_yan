"""Data-layer review 2026-10-13: one listing answer that leaves out stocks still trading must not mark them delisted (on a
replayed 10-12 update, 60 of them were 已退市 in search until the next answer); one that stopped trading still is."""
import polars as pl

from tests.test_pipeline import DAYS, FakeJP, pipe


class ShortList(FakeJP):
    """Its listing answer leaves out 130A (which still trades) and 9999 is in no list (as in FakeJP)."""
    def fetch(self, dataset, unit, start, end):
        out = super().fetch(dataset, unit, start, end)
        if dataset == "instruments":
            out["instruments"] = out["instruments"].filter(pl.col("listing_id") != "JP:130A")
        return out


class Stopped(ShortList):
    """130A left out of the list and no longer trading (really delisted)."""
    def fetch(self, dataset, unit, start, end):
        out = super().fetch(dataset, unit, start, end)
        if dataset == "bars_1d":
            out["bars_1d"] = out["bars_1d"].filter(pl.col("listing_id") != "JP:130A")
        return out


def status(p, lid):
    return p.store.read("instruments").filter(pl.col("listing_id") == lid)["status"][0]


def test_a_stock_left_out_of_one_list_but_trading_is_not_delisted(paths):
    pipe(paths, FakeJP()).backfill(DAYS[0], DAYS[0])
    p = pipe(paths, ShortList())
    p.update(DAYS[1], wait=False)
    assert status(p, "JP:130A") == "L"
    q = pipe(paths, Stopped())
    q.update(DAYS[2], wait=False)
    assert status(q, "JP:130A") == "D"
