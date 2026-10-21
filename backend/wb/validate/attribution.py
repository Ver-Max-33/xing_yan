"""候选条件归因 (spec §9.5): on all of a rule's signals, a set of candidate features read on the signal day (known then),
cut at their in-sample quintiles; each half's 20-day excess by group, in samples as the event study forms them (a
market's signals of one day averaged). A filter is suggested only where both halves rise, or both fall, group after
group, every group with enough samples — five groups ordered by chance in both halves the same way is about 1 in
7,200 — and only if it betters both halves' win rate. Results only: nothing changes the rule until you adopt it."""
from datetime import date

import numpy as np
import polars as pl

from wb.engine.l1.compiler import CompileError
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import Rank, Rule, RuleError
from wb.validate.summary import FEW, _stats, halves

QUINTS = (0.2, 0.4, 0.6, 0.8)
MAX_SUGGESTIONS = 3   # spec §9.5: at most three at a time
# the candidates (spec §9.5: 成交额、市值、换手、相对强度、离均线距离、箱体属性 …), each a formula on the signal day;
# 行业 and 大盘状态 are in the event study's groups (a category has no order to suggest a threshold from)
FEATURES = [
    {"id": "amount20", "label": "成交额（20 日均）", "expr": "MA(AMOUNT, 20)"},
    {"id": "mv", "label": "总市值", "expr": "TOTAL_MV"},
    {"id": "turnover20", "label": "换手率（20 日均，%）", "expr": "MA(TURNOVER, 20)"},
    {"id": "dist20", "label": "离 20 日线", "expr": "MA_DIST(20)"},
    {"id": "dist60", "label": "离 60 日线", "expr": "MA_DIST(60)"},
    {"id": "rel20", "label": "20 日相对强弱", "expr": "REL_PERF(20)"},
    {"id": "rel60", "label": "60 日相对强弱", "expr": "REL_PERF(60)"},
    {"id": "amp40", "label": "两个月振幅", "expr": "AMPLITUDE(40)"},
    {"id": "volr", "label": "量比（对前 5 日）", "expr": "VOL_RATIO(5)"},
    {"id": "spread", "label": "四线离散", "expr": "MA_SPREAD([5, 10, 20, 60])"},
    {"id": "redgreen", "label": "红肥绿瘦（20 根里阳线的量占比）", "expr": "RED_GREEN_SHARE(20)"},
    # 位置 and 空间 (低位 / 中继 / 高位; 箱外空间 ≥ 约 10%), never tested as a filter
    # before (2026-10-11): where the close sits in its year's range, and the room up to the nearest left pressure box
    # (nothing above is +inf: the top fifth)
    {"id": "pos250", "label": "一年里的位置（0 最低，1 最高）", "expr": "(C - LLV(L, 250)) / (HHV(H, 250) - LLV(L, 250))"},
    {"id": "space", "label": "上方空间（到左侧压力）", "expr": "pressure.space"},
]


def candidates(rule: Rule) -> list[dict]:
    """The preset features, and the first box stage's width, its bars up to the signal (a box's own length is known
    only once it ended, after the break) and its candles' length — on its own and against the market's daily moves in
    the same bars (short, even candles mean controlled trading; 2016–2025, 105k first breaks of big boxes: the fifth with the
    longest candles against the market had a median 20-day excess ~3 points under the shortest's, in both halves)."""
    box = next((s.id for s in rule.stages if s.kind == "structure" and s.use == "BOX"), None)
    gold = next((s.id for s in rule.stages if s.kind == "structure" and s.use == "GOLD"), None)
    if not (box or gold):
        return FEATURES
    of = box or gold   # a 金品's bars are its small box's: the consolidation its signal breaks
    bar = f"MEAN(BAR_LEN, of={of})"   # (H − L) ÷ the close before, averaged over the box's bars
    shape = ([{"id": "box_width", "label": "箱宽", "expr": f"{box}.width"},
              {"id": "box_length", "label": "箱长（到信号前的根数）", "expr": f"COUNT(RAW_C > 0, of={box})"}] if box else [])
    return FEATURES + shape + [{"id": "box_bar", "label": "箱内 K 线平均长度（(最高 − 最低) ÷ 前一天收盘）", "expr": bar},
                               {"id": "box_bar_rel", "label": "箱内 K 线平均长度 ÷ 同期大盘平均日涨跌幅",
                                "expr": f"{bar} / MEAN(ABS(IDX_C / REF(IDX_C, 1) - 1), of={of})"}]


def feature_rule(rule: Rule, market: str, available: set[str], specs: list[dict]) -> tuple[Rule, list[dict], list[dict]]:
    """The rule ranked by every feature this market's data can compute — the matcher reads a ranking on the signal bar,
    as of then (spec §5.8) — with those features, and those left out with why."""
    ok, out = [], []
    for f in specs:
        try:
            Matcher.program(rule.model_copy(update={"ranking": [Rank(expr=f["expr"])]}), market, available)
            ok.append(f)
        except (RuleError, CompileError) as e:
            out.append({**f, "market": market, "why": str(e)})
    return rule.model_copy(update={"ranking": [Rank(expr=f["expr"]) for f in ok]}), ok, out


PLAN = "ret"   # the metric of a check-up by the trade plan: groups ordered by their expectancy (design 2026-10-09 §8)


