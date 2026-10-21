"""市场环境与板块强度 (spec §7.3) from the daily bars already stored — no extra subscription. Look at the market
first, then the industries, then the stock: breadth per day, how long the limit-up streaks are, each
index against its 60- and 200-day lines with its own box, and each industry's strength (申万一级 for A 股, 33 業種
for 日股). A value the data does not carry (limit flags in a test profile) is unknown, never zero."""
from datetime import timedelta

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame, index_states
from wb.engine.l1.functions.xs import SMALL
from wb.engine.l1.run import evaluate

DAYS = 60        # days of breadth shown
LOOKBACK = 400   # calendar days of bars read: the longest window is 60 bars (plus a limit-up streak's own length)
FORMULAS = {
    "up": "C > REF(C, 1)", "down": "C < REF(C, 1)", "chg": "C / REF(C, 1) - 1",
    "ret5": "C / REF(C, 5) - 1", "ret20": "C / REF(C, 20) - 1",
    "limit_up": "LIMIT_UP_CLOSE", "touched": "LIMIT_UP_TOUCH", "limit_down": "LIMIT_DOWN_CLOSE",
    "streak": "LIMIT_UP_STREAK", "high60": "NEW_HIGH(60)", "low60": "C < REF(LLV(L, 60), 1)",
}


def _count(col: str) -> pl.Expr:
    """How many are true; None when no listing's value is known (a missing flag is not a zero)."""
    return pl.when(pl.col(col).is_not_null().any()).then(pl.col(col).sum().cast(pl.Int64))


def breadth(v: pl.DataFrame) -> list[dict]:
    """Per day: listings with a bar, up, down (against the previous close), closed at the limit up, touched it but
    did not close there (炸板), closed at the limit down, new 60-day highs and lows."""
    broken = pl.when(pl.col("touched").is_null() | pl.col("limit_up").is_null()).then(None) \
        .otherwise(pl.col("touched") & ~pl.col("limit_up"))
    out = (v.with_columns(broken.alias("broken")).group_by("date").agg(
        pl.len().cast(pl.Int64).alias("total"), *[pl.col(c).fill_null(False).sum().cast(pl.Int64).alias(c)
                                                  for c in ("up", "down", "high60", "low60")],
        *[_count(c).alias(c) for c in ("limit_up", "broken", "limit_down")]).sort("date"))
    keys = ("total", "up", "down", "limit_up", "broken", "limit_down", "high60", "low60")
    return [{"date": str(r["date"]), **{k: r[k] for k in keys}} for r in out.iter_rows(named=True)]


def streaks(streak: pl.Series) -> dict | None:
    """Listings by how many limit-up closes in a row they have (1, 2, 3, 4, 5 or more); None when none is known."""
    known = streak.drop_nulls()
    if known.is_empty():
        return None
    return {"1": int((known == 1).sum()), "2": int((known == 2).sum()), "3": int((known == 3).sum()),
            "4": int((known == 4).sum()), "5+": int((known >= 5).sum())}


def sectors(v: pl.DataFrame) -> list[dict]:
    """One day's industries: members, the share of them up, the median change today and over 5 / 20 days, closes at
    the limit up, and the rank by the 5- and 20-day medians (1 strongest) — the same rank INDUSTRY_RANK gives a rule:
    ties share a rank, and an industry ranks only on more than SMALL members with a known return. An industry of
    SMALL members or fewer is listed apart (§7.3); listings without an industry are no group."""
    g = (v.filter(pl.col("industry").is_not_null()).group_by("industry").agg(
        pl.len().cast(pl.Int64).alias("members"), pl.col("up").cast(pl.Float64).mean().alias("up_ratio"),
        pl.col("chg").median().alias("chg"), pl.col("ret5").median().alias("ret5"), pl.col("ret20").median().alias("ret20"),
        _count("limit_up").alias("limit_up"), *[pl.col(f"ret{n}").count().alias(f"_known{n}") for n in (5, 20)])
         .with_columns((pl.col("members") <= SMALL).alias("small")))
    g = g.with_columns(*[pl.when(pl.col(f"_known{n}") > SMALL).then(pl.col(f"ret{n}")).rank("dense", descending=True)
                         .cast(pl.Int64).alias(f"rank{n}") for n in (5, 20)]).drop("_known5", "_known20")
    g = g.sort(["small", "rank20", "industry"], nulls_last=True)
    return g.to_dicts()


def _indexes(store: Store, snap) -> list[dict]:
    """Each index of the market: its close and change, the 60- and 200-day lines and where it stands (INDEX_STATE),
    and its latest box (the same BOX as the stocks', on the index's own bars)."""
    from wb.engine.l2.spec import compute

    bars = store.scan("index_bars_1d", snap).select("index_id", "date", "open", "high", "low", "close", "volume").collect()
    if bars.is_empty():
        return []
    st = index_states(bars.select("index_id", "date", "close"))
    boxes = compute(bars.rename({"index_id": "listing_id"}).sort("listing_id", "date"), ["BOX"])["BOX"]["boxes"]
    out = []
    for (iid,), g in st.group_by("index_id", maintain_order=True):
        g = g.sort("date")
        last, prev = g.row(-1, named=True), g.row(-2, named=True) if g.height > 1 else None
        b = (boxes.filter((pl.col("listing_id") == iid) & (pl.col("status") == "established") & pl.col("scale").is_in(["mid", "long"]))
             .sort("known_at").tail(1))
        box = None
        if b.height:
            r = b.row(0, named=True)
            box = {"scale": r["scale"], "start": str(r["start"]), "end": str(r["end"]) if r["end"] else None,
                   "top": r["top"], "bottom": r["bottom"], "width": r["width"], "ended": r["ended_at"] is not None,
                   "end_reason": r["end_reason"]}
        dist = lambda ma: last["close"] / last[ma] - 1 if last[ma] else None  # noqa: E731
        out.append({"index_id": iid, "date": str(last["date"]), "close": last["close"],
                    "chg": last["close"] / prev["close"] - 1 if prev and prev["close"] else None,
                    "ma60": last["ma60"], "ma200": last["ma200"], "dist60": dist("ma60"), "dist200": dist("ma200"),
                    "state": last["idx_state"], "box": box})
    return sorted(out, key=lambda i: i["index_id"])


def market_view(paths: Paths, market: str) -> dict:
    """Everything the 市场 page shows for the market's latest snapshot."""
    store = Store(paths, market)
    snap = store.latest()
    if snap is None:
        return {"day": None, "breadth": [], "streaks": None, "sectors": [], "indexes": [], "known": {"limits": False, "industry": False}}
    comp = Compiler(market, available=available_columns(store, snap))
    for name, formula in FORMULAS.items():
        comp.add(name, formula)
    prog = comp.program()
    frame = build_frame(store, market, paths.profile, prog.columns | {"industry"}, start=snap.as_of - timedelta(days=LOOKBACK),
                        snap=snap)
    calendar = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    v = evaluate(frame, prog, calendar).join(frame.select("listing_id", "date", "industry"), on=["listing_id", "date"])
    days = sorted(v["date"].unique().to_list())[-DAYS:]
    v = v.filter(pl.col("date").is_in(days))
    today = v.filter(pl.col("date") == days[-1]) if days else v
    return {"day": str(days[-1]) if days else None, "breadth": breadth(v), "streaks": streaks(today["streak"]),
            "sectors": sectors(today), "indexes": _indexes(store, snap),
            "known": {"limits": bool(today["limit_up"].is_not_null().any()), "industry": bool(today["industry"].is_not_null().any())}}
