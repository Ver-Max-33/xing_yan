"""A made-up market the size of the real A-share one, for timing the structure computation where real data cannot go
(the Windows check, .github/workflows/windows-check.yml): `uv run python packaging/bench_data.py DIR [LISTINGS]`.

6,000 listings over ~10 years of random walks (most listed all along, the rest from a random day) — ~12 million daily
bars, as many as the real CN market's 10.9 million — published as the test profile's CN snapshot, marked as demo data."""
import sys
from pathlib import Path

import numpy as np
import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.datasets import conform
from wb.devdata import DEMO, index_rows, trading_days


def bars(listings: int, days: list, seed: int = 7) -> pl.DataFrame:
    rng, n, parts = np.random.default_rng(seed), len(days), []
    for k in range(listings):
        start = 0 if rng.random() < 0.6 else int(rng.integers(0, n - 250))
        m = n - start
        walk = rng.uniform(3, 80) * np.exp(np.cumsum(rng.normal(0.0002, 0.022, m)))
        pre = np.round(np.concatenate([[walk[0]], walk[:-1]]), 2)
        up, down = np.round(pre * 1.1, 2), np.round(pre * 0.9, 2)
        close = np.clip(np.round(walk, 2), down, up)
        open_ = np.clip(np.round(pre * (1 + rng.uniform(-0.01, 0.01, m)), 2), down, up)
        high = np.minimum(np.round(np.maximum(open_, close) * (1 + rng.uniform(0, 0.015, m)), 2), up)
        low = np.maximum(np.round(np.minimum(open_, close) * (1 - rng.uniform(0, 0.015, m)), 2), down)
        volume = np.round(rng.lognormal(13.5, 0.6, m))
        parts.append(pl.DataFrame({
            "listing_id": f"CN:{600000 + k:06d}", "date": days[start:], "open": open_, "high": high, "low": low,
            "close": close, "pre_close": pre, "volume": volume, "amount": np.round(volume * (open_ + close) / 2),
            "adj_factor": 1.0, "share_factor": 1.0, "up_limit": up, "down_limit": down, "ul": None, "ll": None,
            "suspended": False, "source": "bench"}))
    return conform(pl.concat(parts), "bars_1d")


def write(root: Path, listings: int = 6000, n: int = 2450) -> int:
    days = trading_days(n)
    b = bars(listings, days)
    firsts = b.group_by("listing_id").agg(pl.col("date").min().alias("list_date")).sort("listing_id")
    inst = conform(firsts.with_columns(
        market=pl.lit("CN"), code=pl.col("listing_id").str.slice(3), exchange=pl.lit("XSHG"),
        name=pl.lit("模拟") + pl.col("listing_id").str.slice(3), name_en=pl.lit("bench"), board=pl.lit("主板"),
        status=pl.lit("L"), source=pl.lit("bench")), "instruments")
    actions = conform(pl.DataFrame(schema={"listing_id": pl.Utf8, "ex_date": pl.Date, "kind": pl.Utf8, "ratio": pl.Float64,
                                           "source": pl.Utf8}), "corp_actions")
    Store(Paths(root=root, profile="test"), "CN").publish({
        "bars_1d": b, "corp_actions": actions, "instruments": inst,
        "calendar": conform(pl.DataFrame({"date": days, "is_open": True, "source": "bench"}), "calendar"),
        "index_bars_1d": pl.concat([index_rows("CN:IDX:000001", days, 41, 3300.0), index_rows("CN:IDX:000300", days, 42, 3900.0)]),
    }, as_of=days[-1], note=DEMO)
    return b.height


if __name__ == "__main__":
    print(f"{write(Path(sys.argv[1]), *map(int, sys.argv[2:3])):,} bars")