def _order(groups: list[dict], half: str, by: str = "median") -> str | None:
    meds = [g[half][by] for g in groups]
    if any(m is None for m in meds):
        return None
    if all(a < b for a, b in zip(meds, meds[1:], strict=False)):
        return "up"
    if all(a > b for a, b in zip(meds, meds[1:], strict=False)):
        return "down"
    return None


def grouped(rows: pl.DataFrame, col: str, oos_start: date, metric: str | None = None) -> dict:
    """One numeric feature: its in-sample quintile cuts, each group's (lo, hi] with both halves' signals, samples,
    win rate and median; `direction` up / down when both halves are ordered the same way, with every group's samples
    at least FEW (else 样本太少: numbers only)."""
    known = rows.filter(pl.col(col).is_not_null())
    ins = known.filter(pl.col("signal_date") < oos_start)[col]
    if ins.len() <= len(QUINTS):
        return {"cuts": None, "groups": [], "direction": None, "verdict": "样本太少"}
    top = ins.filter(ins.is_finite()).max()   # +inf (pressure.space: no box above) is above every cut, which stay numbers
    cuts = [float(ins.clip(upper_bound=top).quantile(q, "linear")) for q in QUINTS]
    g = known.with_columns(pl.sum_horizontal([(pl.col(col) > c).cast(pl.Int8) for c in cuts]).alias("_g"))
    groups = []
    for k in range(len(cuts) + 1):
        part = g.filter(pl.col("_g") == k)
        groups.append({"group": k, "lo": cuts[k - 1] if k else None, "hi": cuts[k] if k < len(cuts) else None,
                       "in": _stats(part.filter(pl.col("signal_date") < oos_start), metric),
                       "out": _stats(part.filter(pl.col("signal_date") >= oos_start), metric)})
    if any(gr[h]["effective"] < FEW for gr in groups for h in ("in", "out")):
        return {"cuts": cuts, "groups": groups, "direction": None, "verdict": "样本太少"}
    by = "mean" if metric == PLAN else "median"
    d_in, d_out = _order(groups, "in", by), _order(groups, "out", by)
    direction = d_in if d_in is not None and d_in == d_out else None
    return {"cuts": cuts, "groups": groups, "direction": direction, "verdict": "单调" if direction else "不单调"}


def _num(v: float) -> str:
    """A threshold as a formula reads it: positional, six significant digits."""
    return np.format_float_positional(v, precision=6, unique=False, fractional=False, trim="-")


def suggestions(rows: pl.DataFrame, features: list[dict], oos_start: date, results: dict[str, dict],
                metric: str | None = None) -> list[dict]:
    """The filters the ordered features suggest — rising: drop the lowest fifth (`expr > q20`); falling: the highest
    (`expr <= q80`) — with the signals, samples, win rates and medians before and after on the same signals, best
    out-of-sample gain first, at most MAX_SUGGESTIONS."""
    base = halves(rows, oos_start, metric)
    out = []
    for f in features:
        r = results.get(f["id"])
        if not r or not r["direction"]:
            continue
        op, cut = (">", r["cuts"][0]) if r["direction"] == "up" else ("<=", r["cuts"][-1])
        keep = pl.col(f["id"]) > cut if op == ">" else pl.col(f["id"]) <= cut
        after = halves(rows.filter(keep), oos_start, metric)
        keys = ("mean_in", "mean_out") if metric == PLAN else ("win_in", "win_out")
        better = all(after[k] is not None and base[k] is not None and after[k] > base[k] for k in keys)
        if better:
            out.append({"feature": f["id"], "label": f["label"], "op": op, "threshold": cut,
                        "condition": f"{f['expr']} {op} {_num(cut)}", "before": base, "after": after})
    gain = "mean_out" if metric == PLAN else "win_out"
    return sorted(out, key=lambda s: s["after"][gain] - s["before"][gain], reverse=True)[:MAX_SUGGESTIONS]


def attribute(table: pl.DataFrame, specs: list[dict], oos_start: date, stage: str | None, metric: str | None = None) -> dict:
    """Every feature's groups in each market on its own (元 and 円, or two markets' own levels, cut at one set of
    quintiles would make the groups the markets), a feature a market's signals never know said as such; the filters
    suggested only for one market's check-up (one threshold for one market) and when the signal stage is an event (a
    filter is a condition on the signal bar: an `at` of that stage)."""
    markets = sorted(table["market"].unique().to_list())
    feats, skipped, res = [], [], {}
    for f in (f for f in specs if f["id"] in table.columns):
        for m in markets:
            part = table.filter(pl.col("market") == m)
            if part[f["id"]].drop_nulls().is_empty():
                skipped.append({**f, "market": m, "why": "这个市场的信号当天都算不出"})
                continue
            res[(f["id"], m)] = grouped(part, f["id"], oos_start, metric)
            feats.append({**f, "market": m, **res[(f["id"], m)]})
    one = len(markets) == 1
    found = {fid: r for (fid, _), r in res.items()} if one else {}
    return {"features": feats, "skipped": skipped, "stage": stage, "markets": markets,
            "suggestions": suggestions(table, [f for f in specs if f["id"] in found], oos_start, found, metric) if stage and one else []}
