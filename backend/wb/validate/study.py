"""The event study and the condition check-ups (spec §9.1, §9.5), on each market's stored data. The split into
样本内 / 样本外 (`oos_start`) is fixed before a run and never moved by its results. A check-up drops each condition in
turn and scans each tuned parameter alone (at most SCAN_POINTS values, the current one among them), judging every
variant by the same outcomes; the single-condition test runs one formula as the event and compares it with random
entries over the same days. Results are suggestions: nothing here changes a rule."""
import json
import math
from collections.abc import Callable
from datetime import date

import numpy as np
import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l3.rule import Rule, load_rule
from wb.engine.l3.understand import MARKET
from wb.validate.attribution import attribute, candidates, feature_rule
from wb.validate.control import control_signals
from wb.validate.engine import Variant, market_signals
from wb.validate.outcomes import HOLD, outcomes
from wb.validate.plantrades import plan_rule, plan_trades
from wb.validate.research import halves_plan, report
from wb.validate.summary import FEW, summarize

SCAN_POINTS = 15
RANDOM = 5000  # random entries at most

Progress = Callable[[str], None]


def _say(progress: Progress | None, text: str) -> None:
    if progress:
        progress(text)


def _plan_names(rule: Rule) -> list[str]:
    p = rule.trade_plan
    return [f"stop{j}" for j in range(len(p.stop))] + [f"take{j}" for j in range(len(p.take))]


def _signals(paths_of, variants: list[Variant], markets: list[str], start: date, end: date,
             progress: Progress | None, extra: Callable[[str], Variant | None] | None = None,
             plan: bool = False) -> list[pl.DataFrame]:
    """Each variant's signals over the markets, with the market; `extra`: one more variant a market (its signals with
    their ranking values come last, the markets' own in one table); `plan`: with its trade plan's lines (stop0…,
    take0…), read on the signal bar — for a plan buying by cross, its crosses (read the evening before; design
    2026-10-11 §6) with their price, the top they crossed and the break line (edge, line), how they went (gap,
    closed_above, signal) and the structures."""
    names = _plan_names(variants[0].rule) if plan else []
    cross = variants[0].rule.trade_plan if plan and variants[0].rule.trade_plan.entry == "cross" else None
    keep = (["entry_price", "edge", "line", "gap", "closed_above", "signal"]
            + [s.id for s in variants[0].rule.stages if s.kind == "structure"] if cross else [])
    if plan:
        variants = [Variant(plan_rule(v.rule), v.skip) for v in variants]
    out, more = [[] for _ in variants], []
    for m in markets:
        def step(n, total, m=m):
            _say(progress, f"{MARKET[m]} 第 {n}/{total} 批股票")
        x = extra(m) if extra else None
        got = market_signals(paths_of(m), m, variants + ([x] if x else []), start, end, step, lines=x is not None or bool(names),
                             cross=cross)
        for i, sig in enumerate(got[:len(variants)]):
            out[i].append(sig.select("listing_id", "signal_date", *[pl.col(f"rank{j}").alias(k) for j, k in enumerate(names)],
                                     *[c for c in keep if c in sig.columns])
                          .with_columns(pl.lit(m).alias("market")))
        if x:
            more.append(got[-1].with_columns(pl.lit(m).alias("market")))
    return [pl.concat(parts) for parts in out] + ([pl.concat(more, how="diagonal_relaxed")] if extra else [])


def _outcomes(paths_of, signals: pl.DataFrame, markets: list[str]) -> pl.DataFrame:
    parts = [outcomes(paths_of(m), m, signals.filter(pl.col("market") == m)) for m in markets]
    return pl.concat(parts, how="vertical_relaxed")


