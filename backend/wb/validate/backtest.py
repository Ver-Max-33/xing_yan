"""组合回测 over the markets (spec §9.2): each market is a book of its own — its currency, its trading rules, its costs —
run over the market's trading days in the range on the rule's signals (with their box lines and the rule's ranking),
reported against the market's benchmark with the drawdown, the trades, what could not be done, and the 样本内 /
样本外 split fixed before the run. Annualised figures count 252 trading days a year."""
import itertools
import math
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date

import numpy as np
import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l3.calibrate import axes as grid_axes
from wb.engine.l3.rule import Rank, Rule, RuleError
from wb.engine.l3.understand import MARKET
from wb.markets import load_market
from wb.validate.engine import Variant, market_signals
from wb.validate.portfolio import CASH, COSTS, RATES, Settings, prepare, simulate

YEAR = 252
HEAT = 9            # values a heatmap axis takes at most: evenly over the §6.5 grid, the current value among them
STRESS = (1, 2, 3)  # the costs raised: × 1, × 2, × 3
BARS = ("listing_id", "date", "open", "high", "low", "close", "adj_factor", "share_factor", "up_limit", "down_limit", "ul", "ll")

Progress = Callable[[str], None]


def book_settings(market: str, body: dict | None = None) -> Settings:
    """A market's book: the defaults (its cash, its costs, its lot; T+1 on CN) with what the run asked for."""
    body = body or {}
    base = COSTS[market]
    over = {k: float(v) for k, v in (body.get("costs") or {}).items() if k in RATES and v is not None}
    costs = replace(base, **over, history=tuple(h for h in base.history if h[1] not in over))   # a rate set: every day
    return Settings(cash=float(body.get("cash") or CASH[market]), costs=costs, max_positions=int(body.get("max_positions") or 10),
                    hold=body.get("hold", 20), box_stop=bool(body.get("box_stop", True)), trail=body.get("trail"),
                    target=body.get("target"), lot=load_market(market).lot_size, t_plus_1=market == "CN",
                    stall=body.get("stall"), weak_positions=body.get("weak_positions"), own=bool(body.get("own", True)))


def _days(store: Store, snap, start: date, end: date) -> list[date]:
    cal = store.read("calendar", snap).filter(pl.col("is_open") & pl.col("date").is_between(start, min(end, snap.as_of)))
    return cal["date"].unique().sort().to_list()


def _bars(store: Store, snap, lids: list[str], start: date) -> pl.DataFrame:
    """The exchange's prices of these listings from `start`; a day marked suspended has no prices."""
    lf = store.scan("bars_1d", snap).filter(pl.col("listing_id").is_in(lids) & (pl.col("date") >= start))
    have = set(lf.collect_schema().names())
    cols = [pl.col(c) if c in have else pl.lit(None).alias(c) for c in BARS]
    df = lf.select(*cols, *([pl.col("suspended")] if "suspended" in have else [])).collect()
    if "suspended" in df.columns:
        off = pl.col("suspended").fill_null(False)
        df = df.with_columns([pl.when(off).then(None).otherwise(pl.col(c)).alias(c) for c in ("open", "high", "low", "close")]).drop("suspended")
    df = df.cast({"open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "adj_factor": pl.Float64,
                  "share_factor": pl.Float64, "up_limit": pl.Float64, "down_limit": pl.Float64, "ul": pl.Boolean, "ll": pl.Boolean})
    if store.market == "CN":   # the cash a holder is paid: the 分红 records (JP's factors carry splits only, no dividends)
        paid = (store.scan("corp_actions", snap).filter(pl.col("listing_id").is_in(lids) & (pl.col("cash_div") > 0))
                .group_by("listing_id", pl.col("ex_date").alias("date")).agg(pl.col("cash_div").sum()).collect())
        df = df.join(paid, on=["listing_id", "date"], how="left")
    return df


def _own(rule: Rule, s: Settings) -> bool:
    return bool(s.own and (rule.plan is not None or rule.judge is not None))


