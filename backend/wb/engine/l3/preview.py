"""Single-stock debugging and the sample gallery (spec §6.3).

A preview runs a rule on one listing over its whole history: every structure primitive with the
rule's settings (the chart draws them all, spec §8.2), the rule's formula columns, and the matcher
with its signals — kept, so clicking a bar asks it for that day's explanation. The gallery runs a
rule over a fixed sample of listings (default about 300 over the last two years) and lists the hits."""
import hashlib
import math
from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1 import ast as A
from wb.engine.l1.frame import available_columns, not_stocks
from wb.engine.l2.run import l2_bars
from wb.engine.l2.spec import compute
from wb.engine.l3.explain import render
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import Rule, RuleError
from wb.engine.l3.run import frame_for, market_turn, run_rule


@dataclass
class Preview:
    rule: Rule
    market: str
    listing_id: str
    snapshot_id: str
    tables: dict           # compute()'s output for the listing, with the rule's primitive settings
    traded: list[date]     # the bars the primitives counted
    matcher: Matcher
    signals: pl.DataFrame  # every signal over the listing's history


def _latest(paths: Paths, market: str):
    store = Store(paths, market)
    snap = store.latest()
    if snap is None:
        raise RuleError(f"{market} 还没有数据快照")
    return store, snap


def preview(paths: Paths, rule: Rule, market: str, listing_id: str) -> Preview:
    store, snap = _latest(paths, market)
    available = available_columns(store, snap)
    bars = l2_bars(store, market, paths.profile, [listing_id], snap=snap)
    tables = compute(bars, None, rule.primitive_params())
    calendar = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    with market_turn(Matcher.cross_sectional(rule, market, available)):
        frame, pool = frame_for(store, market, paths.profile, rule, available, [listing_id], snap)
        m = Matcher(rule, market, frame, tables, calendar, available, pool)
    first, last = (m.frame["date"].min(), m.frame["date"].max()) if m.frame.height else (date.min, date.min)
    return Preview(rule, market, listing_id, snap.id, tables, bars["date"].to_list(), m, m.signals(first, last))


def sample(paths: Paths, market: str, n: int = 300, seed: int = 0) -> list[str]:
    """A fixed sample of the stocks traded in the last year: the same seed always gives the same
    listings, so hit counts stay comparable while parameters change."""
    store, snap = _latest(paths, market)
    bars = store.scan("bars_1d", snap)
    last = bars.select(pl.col("date").max()).collect().item()
    traded = bars.filter(pl.col("date") > last - timedelta(days=365)).select("listing_id").unique()
    if (funds := not_stocks(store, market, snap)) is not None:
        traded = traded.join(funds, on="listing_id", how="anti")
    ids = traded.collect()["listing_id"].to_list()
    return sorted(sorted(ids, key=lambda lid: hashlib.sha1(f"{seed}:{lid}".encode()).hexdigest())[:n])


def gallery(paths: Paths, rule: Rule, market: str, listings: list[str], years: int = 2,
            workers: int | None = None) -> pl.DataFrame:
    """The rule's signals on `listings` over the last `years`: listing_id, signal_date, the bound
    structures, name. Nothing is recorded (a preview, spec §6.3)."""
    store, snap = _latest(paths, market)
    end = store.scan("bars_1d", snap).select(pl.col("date").max()).collect().item()
    start = end.replace(year=end.year - years) if not (end.month == 2 and end.day == 29) else end - timedelta(days=365 * years)
    run = run_rule(paths, rule, market, start=start, end=end, listings=listings, workers=workers, record=False)
    if run.rows.is_empty():
        return run.rows.with_columns(pl.lit(None, pl.UInt32).alias("days"))
    # a run of hits on neighbouring bars (a screen holding day after day) is one hit: its first day
    i = run.matcher.frame.select("listing_id", pl.col("date").alias("signal_date"), "i")
    rows = run.rows.join(i, on=["listing_id", "signal_date"], how="left").sort("listing_id", "signal_date")
    rows = rows.with_columns((pl.col("i").diff().over("listing_id").fill_null(2) > 1).cum_sum().alias("_run"))
    first = rows.group_by("_run", maintain_order=True).agg(pl.all().first(), pl.len().alias("days"))
    return first.drop("_run", "i").sort("signal_date", "listing_id")


