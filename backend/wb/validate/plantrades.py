"""交易计划 on a market's stored data (design 2026-10-09 §5): a rule's signals carry their plan's lines, read on the
signal bar as a ranking is (`plan_rule`); the bars carry the exchange's prices, the factor that adjusts them, the
day's limits and the plan's 卖出条件 — a chunk of listings at a time, as the signals are found — and every signal is
traded by the plan (`trade`), with its return net of costs and the benchmark over the same times (`returns`). A buy
on the open needs the stock to trade on the market's next day; a listing still trading on the data's last day keeps
what it holds (`open`), one that stopped before is sold at its last close (`gone`)."""
from collections.abc import Callable

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.run import evaluate
from wb.engine.l3.rule import Rank, Rule
from wb.markets import load_market
from wb.validate.engine import CHUNK
from wb.validate.portfolio import COSTS
from wb.validate.trades import returns, trade

BARS = {"raw_open", "raw_high", "raw_low", "raw_close", "up_limit", "down_limit", "ul", "ll"}


def plan_rule(rule: Rule) -> Rule:
    """The rule ranked by its plan's lines (its 失效线, then its 目标位), read on the signal bar as a ranking is."""
    p = rule.trade_plan
    return rule.model_copy(update={"ranking": [Rank(expr=x.line) for x in p.stop + p.take]})


def lines_of(signals: pl.DataFrame, rule: Rule) -> pl.DataFrame:
    """Signals as market_signals gives them for plan_rule(rule) — rank0… — with the lines named stop0…, take0…."""
    p = rule.trade_plan
    names = [f"stop{j}" for j in range(len(p.stop))] + [f"take{j}" for j in range(len(p.take))]
    return signals.rename({f"rank{j}": x for j, x in enumerate(names) if f"rank{j}" in signals.columns})


def benchmark(store: Store, market: str, profile: str, snap) -> pl.DataFrame:
    iid = load_market(market).benchmark[profile]
    return (store.scan("index_bars_1d", snap).filter(pl.col("index_id") == iid).select("date", "open", "high", "low", "close")
            .sort("date").collect())


def plan_trades(paths: Paths, market: str, rule: Rule, signals: pl.DataFrame, relative: bool = False,
                progress: Callable[[int, int], None] | None = None) -> pl.DataFrame:
    """Every signal (listing_id, signal_date and the plan's lines: rank0… as plan_rule ranked them, or stop0…, take0…
    — shares of the entry price with `relative`) traded by the rule's plan: trade()'s columns, the signals' own (the
    lines, a control's draw …), ret, bench_ret, excess, exit_date. One row per listing and day."""
    store = Store(paths, market)
    snap = store.latest()
    plan = rule.trade_plan
    sig = (signals if relative else lines_of(signals, rule)).unique(["listing_id", "signal_date"], keep="first", maintain_order=True)
    sig = sig.sort("signal_date", "listing_id").with_row_index("sid").with_columns(pl.col("sid").cast(pl.Int64))
    comp = Compiler(market, {p.id: p.value for p in rule.params}, available_columns(store, snap))
    for j, x in enumerate(plan.exit):
        comp.add(f"exit{j}", x.when, want="cond")
    prog = comp.program()
    cal = (store.read("calendar", snap).filter(pl.col("is_open") & (pl.col("date") <= snap.as_of))["date"].unique().sort())
    # the market's next trading day after each signal (none past the data)
    sig = (sig.sort("signal_date").join_asof(pl.DataFrame({"_nd": cal}).sort("_nd"), left_on="signal_date", right_on="_nd",
                                             strategy="forward", allow_exact_matches=False).sort("sid"))
    lids = sorted(sig["listing_id"].unique().to_list())
    chunks = [lids[i:i + CHUNK] for i in range(0, len(lids), CHUNK)]
    out = []
    for n, chunk in enumerate(chunks, 1):
        frame = build_frame(store, market, paths.profile, BARS | prog.columns, chunk, snap=snap)
        b = frame.select("listing_id", "date", pl.col("raw_open").alias("open"), pl.col("raw_high").alias("high"),
                         pl.col("raw_low").alias("low"), pl.col("raw_close").alias("close"),
                         # a row the data check leaves unknown (spec §4.6) keeps the factor of the day before
                         (pl.col("close") / pl.col("raw_close")).forward_fill().over("listing_id").alias("adj"),
                         *[(pl.col(c) if c in frame.columns else pl.lit(None, pl.Float64)).alias(a)
                           for c, a in (("up_limit", "up"), ("down_limit", "down"))],
                         *[(pl.col(c).fill_null(False) if c in frame.columns else pl.lit(False)).alias(c) for c in ("ul", "ll")])
        if plan.exit:
            b = b.join(evaluate(frame, prog, cal.to_list()), on=["listing_id", "date"], how="left")
        b = b.with_columns((pl.col("date").max().over("listing_id") >= snap.as_of).alias("live"))
        s = (sig.filter(pl.col("listing_id").is_in(chunk))
             .join(b.select("listing_id", pl.col("date").alias("signal_date"), pl.col("date").shift(-1).over("listing_id").alias("_nb")),
                   on=["listing_id", "signal_date"], how="left")
             # no next trading day in the data yet: not bought yet (`open`), never a halt
             .with_columns(((pl.col("_nb") == pl.col("_nd")) | pl.col("_nd").is_null()).fill_null(False).alias("next_ok")))
        if plan.entry == "cross" and "entry_price" not in s.columns:   # a cross's control: daily bars do not say when the
            # cross was — bought at its stock's average price of the day (design 2026-10-11 §10)
            avg = (pl.col("open") + pl.col("high") + pl.col("low") + pl.col("close")) / 4
            s = s.join(b.select("listing_id", pl.col("date").alias("signal_date"), avg.alias("entry_price")),
                       on=["listing_id", "signal_date"], how="left")
        t = trade(s, b, plan, t_plus_1=market == "CN", relative=relative)
        keep = [c for c in s.columns if c not in ("_nd", "_nb", "next_ok") and c not in t.columns]   # the lines, a draw …
        out.append(t.join(s.select("sid", *keep), on="sid", how="left"))
        if progress:
            progress(n, len(chunks))
    trades = pl.concat(out) if out else pl.DataFrame()
    if trades.is_empty():
        return trades
    return returns(trades, benchmark(store, market, paths.profile, snap), COSTS[market], plan.entry).sort("sid")