def _owned(rule: Rule, s: Settings) -> Rule:
    """The rule ranked by its trade plan's 失效线 and first 目标位 after its own ranking (`own`, spec §9.1): its signals
    carry them."""
    if not _own(rule, s):
        return rule
    p = rule.trade_plan
    return rule.model_copy(update={"ranking": [*rule.ranking, *[Rank(expr=x.line) for x in p.stop + p.take[:1]]]})


def _own_lines(sig: pl.DataFrame, rule: Rule, s: Settings) -> pl.DataFrame:
    """Its own 失效线 in place of the box line — the highest of the plan's lines a signal has (the 假底 when there is one, else the box bottom);
    none without one — and its first 目标位 beside it (`own`)."""
    if not _own(rule, s):
        return sig
    p, k = rule.trade_plan, len(rule.ranking)
    stops = [pl.col(f"rank{k + j}") for j in range(len(p.stop))]
    none = pl.lit(None, pl.Float64)
    return sig.with_columns((pl.max_horizontal(stops) if stops else none).alias("stop"),
                            (pl.col(f"rank{k + len(p.stop)}") if p.take else none).alias("target_px"))


def _own_settings(rule: Rule, s: Settings) -> Settings:
    """`own`: the plan's days are the holding period; a rule without a plan or a judgement keeps the book's exits."""
    if not s.own:
        return s
    return replace(s, hold=rule.trade_plan.days) if _own(rule, s) else replace(s, own=False)


def _own_note(rule: Rule, s: Settings) -> list[str]:
    """The exits `own` used with this rule, for its report, and what of its plan the book cannot do yet (the 卖出条件
    and partial sales come with the account's redesign)."""
    if not _own(rule, s):
        return []
    p = rule.trade_plan
    stops = "、".join(x.line for x in p.stop)
    said = [p.stop and f"收盘跌破失效线（{stops}{'，取最高的那条' if len(p.stop) > 1 else ''}）次日开盘卖",
            p.take and f"盘中碰到目标位 {p.take[0].line} 就卖", f"最多持有 {p.days} 个交易日"]
    missing = ["越线买（按信号第二天开盘买）" if p.entry == "cross" else "尾盘买（按信号第二天开盘买）" if p.entry == "close" else "",
               f"卖出条件（{'、'.join(x.when for x in p.exit)}）" if p.exit else "",
               "分批卖" if any(x.share < 1 for x in p.stop + p.take + p.exit) else "",
               "不创新高就卖" if p.stall else ""]
    note = "按规则自己的交易计划离场：" + "，".join(x for x in said if x)
    if any(missing):
        note += f"；组合回测还没有{'、'.join(x for x in missing if x)}：账户回测改版时加上"
    return [note]


def _ordered(sig: pl.DataFrame, rule: Rule) -> pl.DataFrame:
    """Each day's signals in the rule's order (its ranking, nulls last, then the code): `rank` 0, 1, …"""
    keys = [pl.col(f"rank{j}") * (-1 if r.dir == "desc" else 1) for j, r in enumerate(rule.ranking) if f"rank{j}" in sig.columns]
    return (sig.sort(["signal_date", *keys, "listing_id"], nulls_last=True)
            .with_columns(pl.int_range(pl.len()).over("signal_date").alias("rank")))


def _part(dates: list[date], navs: np.ndarray, base: float) -> dict:
    """Return, annualised return, drawdown, volatility and Sharpe of a stretch of NAV that started from `base`."""
    if len(navs) == 0:
        return {"total": None, "cagr": None, "max_drawdown": None, "vol": None, "sharpe": None, "days": 0}
    v = np.concatenate([[base], navs])
    r = v[1:] / v[:-1] - 1
    total = float(v[-1] / base - 1)
    years = len(navs) / YEAR
    peak = np.maximum.accumulate(v)
    dd = v / peak - 1
    k = int(dd.argmin())
    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    return {"total": total, "cagr": (1 + total) ** (1 / years) - 1 if years >= 0.5 and total > -1 else None,
            "max_drawdown": float(dd.min()), "drawdown_end": dates[k - 1].isoformat() if k > 0 else None,
            "vol": sd * math.sqrt(YEAR) if sd else None, "sharpe": float(r.mean()) / sd * math.sqrt(YEAR) if sd else None,
            "days": len(navs)}


