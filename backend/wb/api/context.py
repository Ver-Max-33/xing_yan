"""Request context: per-market data profile (spec §4.3), stores and small caches.

SQLite connections are opened per call (FastAPI runs sync endpoints in a thread pool and a
connection must stay on its thread); parquet reads are cached by snapshot id, so a new snapshot
invalidates them automatically.
"""
import threading
from collections import OrderedDict
from datetime import date
from pathlib import Path

import polars as pl

from wb.config import Paths, get_paths
from wb.data.store import Snapshot, Store
from wb.engine.l1.frame import from_listing
from wb.listing import parse_id
from wb.markets import MARKETS
from wb.workspace.search import build_index
from wb.workspace.store import Workspace



def _with_turnover(store, snap, daily: pl.DataFrame) -> pl.DataFrame:
    """The day's 换手率 (valuation, %) beside its bar — 筹码分布 spreads it (spec §8.6); none where the snapshot has none."""
    if not snap.manifest.get("valuation") or daily.is_empty():
        return daily.with_columns(pl.lit(None, pl.Float64).alias("turnover_rate"))
    t = (store.scan("valuation", snap).filter(pl.col("listing_id") == daily["listing_id"][0])
         .select("date", pl.col("turnover_rate")).collect())
    return daily.join(t, on="date", how="left")

class LRU:
    def __init__(self, size: int):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key, make):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        value = make()
        with self.lock:
            self.data[key] = value
            while len(self.data) > self.size:
                self.data.popitem(last=False)
        return value


class Context:
    def __init__(self, root: str | Path | None = None):
        self.root = get_paths("test", root).root
        self.daily_cache = LRU(64)
        self.index_cache = LRU(4)
        self.misc_cache = LRU(32)

    def workspace(self) -> Workspace:
        return Workspace(get_paths("test", self.root))

    def paths(self, market: str) -> Paths:
        return get_paths(self.workspace().profile(market), self.root)

    def store(self, market: str) -> Store:
        return Store(self.paths(market), market)

    def latest(self, market: str) -> Snapshot | None:
        return self.store(market).latest()

    def daily(self, listing_id: str) -> tuple[Snapshot | None, pl.DataFrame]:
        market, _ = parse_id(listing_id)
        store = self.store(market)
        snap = store.latest()
        if snap is None:
            return None, store.read("bars_1d").head(0)
        df = self.daily_cache.get((store.base, snap.id, listing_id), lambda: _with_turnover(   # a 北交所 stock from its first
            store, snap, from_listing(store.scan("bars_1d", snap).filter(pl.col("listing_id") == listing_id), store, market, snap)   # day there
            .collect().sort("date")))
        return snap, df

    def calendar_open(self, market: str) -> list[date]:
        store = self.store(market)
        snap = store.latest()
        if snap is None:
            return []
        return self.misc_cache.get(("cal", store.base, snap.id), lambda: sorted(
            store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()))

    def instruments(self, market: str) -> pl.DataFrame:
        store = self.store(market)
        snap = store.latest()
        return self.misc_cache.get(("inst", store.base, snap.id if snap else None),
                                   lambda: store.read("instruments", snap))

    def search_index(self) -> pl.DataFrame:
        snaps = tuple((m, s.id if (s := self.latest(m)) else None) for m in MARKETS)
        return self.index_cache.get(snaps, lambda: pl.concat(
            [build_index(self.instruments(m)) for m in MARKETS], how="diagonal_relaxed"))