_FLIP = {">": "<", ">=": "<=", "<": ">", "<=": ">=", "==": "==", "!=": "!="}


def _just_passing(op: str, value: float, integer: bool):
    """The threshold at which `value op threshold` just holds."""
    if integer:
        if op in (">=", ">"):
            t = math.floor(value)
            return t - 1 if op == ">" and t == value else t
        t = math.ceil(value)
        return t + 1 if op == "<" and t == value else t
    tiny = max(abs(value) * 1e-9, 1e-12)
    return value - tiny if op == ">" else value + tiny if op == "<" else value


def missed(paths: Paths, rule: Rule, market: str, listing_id: str, start: date, end: date, listings: list[str],
           years: int = 2) -> dict:
    """漏检排查 (spec §6.3): `listing_id` should have hit between start and end. If it did, the days;
    else the day it got furthest (deepest stage, then the closest miss), that miss, and — when the
    missing condition's threshold is a parameter — the value that would let that day pass and the
    sample's hit count before and after (e.g. 37 → 112)."""
    p = preview(paths, rule, market, listing_id)
    hits = p.signals.filter(pl.col("signal_date").is_between(start, end))
    if hits.height:
        return {"status": "hit", "days": hits["signal_date"].to_list()}
    ids = [s.id for s in rule.stages]
    days = p.matcher.frame.filter((pl.col("listing_id") == listing_id) & pl.col("date").is_between(start, end))["date"].to_list()
    best = None
    for d in days:
        det = p.matcher.detail(listing_id, d)
        if det["status"] is None:
            continue
        m = det["closest_miss"]
        gap = (abs(m["threshold"] - m["value"]) / abs(m["threshold"])
               if m and isinstance(m["value"], float) and isinstance(m["threshold"], float) and m["threshold"] else math.inf)
        key = (ids.index(det["deepest_stage"]) if det["deepest_stage"] else -1, -gap)
        if best is None or key > best[0]:
            best = (key, d, det)
    if best is None:
        return {"status": "no_bars", "days": [], "note": "这段时间这只股票没有 K 线，或者它不在规则的股票池里"}
    _, day, det = best
    out = {"status": "missed", "day": day, "deepest_stage": det["deepest_stage"], "miss": det["closest_miss"],
           "names": det.get("names"), "lines": render(det), "param": None, "relaxed": None, "hits_before": None, "hits_after": None, "note": None}
    m = det["closest_miss"]
    if not m:
        return out
    node = next(n for sid, cid, _, n, _ in p.matcher.conds if sid == m["stage"] and cid == m["id"])
    # `x op $p` (the parameter on the right) or `$p op x` (on the left: the condition read the other way)
    if isinstance(node, A.Binary) and isinstance(node.b, A.Param) and m["value"] is not None:
        param, op, x = node.b.name, m["op"], m["value"]
    elif isinstance(node, A.Binary) and isinstance(node.a, A.Param) and m["threshold"] is not None:
        param, op, x = node.a.name, _FLIP[m["op"]], m["threshold"]
    else:
        out["note"] = "这个条件的阈值不是参数（$名字），不能自动放宽；只有 at / during 里和参数比较的条件可以"
        return out
    out.update(param=param, relaxed=_just_passing(op, x, rule.whole(param)))
    try:
        looser = rule.with_params({param: out["relaxed"]})
    except RuleError as e:
        out["note"] = f"放宽到 {out['relaxed']} 超出了参数的范围：{e}"
        return out
    out["hits_before"] = gallery(paths, rule, market, listings, years).height
    out["hits_after"] = gallery(paths, looser, market, listings, years).height
    return out


def compare_versions(paths: Paths, a: Rule, b: Rule, market: str, listings: list[str], years: int = 2) -> dict:
    """版本对比 (spec §6.3): the hits `b` adds to and loses from `a` on the same sample."""
    ga, gb = (gallery(paths, r, market, listings, years) for r in (a, b))
    key = ["listing_id", "signal_date"]
    return {"added": gb.join(ga.select(key), on=key, how="anti").to_dicts(),
            "lost": ga.join(gb.select(key), on=key, how="anti").to_dicts(),
            "kept": ga.join(gb.select(key), on=key, how="semi").height}