def _trades(t: pl.DataFrame) -> dict:
    done = t.filter(pl.col("exit_date").is_not_null())
    return {"trades": done.height, "open": t.height - done.height,
            "win": float((done["pnl"] > 0).mean()) if done.height else None,
            "avg_return": float(done["ret"].mean()) if done.height else None,
            "avg_days": float(done["days"].mean()) if done.height else None,
            "reasons": dict(done.group_by("reason").len().iter_rows()) if done.height else {}}


def book_stats(nav: pl.DataFrame, trades: pl.DataFrame, ledger: pl.DataFrame, cash: float, bench: pl.DataFrame,
               oos_start: date) -> dict:
    dates, navs = nav["date"].to_list(), nav["nav"].to_numpy()
    whole = _part(dates, navs, cash)
    cut = sum(1 for d in dates if d < oos_start)
    split = {"oos_start": oos_start.isoformat(), "in": {**_part(dates[:cut], navs[:cut], cash),
                                                       **_trades(trades.filter(pl.col("entry_date") < oos_start))},
             "out": {**_part(dates[cut:], navs[cut:], navs[cut - 1] if cut else cash),
                     **_trades(trades.filter(pl.col("entry_date") >= oos_start))}}
    paid = {k: float(-ledger.filter(pl.col("kind") == k)["amount"].sum()) for k in ("fee", "slippage")}
    b = nav.select("date").join(bench, on="date", how="left")["close"].fill_null(strategy="forward").fill_null(strategy="backward")
    bt = float(b[-1] / b[0] - 1) if b.len() and b[0] else None
    exposure = (nav["holdings"] / nav["nav"]).mean() if nav.height else None
    return {"stats": {**whole, **_trades(trades), "exposure": exposure, "fees": paid["fee"], "slippage": paid["slippage"],
                      "dividends": float(ledger.filter(pl.col("kind") == "dividend")["amount"].sum()),
                      "end_nav": float(navs[-1]) if len(navs) else cash},
            "split": split,
            "benchmark": {"total": bt, "excess": whole["total"] - bt if bt is not None and whole["total"] is not None else None},
            "curve": [[d.isoformat(), float(v) / cash, float(x) / float(b[0]) if b[0] else None]
                      for d, v, x in zip(dates, navs, b.to_list(), strict=True)]}


def _market(paths: Paths, market: str, start: date, end: date, lids: list[str]):
    """A market's trading days in the range, the bars of these listings (prepared) and its benchmark's closes."""
    store = Store(paths, market)
    snap = store.latest()
    days = _days(store, snap, start, end)
    if not days:
        raise RuleError(f"{market} 在 {start} 到 {end} 没有交易日")
    books = prepare(_bars(store, snap, lids, start)) if lids else {}
    iid = load_market(market).benchmark[paths.profile]
    bench = (store.scan("index_bars_1d", snap).filter((pl.col("index_id") == iid) & pl.col("date").is_between(days[0], days[-1]))
             .select("date", "close").collect())
    return days, books, bench, iid


def _weak(closes: pl.DataFrame, days: list[date]) -> set[date]:
    """The days whose open follows a benchmark close below its 200-day line (随大势); none before 200 closes."""
    flag = closes.sort("date").select("date", (pl.col("close") < pl.col("close").rolling_mean(200)).alias("weak"))
    on = pl.DataFrame({"date": days}, schema={"date": pl.Date}).join_asof(flag, on="date", allow_exact_matches=False)
    return set(on.filter(pl.col("weak").fill_null(False))["date"].to_list())


