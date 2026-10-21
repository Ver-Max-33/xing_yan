"""Which industry a listing is in on a day — one scheme across the app (design 2026-10-12-market-page-redesign §6.3): for
A shares 申万 2021 by each membership spell (一级 `name`, 二级 `l2_name`; the rules read the same spells), for 日股 the
33 業種 of the instruments list (one level, today's). The instruments list's own `industry` of A shares is Tushare's
label (白酒, 元器件, …): never shown, only placed in 申万 for a rule that names it (`tushare_home`)."""
from datetime import date

import polars as pl

from wb.data.store import Snapshot, Store

COLS = {"listing_id": pl.Utf8, "l1": pl.Utf8, "l2": pl.Utf8}
WHAT = {("CN", "l1"): "申万一级行业", ("CN", "l2"): "申万二级行业", ("JP", "l1"): "33 業種"}


def at(store: Store, snap: Snapshot | None, market: str, day: date) -> pl.DataFrame:
    """listing_id, l1, l2 on `day`: an A share's spell covering it — [in_date, out_date], a null out_date still in, the
    convention of the rules' frame (_in_intervals) —, a 日股 listing's 33 業種 (l2 null). Empty without the data."""
    if snap is None:
        return pl.DataFrame(schema=COLS)
    if market == "CN":
        if not snap.manifest.get("industry"):
            return pl.DataFrame(schema=COLS)
        rows = store.scan("industry", snap)
        l2 = pl.col("l2_name") if "l2_name" in rows.collect_schema().names() else pl.lit(None, pl.Utf8)
        got = (rows.filter((pl.col("in_date") <= day) & (pl.col("out_date").is_null() | (pl.col("out_date") >= day)))
               .select("listing_id", "in_date", pl.col("name").alias("l1"), l2.alias("l2")).collect())
        return got.sort("in_date").unique("listing_id", keep="last").select([pl.col(c).cast(t) for c, t in COLS.items()])
    inst = store.read("instruments", snap)
    if "industry" not in inst.columns:
        return pl.DataFrame(schema=COLS)
    return (inst.filter(pl.col("industry").is_not_null())
            .select("listing_id", pl.col("industry").alias("l1"), pl.lit(None, pl.Utf8).alias("l2")))


def parse(ref: str, market: str) -> tuple[str, str]:
    """`l1:名称` / `l2:名称` → (level, name); 日股 has one level."""
    level, _, name = (ref or "").partition(":")
    if level not in ("l1", "l2") or not name.strip():
        raise ValueError("行业要写成 l1:名称 或 l2:名称")
    if market != "CN" and level == "l2":
        raise ValueError("日股只有一级（33 業種）")
    return level, name.strip()


def members(store: Store, snap: Snapshot | None, market: str, ref: str, day: date) -> set[str]:
    """The listings in the industry `ref` on `day`; a ValueError says what is wrong with `ref` in this market."""
    level, name = parse(ref, market)
    hit = at(store, snap, market, day).filter(pl.col(level) == name)
    if hit.is_empty():
        raise ValueError(f"没有叫「{name}」的{WHAT[(market, level)]}")
    return set(hit["listing_id"].to_list())


def tushare_home(store: Store, snap: Snapshot | None, label: str) -> str | None:
    """The 申万一级 most A shares with Tushare's label `label` are in now (None when no listing has the label)."""
    if snap is None:
        return None
    inst = store.read("instruments", snap)
    if "industry" not in inst.columns:
        return None
    ids = inst.filter(pl.col("industry").str.to_lowercase() == label.lower())["listing_id"]
    if ids.is_empty():
        return None
    l1 = at(store, snap, "CN", snap.as_of).filter(pl.col("listing_id").is_in(ids.implode()))["l1"].drop_nulls()
    if l1.is_empty():
        return None
    counts = l1.value_counts()
    return counts.sort([counts.columns[1], counts.columns[0]], descending=[True, False]).row(0)[0]
