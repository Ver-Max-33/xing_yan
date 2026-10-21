"""P0 acceptance helper: re-download a sample of stored bars and compare with the source's raw values."""
from datetime import date

import polars as pl

from wb.data.pipeline import Pipeline
from wb.listing import to_yahoo

FIELDS = ["open", "high", "low", "close", "volume", "amount"]


def spotcheck(pipe: Pipeline, n: int = 20, day: date | None = None, seed: int = 0) -> pl.DataFrame:
    """One row per (listing, field) with stored value, source value and ok flag."""
    bars = pipe.store.scan("bars_1d")  # one day, never the whole table
    day = day or bars.select(pl.col("date").max()).collect().item()
    pool = bars.filter((pl.col("date") == day) & ~pl.col("suspended").fill_null(False)).collect()
    sample = pool.sample(n=min(n, pool.height), seed=seed)
    prov = pipe.provider_for("bars_1d")
    if prov.unit_kinds["bars_1d"] == "date":
        fresh = prov.fetch("bars_1d", day.isoformat(), day, day)["bars_1d"]
    else:  # per-ticker sources: fetch up to today so later splits are undone the same way
        frames = [prov.fetch("bars_1d", to_yahoo(lid), day, pipe.today())["bars_1d"] for lid in sample["listing_id"]]
        fresh = pl.concat(frames, how="diagonal_relaxed")
    fresh = fresh.filter(pl.col("date") == day)
    joined = sample.select("listing_id", *FIELDS).join(
        fresh.select("listing_id", *[pl.col(f).cast(pl.Float64).alias(f"{f}_src") for f in FIELDS if f in fresh.columns]),
        on="listing_id", how="left")
    rows = []
    for r in joined.iter_rows(named=True):
        for f in FIELDS:
            a, b = r[f], r.get(f"{f}_src")
            if a is None and b is None:
                continue
            ok = a is not None and b is not None and abs(a - b) <= 1e-6 * max(1.0, abs(b))
            rows.append({"listing_id": r["listing_id"], "field": f, "stored": a, "source": b, "ok": ok})
    return pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "field": pl.Utf8, "stored": pl.Float64,
                                      "source": pl.Float64, "ok": pl.Boolean})