def _weak_days(paths: Paths, market: str, iid: str, days: list[date], s: Settings) -> set[date]:
    """`_weak` over the benchmark's whole history, when the book asks for fewer places in a weak market."""
    if s.weak_positions is None or not days:
        return set()
    store = Store(paths, market)
    closes = (store.scan("index_bars_1d", store.latest()).filter((pl.col("index_id") == iid) & (pl.col("date") <= days[-1]))
              .select("date", "close").collect())
    return _weak(closes, days)


def _run(sig: pl.DataFrame, books: dict, days: list[date], s: Settings, weak: set[date] | frozenset = frozenset()) -> dict:
    lines = [c for c in ("stop", "target_px") if c in sig.columns]   # the box line, or the rule's own 失效线 and 目标位
    return simulate(sig.select("listing_id", "signal_date", *lines, "rank"), books, days, s, weak)


def _notes(market: str, sig: pl.DataFrame, s: Settings) -> list[str]:
    """What a report on this book must say: an exit that does not apply, and what the market's data cannot book."""
    out = []
    if s.box_stop and not s.own and sig["stop"].null_count() == sig.height:
        out.append("规则里没有箱体阶段：跌破箱体下沿止损不适用")
    if market == "JP":
        out += ["日股只有整天的涨跌停标记（UL / LL）：开盘在最高价的 UL 日买不进、在最低价的 LL 日卖不出，是近似",
                "日股的复权因子只有拆股：分红没有记，收益只算价格"]
    return out


def run_book(paths: Paths, market: str, rule: Rule, sig: pl.DataFrame, start: date, end: date, oos_start: date,
             s: Settings) -> dict:
    """One market's book on its signals (already with their lines and order): the simulation and its report."""
    days, books, bench, iid = _market(paths, market, start, end, sig["listing_id"].unique().to_list())
    notes = _notes(market, sig, s)
    out = _run(sig, books, days, s, _weak_days(paths, market, iid, days, s))
    report = book_stats(out["nav"], out["trades"], out["ledger"], s.cash, bench, oos_start)
    settings = asdict(s)
    return {**out, "report": {**report, "counts": out["counts"], "notes": notes, "benchmark_id": iid, "settings": settings,
                              "signals": sig.height}}


def backtest(paths_of: Callable[[str], Paths], rule: Rule, markets: list[str], start: date, end: date, oos_start: date,
             settings: dict[str, Settings], progress: Progress | None = None) -> dict:
    """Each market's book, with the trades, ledger and NAV of all of them (a `market` column; each in its currency)."""
    books, parts = {}, {"nav": [], "trades": [], "ledger": []}
    for m in markets:
        paths = paths_of(m)

        def step(n, total, m=m):
            if progress:
                progress(f"{MARKET[m]} 第 {n}/{total} 批股票的信号")
        s = _own_settings(rule, settings[m])
        sig = market_signals(paths, m, [Variant(_owned(rule, s))], start, end, step, lines=True)[0]
        sig = _ordered(_own_lines(sig, rule, s), rule)
        if progress:
            progress(f"{MARKET[m]} 逐日撮合")
        out = run_book(paths, m, rule, sig, start, end, oos_start, s)
        # the settings as asked (what the form compares with); what the rule's own judgement used is said
        books[m] = {**out["report"], "settings": asdict(settings[m]), "notes": [*_own_note(rule, settings[m]), *out["report"]["notes"]]}
        names = dict(Store(paths, m).read("instruments").select("listing_id", "name").iter_rows())
        for k in parts:
            df = out[k].with_columns(pl.lit(m).alias("market"))
            if "listing_id" in df.columns:
                df = df.with_columns(pl.col("listing_id").replace_strict(names, default=None, return_dtype=pl.Utf8).alias("name"))
            parts[k].append(df)
    return {"books": books, **{k: pl.concat(v, how="diagonal_relaxed") for k, v in parts.items()}}