def plan_study(paths_of: Callable[[str], Paths], rule: Rule, sig: pl.DataFrame, markets: list[str],
               progress: Progress | None = None, seed: int = 0, control: bool = True) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The signals (with their plan's lines and market) traded by the rule's plan, and (`control`) their random control
    traded the same way (design 2026-10-09 §5, §6.2), each with its market."""
    trades, ctrl = [], []
    for m in markets:
        part = sig.filter(pl.col("market") == m).drop("market")
        if part.is_empty():
            continue
        def step(n, total, m=m):
            _say(progress, f"{MARKET[m]} 按交易计划买卖：第 {n}/{total} 批股票")
        t = plan_trades(paths_of(m), m, rule, part, progress=step)
        trades.append(t.with_columns(pl.lit(m).alias("market")))
        if not control:
            continue
        _say(progress, f"{MARKET[m]} 随机对照")
        c = control_signals(paths_of(m), m, rule, t, seed=seed)
        if c.height:
            ctrl.append(plan_trades(paths_of(m), m, rule, c, relative=True).with_columns(pl.lit(m).alias("market")))
    return (pl.concat(trades, how="diagonal_relaxed") if trades else pl.DataFrame(),
            pl.concat(ctrl, how="diagonal_relaxed") if ctrl else pl.DataFrame())


def event_study(paths_of: Callable[[str], Paths], rule: Rule, markets: list[str], start: date, end: date,
                oos_start: date, progress: Progress | None = None) -> dict:
    """The rule's signals traded by its plan (交易计划: the report on top, design 2026-10-09 §6) and, as before, each
    signal's moves 1 to 60 days on, its groups and its worst cases."""
    sig = _signals(paths_of, [Variant(rule)], markets, start, end, progress, plan=True)[0]
    _say(progress, "计算每个信号之后的收益")
    rows = _outcomes(paths_of, sig, markets)
    summary = summarize(rows, oos_start)
    trades, ctrl = plan_study(paths_of, rule, sig, markets, progress)
    names = {}
    for m in markets:  # the worst cases come with their names: the page need not load every signal for twenty
        st = Store(paths_of(m), m)
        names.update(dict(st.read("instruments", st.latest()).select("listing_id", "name").iter_rows()))
    summary["worst"] = [{**w, "name": names.get(w["listing_id"])} for w in summary["worst"]]
    context = rows.select("market", "listing_id", "signal_date", "industry", "above200")
    summary["metric"], summary["judge"] = "plan", None
    summary["plan"] = report(trades, ctrl if ctrl.height else None, oos_start, rule.trade_plan, context=context) if trades.height else None
    if summary["plan"] is not None:
        worst = (trades.filter(pl.col("status").is_in(["done", "gone"]) & pl.col("ret").is_not_null()).sort("ret").head(20)
                 .select("market", "listing_id", "signal_date", "entry_date", "exit_date", "ret", "excess", "reason", "held"))
        summary["plan"]["worst"] = [{**w, "name": names.get(w["listing_id"])} for w in worst.to_dicts()]
        summary["plan"]["entries"] = entries(paths_of, rule, sig, summary["plan"], markets, start, end, oos_start, progress)
    return {"summary": summary, "rows": rows}


ENTRIES = {"cross": "盘中越线买", "close": "尾盘确认买", "open": "第二天开盘买"}
BRIEF = ("days", "signals", "bought", "finished", "expectancy", "ci", "vs_control", "vs_control_ci", "control_days", "win",
         "payoff", "pf", "held", "per_day", "few", "blocked", "plan")


def can_cross(rule: Rule) -> bool:
    """A 潜伏 stage whose next stage breaks a line up: a line to cross."""
    ids = [s.id for s in rule.stages]
    if rule.watch_stage not in ids or ids.index(rule.watch_stage) + 1 >= len(ids):
        return False
    nxt = rule.stages[ids.index(rule.watch_stage) + 1]
    return nxt.kind == "event" and (nxt.on or "").endswith(".break_up")


def entries(paths_of, rule: Rule, sig: pl.DataFrame, own: dict, markets: list[str], start: date, end: date, oos_start: date,
            progress: Progress | None = None) -> dict:
    """买法对比 (design 2026-10-11 §10): the rule bought each way it can be — at the line during the day (a 潜伏 rule), at
    the signal close, at the next open — with its own sales (stops, targets, exits, the same fills), each by the plan's
    report; the rule's own way is the report above. How many ways were compared is said: picking the best of them is
    a choice among that many (PBO comes with the comparisons' page, P5)."""
    plan = rule.trade_plan
    ways = [e for e in ENTRIES if e != "cross" or can_cross(rule)]
    got = {"cross" if plan.entry == "cross" else "bar": sig}   # the close and the open buy the same signals
    rows = []
    for e in ways:
        if e == plan.entry:
            rep = own
        else:
            r = rule.model_copy(update={"plan": plan.model_copy(update={"entry": e, "fill": plan.fills}), "judge": None})
            key = "cross" if e == "cross" else "bar"
            if key not in got:
                got[key] = _signals(paths_of, [Variant(r)], markets, start, end, progress, plan=True)[0]
            _say(progress, f"买法对比：{ENTRIES[e]}")
            t, c = plan_study(paths_of, r, got[key], markets, progress)
            rep = report(t, c if c.height else None, oos_start, r.trade_plan) if t.height else None
        rows.append({"entry": e, "label": ENTRIES[e], **({k: rep[k] for k in BRIEF} if rep else {"days": 0})})
    return {"compared": len(ways), "rows": rows}


# ---- the condition check-up ----------------------------------------------------------------------------------
def conditions(rule: Rule) -> list[tuple[str, str, str]]:
    """(stage, condition id, text) of every condition a check-up can drop: at, during, all, any (a stage's own
    event `when` is what the stage is, not a condition of it)."""
    out = []
    for s in rule.stages:
        out += [(s.id, c.id, c.expr) for c in [*s.at, *s.during]]
        out += [(s.id, s.id, getattr(s, k)) for k in ("all", "any") if getattr(s, k)]
    return out


def scan_points(lo, hi, step, current) -> list:
    """The values a parameter is scanned at: its range by its step, at most SCAN_POINTS evenly spread, with the
    current value among them."""
    whole = isinstance(lo, int) and isinstance(hi, int) and isinstance(step, int)
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    picks = range(n) if n <= SCAN_POINTS else sorted({round(i * (n - 1) / (SCAN_POINTS - 1)) for i in range(SCAN_POINTS)})
    vals = [lo + k * step if whole else round(lo + k * step, 10) for k in picks]
    if current not in vals:
        near = min(range(len(vals)), key=lambda i: abs(vals[i] - current))
        vals[near] = current
    return sorted(set(vals))


def _verdict(base: dict, var: dict) -> str:
    """有效: dropping it makes both halves' expectancy (by the plan, per signal day) worse by more than their standard
    error; 有害: better in both; 多余: neither moves by more than it; else 不一致 (design 2026-10-09 §8). Under FEW days
    on either side: no conclusion."""
    if min(base["effective_in"], base["effective_out"], var["effective_in"], var["effective_out"]) < FEW:
        return "样本太少"
    d, se = {}, {}
    for g in ("in", "out"):
        if base[f"mean_{g}"] is None or var[f"mean_{g}"] is None:
            return "样本太少"
        d[g] = var[f"mean_{g}"] - base[f"mean_{g}"]
        se[g] = math.sqrt((base[f"se_{g}"] or 0) ** 2 + (var[f"se_{g}"] or 0) ** 2)
    if all(d[g] < -se[g] for g in d):
        return "有效"
    if all(d[g] > se[g] for g in d):
        return "有害"
    if all(abs(d[g]) <= se[g] for g in d):
        return "多余"
    return "不一致"


def _delta(a, b):
    return None if a is None or b is None else a - b


def _plateau(points: list[dict], current) -> list | None:
    """The values around the current one whose 样本外 expectancy stays within its standard error of the current's."""
    i = next((k for k, p in enumerate(points) if p["value"] == current), None)
    if i is None or points[i]["mean_out"] is None:
        return None
    ref, band = points[i]["mean_out"], points[i]["se_out"] or 0.0
    ok = [p["mean_out"] is not None and p["effective_out"] >= FEW and abs(p["mean_out"] - ref) <= band for p in points]
    lo = hi = i
    while lo > 0 and ok[lo - 1]:
        lo -= 1
    while hi < len(points) - 1 and ok[hi + 1]:
        hi += 1
    return [points[lo]["value"], points[hi]["value"]]


def checkup(paths_of: Callable[[str], Paths], rule: Rule, markets: list[str], start: date, end: date,
            oos_start: date, progress: Progress | None = None) -> dict:
    conds = conditions(rule)
    scans = [(p.id, scan_points(p.range[0], p.range[1], p.step, p.value)) for p in rule.params
             if p.range and p.step and isinstance(p.value, (int, float)) and not isinstance(p.value, bool)]
    variants = [Variant(rule)] + [Variant(rule, frozenset({(sid, cid)})) for sid, cid, _ in conds]
    points = [(pid, v) for pid, vals in scans for v in vals if v != rule.param(pid)]
    variants += [Variant(rule.with_params({pid: v})) for pid, v in points]
    specs, ranked, skipped = candidates(rule), {}, []

    def feature_variant(m: str) -> Variant | None:   # 候选条件归因: the rule ranked by each feature, read on the signal bar
        st = Store(paths_of(m), m)
        r, ok, out = feature_rule(rule, m, available_columns(st, st.latest()), specs)
        ranked[m] = ok
        skipped.extend(out)
        return Variant(r) if ok else None
    # every variant judged by the rule's plan (design 2026-10-09 §8): its trades' expectancy per signal day
    sigs = _signals(paths_of, variants, markets, start, end, progress, feature_variant, plan=True)
    feats = sigs.pop()
    _say(progress, "按交易计划买卖每个信号")
    key = ["market", "listing_id", "signal_date"]
    every = pl.concat(sigs, how="diagonal_relaxed").unique(key, keep="first", maintain_order=True)
    trades = plan_study(paths_of, rule, every, markets, progress, control=False)[0]   # once for all variants
    stats = [halves_plan(trades.join(s.select(key), on=key, how="semi"), oos_start) for s in sigs]
    base = stats[0]
    ablation = []
    for (sid, cid, text), st in zip(conds, stats[1:1 + len(conds)], strict=True):
        ablation.append({"condition": f"{sid}.{cid}", "stage": sid, "text": text, "signals": st["signals"],
                         "d_mean_in": _delta(st["mean_in"], base["mean_in"]), "d_mean_out": _delta(st["mean_out"], base["mean_out"]),
                         "d_win_in": _delta(st["win_in"], base["win_in"]), "d_win_out": _delta(st["win_out"], base["win_out"]),
                         "verdict": _verdict(base, st)})
    by_point = dict(zip(points, stats[1 + len(conds):], strict=True))
    out_scans = []
    for pid, vals in scans:
        pts = [{"value": v, **(base if v == rule.param(pid) else by_point[(pid, v)]),
                "few": (base if v == rule.param(pid) else by_point[(pid, v)])["effective_out"] < FEW} for v in vals]
        out_scans.append({"param": pid, "current": rule.param(pid), "points": pts, "plateau": _plateau(pts, rule.param(pid))})
    names = []
    for m, ok in ranked.items():   # each market's ranking columns, by feature
        names += [f["id"] for f in ok if f["id"] not in names]
    parts = [feats.filter(pl.col("market") == m).select("market", "listing_id", "signal_date",
                                                        *[pl.col(f"rank{j}").alias(f["id"]) for j, f in enumerate(ok)])
             for m, ok in ranked.items() if ok]
    table = (trades.join(sigs[0].select(key), on=key, how="semi").filter(pl.col("status").is_in(["done", "gone"]))
             .select(*key, "ret", pl.lit(None, pl.Utf8).alias("blocked")))   # finished trades, by their plan's return
    if parts:
        table = table.join(pl.concat(parts, how="diagonal_relaxed").unique(key), on=key, how="left")
    sid = rule.signal[0]
    stage = sid if rule.stage(sid).kind == "event" else None
    attribution = attribute(table, [f for f in specs if f["id"] in names], oos_start, stage, "ret")
    attribution["skipped"] = skipped + attribution["skipped"]   # each market's: not computable there, or never known
    return {"base": base, "ablation": ablation, "scans": out_scans, "attribution": attribution,
            "compared": len(variants) - 1 + len({f["id"] for f in attribution["features"]}), "oos_start": oos_start,
            "metric": "plan", "judge": None, "plan": rule.trade_plan.model_dump()}


# ---- one condition on its own ---------------------------------------------------------------------------------
def _first_of_runs(sig: pl.DataFrame, paths_of, markets) -> pl.DataFrame:
    """A condition that holds day after day is one event: the first day of each run (by the market's trading days)."""
    parts = []
    for m in markets:
        st = Store(paths_of(m), m)
        cal = (st.read("calendar", st.latest()).filter(pl.col("is_open")).select(pl.col("date").alias("signal_date"))
               .sort("signal_date").with_row_index("k"))
        s = sig.filter(pl.col("market") == m).join(cal, on="signal_date").sort("listing_id", "signal_date")
        parts.append(s.filter((pl.col("k").diff().over("listing_id").fill_null(2) > 1)).drop("k"))
    return pl.concat(parts)


def _random(paths_of, markets: list[str], start: date, end: date, n: int, seed: int) -> pl.DataFrame:
    """Random entries over the same days: a listing and a day it traded, drawn uniformly."""
    rng = np.random.default_rng(seed)
    parts = []
    for m in markets:
        paths = paths_of(m)
        st = Store(paths, m)
        bars = (build_frame(st, m, paths.profile, set(), None, end=end, snap=st.latest())
                .filter(pl.col("date") >= start).select("listing_id", pl.col("date").alias("signal_date")))
        if bars.height:
            pick = rng.choice(bars.height, size=min(n, bars.height), replace=False)
            parts.append(bars[np.sort(pick)].with_columns(pl.lit(m).alias("market")))
    return pl.concat(parts) if parts else pl.DataFrame(schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "market": pl.Utf8})


def single(paths_of: Callable[[str], Paths], expr: str, markets: list[str], start: date, end: date, oos_start: date,
           progress: Progress | None = None, seed: int = 0) -> dict:
    rule = load_rule(json.dumps({"id": "single", "name": "单条件", "markets": markets,
                                 "stages": [{"id": "x", "kind": "event", "when": expr}], "signal_at": "x.at"}, ensure_ascii=False))
    sig = _first_of_runs(_signals(paths_of, [Variant(rule)], markets, start, end, progress)[0], paths_of, markets)
    _say(progress, "计算事件和随机入场之后的收益")
    rows = _outcomes(paths_of, sig, markets)
    rnd = _outcomes(paths_of, _random(paths_of, markets, start, end, min(RANDOM, max(rows.height, 1000)), seed), markets)
    ev, rs = summarize(rows, oos_start), summarize(rnd, oos_start)
    pick = ("signals", "effective", "win", "median", "split", "few", "gap")
    return {"expr": expr, "events": {k: ev[k] for k in pick}, "random": {k: rs[k] for k in pick}, "hold": HOLD}
