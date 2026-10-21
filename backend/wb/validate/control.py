"""随机对照组 (design 2026-10-09 §6.2): does the rule pick better stocks than a random one bought the same day, the same
way? On each day a rule's signals were traded, stocks are drawn at random (a seeded order, `draw`) from those trading
that day in the rule's own pool (its universe as one condition: not ST, listed long enough, …) — never one of that
day's signals — three times as many as the day's control needs, so a draw that cannot be bought (a limit-up open, a
halt) gives way to the next. Each carries one of the day's bought signals' 失效线 and 目标位 as shares of that signal's
entry price, in turn: the same distance to being wrong and to taking profit, not drawn on any structure; the plan's
卖出条件, 不创新高 and days run on the control's own bars."""
import numpy as np
import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame, not_stocks
from wb.engine.l1.run import evaluate
from wb.engine.l3.match import universe_formula
from wb.engine.l3.rule import Rule
from wb.validate.engine import CHUNK

DRAW = 3   # candidates a day, for each place of the day's control


def _pool(store: Store, market: str, profile: str, snap, rule: Rule, pairs: pl.DataFrame) -> pl.DataFrame:
    """The (listing_id, signal_date) pairs in the rule's pool on that day."""
    text, _ = universe_formula(rule, market, available_columns(store, snap))
    if text is None or pairs.is_empty():
        return pairs
    comp = Compiler(market, {p.id: p.value for p in rule.params}, available_columns(store, snap))
    comp.add("in_pool", text, want="cond")
    prog = comp.program()
    cal = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    lids = sorted(pairs["listing_id"].unique().to_list())
    keep = []
    for i in range(0, len(lids), CHUNK):
        chunk = lids[i:i + CHUNK]
        ok = (evaluate(build_frame(store, market, profile, prog.columns, chunk, snap=snap), prog, cal)
              .filter(pl.col("in_pool").fill_null(False)).select("listing_id", pl.col("date").alias("signal_date")))
        keep.append(pairs.filter(pl.col("listing_id").is_in(chunk)).join(ok, on=["listing_id", "signal_date"], how="semi"))
    return pl.concat(keep)


def control_signals(paths: Paths, market: str, rule: Rule, trades: pl.DataFrame, per_day: int = 20, seed: int = 0) -> pl.DataFrame:
    """`trades`: the rule's signals traded by its plan (listing_id, signal_date, entry_adj, stop0…, take0…). Per signal
    day with a bought signal: up to DRAW × per_day candidates in a seeded random order (`draw` 0, 1, …), each with one
    of the day's bought signals' lines as shares of its entry price (stop0…, take0…, for plan_trades(relative=True))."""
    store = Store(paths, market)
    snap = store.latest()
    names = [c for c in trades.columns if (c.startswith("stop") or c.startswith("take")) and c[4:].isdigit()]
    bought = (trades.filter(pl.col("entry_adj").is_not_null())
              .select("signal_date", *[(pl.col(c) / pl.col("entry_adj")).alias(c) for c in names]))
    if bought.is_empty():
        return pl.DataFrame(schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "draw": pl.Int64, **{c: pl.Float64 for c in names}})
    days = sorted(bought["signal_date"].unique().to_list())
    lf = store.scan("bars_1d", snap).filter(pl.col("date").is_in(days) & pl.col("close").is_not_null()
                                            & ~pl.col("suspended").fill_null(False))
    if (funds := not_stocks(store, market, snap)) is not None:   # the stock pool, as the rule's signals read it
        lf = lf.join(funds, on="listing_id", how="anti")
    trading = lf.select("listing_id", pl.col("date").alias("signal_date")).collect()
    trading = trading.join(trades.select("listing_id", "signal_date"), on=["listing_id", "signal_date"], how="anti")
    rng = np.random.default_rng(seed)
    want = DRAW * per_day
    picked = []
    by_day = trading.partition_by("signal_date", as_dict=True)
    for d in days:   # more than the pool will keep, in their random order; the pool cuts them; DRAW × per_day stay
        part = by_day.get((d,))
        if part is None:
            continue
        ids = part["listing_id"].sort().to_list()
        order = rng.permutation(len(ids))
        picked.append(pl.DataFrame({"listing_id": [ids[i] for i in order[:4 * want]], "signal_date": d,
                                    "_o": np.arange(min(len(ids), 4 * want), dtype=np.int64)}))
    if not picked:
        return pl.DataFrame(schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "draw": pl.Int64, **{c: pl.Float64 for c in names}})
    cand = _pool(store, market, paths.profile, snap, rule, pl.concat(picked))
    cand = (cand.sort("signal_date", "_o").with_columns(pl.int_range(pl.len()).over("signal_date").cast(pl.Int64).alias("draw"))
            .filter(pl.col("draw") < want).drop("_o"))
    lines = bought.with_columns(pl.int_range(pl.len()).over("signal_date").alias("_k"),
                                pl.len().over("signal_date").alias("_n"))
    cand = (cand.join(lines.select("signal_date", "_n").unique(), on="signal_date")
            .with_columns((pl.col("draw") % pl.col("_n")).alias("_k"))
            .join(lines.drop("_n"), on=["signal_date", "_k"]).drop("_k", "_n"))
    return cand.sort("signal_date", "draw")