# ---- 稳健性检验 (spec §9.3) ---------------------------------------------------------------------------------------
def _thin(values: list, current) -> list:
    """At most HEAT of a grid axis's values, evenly spread, with the current one among them."""
    picks = list(values) if len(values) <= HEAT else [values[round(i * (len(values) - 1) / (HEAT - 1))] for i in range(HEAT)]
    if current not in picks and not isinstance(current, bool):
        near = min(range(len(picks)), key=lambda i: abs(picks[i] - current))
        picks[near] = current
    return sorted(set(picks))


def _brief(report: dict) -> dict:
    s, sp = report["stats"], report["split"]
    return {"total": s["total"], "cagr": s["cagr"], "max_drawdown": s["max_drawdown"], "sharpe": s["sharpe"], "trades": s["trades"],
            "in_total": sp["in"]["total"], "out_total": sp["out"]["total"], "out_sharpe": sp["out"]["sharpe"],
            "out_trades": sp["out"]["trades"]}


def robustness(paths_of: Callable[[str], Paths], rule: Rule, markets: list[str], start: date, end: date, oos_start: date,
               settings: dict[str, Settings], axes: list[str] | None = None, progress: Progress | None = None) -> dict:
    """参数敏感性: the books over two of the §6.5 grid's parameters (`axes`, else the first two tuned; a third stays at
    its value), each cell its own rule; 成本上调: the rule's own book with the costs × 1, × 2, × 3. Results only."""
    notes, pick, vals, points = [], [], {}, []
    if any(p.tune for p in rule.params):
        grid = grid_axes(rule)
        pick = [a for a in (axes or list(grid)) if a in grid][:2]
        vals = {p: _thin(grid[p], rule.param(p)) for p in pick}
        points = list(itertools.product(*vals.values()))
    else:
        notes.append("规则没有标 tune: true 的参数：只做成本上调压力测试")
    current = tuple(rule.param(p) for p in pick)
    others = [pt for pt in points if pt != current]
    variants = [Variant(rule)] + [Variant(rule.with_params(dict(zip(pick, pt, strict=True)))) for pt in others]
    keys = [current] + others
    cells = {pt: {"at": list(pt)} for pt in points}
    costs = [{"k": k} for k in STRESS]
    for m in markets:
        def step(n, total, m=m):
            if progress:
                progress(f"{MARKET[m]} 第 {n}/{total} 批股票，{len(variants)} 组参数")
        s = _own_settings(rule, settings[m])
        mine = [Variant(_owned(v.rule, s), v.skip) for v in variants]
        sigs = [_ordered(_own_lines(sig, rule, s), rule) for sig in market_signals(paths_of(m), m, mine, start, end, step, lines=True)]
        lids = pl.concat([sig.select("listing_id") for sig in sigs]).unique()["listing_id"].to_list()
        days, books, bench, iid = _market(paths_of(m), m, start, end, lids)
        notes += [n for n in [*_own_note(rule, settings[m]), *_notes(m, sigs[0], s)] if n not in notes]
        weak = _weak_days(paths_of(m), m, iid, days, s)
        for n, (key, sig) in enumerate(zip(keys, sigs, strict=True), 1):
            if progress:
                progress(f"{MARKET[m]} 逐日撮合：第 {n}/{len(sigs)} 组参数")
            if key in cells:
                out = _run(sig, books, days, s, weak)
                cells[key][m] = _brief(book_stats(out["nav"], out["trades"], out["ledger"], s.cash, bench, oos_start))
        for c in costs:
            out = _run(sigs[0], books, days, s.with_costs(c["k"]), weak)
            c[m] = _brief(book_stats(out["nav"], out["trades"], out["ledger"], s.cash, bench, oos_start))
    return {"axes": [{"param": p, "values": vals[p], "current": rule.param(p)} for p in pick], "cells": [cells[pt] for pt in points],
            "costs": costs, "compared": len(points), "notes": notes, "settings": {m: asdict(settings[m]) for m in markets}}
