"""按交易计划的研究 (design 2026-10-09 §6): what a rule's signals earn when traded by its plan.

Each signal day is one sample — the mean of that day's finished trades (net of costs) — because a day's signals move
with one market and a person buys one or two of them, not a hundred: the expectancy is the samples' mean, its 95%
interval from resampling days (2000 times, a fixed seed). The rule against its random control (same days, same way of
trading, same distances to the lines, stocks drawn at random): day by day, the rule's sample less the control's, on
the days both have one. Trades by their ends — win rate, average win and loss, payoff, profit factor, days held,
return per day held, the best and worst move, why each ended — count only finished trades; bought but still held at
the data's end (`open`) and never bought (a limit up, a halt) are counted apart. By year, by half (样本内 / 样本外,
fixed before the run), by the market's state and the industry on the signal day. Under FEW days: numbers, no
conclusion."""
import math
from datetime import date

import numpy as np
import polars as pl

from wb.engine.l3.rule import Plan
from wb.validate.summary import FEW

FINISHED = ("done", "gone")
BOUGHT = ("done", "gone", "open")
BOOT = 2000


def _key(df: pl.DataFrame) -> list[str]:
    return ["market", "signal_date"] if "market" in df.columns else ["signal_date"]


def day_samples(trades: pl.DataFrame, col: str = "ret") -> pl.DataFrame:
    """One sample a signal day: the mean of its finished trades' `col` (v) and how many there were (n)."""
    key = _key(trades)
    t = trades.filter(pl.col("status").is_in(FINISHED) & pl.col(col).is_not_null())
    return t.group_by(key).agg(pl.col(col).mean().alias("v"), pl.len().alias("n")).sort(key)


def bootstrap(v: np.ndarray, n: int = BOOT, seed: int = 0) -> tuple[float | None, float | None]:
    """The 95% interval of the mean by resampling the samples (days) with replacement."""
    v = np.asarray(v, dtype=float)
    if len(v) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    means = v[rng.integers(0, len(v), (n, len(v)))].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def _mean(s: pl.Series) -> float | None:
    return float(s.mean()) if s.len() else None


def trade_stats(trades: pl.DataFrame) -> dict:
    """Finished trades by their ends; bought / open / blocked counted."""
    done = trades.filter(pl.col("status").is_in(FINISHED) & pl.col("ret").is_not_null())
    r = done["ret"]
    wins, losses = r.filter(r > 0), r.filter(r <= 0)
    held = done["held"].fill_null(0)
    reasons = done["reason"].value_counts() if done.height else pl.DataFrame({"reason": [], "count": []})
    return {"signals": trades.height, "bought": int(trades["status"].is_in(BOUGHT).sum()), "finished": done.height,
            "open": int((trades["status"] == "open").sum()), "gone": int((trades["status"] == "gone").sum()),
            "blocked": {k: int((trades["status"] == k).sum()) for k in ("limit", "halt")},
            "no_room": int((trades["no_room"].fill_null(0) > 0).sum()) if "no_room" in trades.columns else 0,
            "win": float((r > 0).mean()) if done.height else None, "avg_win": _mean(wins), "avg_loss": _mean(losses),
            "payoff": (float(wins.mean() / -losses.mean()) if wins.len() and losses.len() and losses.mean() < 0 else None),
            "pf": (float(wins.sum() / -losses.sum()) if wins.len() and losses.len() and losses.sum() < 0 else None),
            "held": _mean(held), "per_day": float(r.sum() / held.sum()) if done.height and held.sum() > 0 else None,
            "mfe": float(done["mfe"].median()) if done.height and done["mfe"].drop_nulls().len() else None,
            "mae": float(done["mae"].median()) if done.height and done["mae"].drop_nulls().len() else None,
            "reasons": {k: c / done.height for k, c in reasons.iter_rows()} if done.height else {}}


def control_days(control: pl.DataFrame | None, per_day: int) -> pl.DataFrame:
    """Each day's control: the first `per_day` bought draws (an unbuyable draw gives way to the next), as day samples."""
    if control is None or control.is_empty():
        return pl.DataFrame(schema={"signal_date": pl.Date, "v": pl.Float64, "n": pl.UInt32})
    key = _key(control)
    first = (control.filter(pl.col("status").is_in(BOUGHT)).sort([*key, "draw"])
             .with_columns(pl.int_range(pl.len()).over(key).alias("_k")).filter(pl.col("_k") < per_day))
    return day_samples(first)


def _part(trades: pl.DataFrame, ctrl: pl.DataFrame) -> dict:
    s = day_samples(trades)
    v = s["v"].to_numpy()
    lo, hi = bootstrap(v)
    key = _key(trades)
    if ctrl.height:   # no control at all (nothing was bought, so nothing was drawn): no comparison
        both = s.join(ctrl.select(*key, pl.col("v").alias("_c")), on=key, how="inner")
        d = (both["v"] - both["_c"]).to_numpy()
    else:
        d = np.array([], dtype=float)
    dlo, dhi = bootstrap(d, seed=1)
    x = day_samples(trades, "excess") if "excess" in trades.columns else None
    stats = trade_stats(trades)
    return {"days": s.height, "expectancy": float(v.mean()) if len(v) else None, "ci": [lo, hi],
            "se": float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else None,
            "excess": float(x["v"].mean()) if x is not None and x.height else None,
            "vs_control": float(d.mean()) if len(d) else None, "vs_control_ci": [dlo, dhi], "control_days": len(d),
            "no_control_days": s.height - len(d), "few": s.height < FEW, **stats}


def _groups(trades: pl.DataFrame, ctrl: pl.DataFrame, by: pl.Expr) -> list[dict]:
    out = []
    t = trades.with_columns(by.alias("_g")).filter(pl.col("_g").is_not_null())
    for (g,), part in t.group_by("_g"):
        p = _part(part.drop("_g"), ctrl)
        out.append({"key": g, **{k: p[k] for k in ("days", "expectancy", "ci", "vs_control", "win", "pf", "finished", "few")}})
    return sorted(out, key=lambda r: (-r["finished"], str(r["key"])))


def labels(plan: Plan) -> dict[str, str]:
    """How each end reads (the plan's own labels)."""
    out = {f"stop{j}": x.label or "失效线" for j, x in enumerate(plan.stop)}
    out |= {f"take{j}": x.label or "目标位" for j, x in enumerate(plan.take)}
    out |= {f"exit{j}": x.label or x.when for j, x in enumerate(plan.exit)}
    return out | {"stall": "不创新高", "days": "到期", "gone": "停止交易（退市、长期停牌）"}


def report(trades: pl.DataFrame, control: pl.DataFrame | None, oos_start: date, plan: Plan, per_day: int = 20,
           context: pl.DataFrame | None = None) -> dict:
    """The plan's research report. `trades`: the rule's signals traded (plan_trades); `control`: its control's (with
    `draw`); `context`: listing_id, signal_date (and market), industry, above200 for the groups."""
    ctrl = control_days(control, per_day)
    whole = _part(trades, ctrl)
    years = []
    for (y,), part in trades.with_columns(pl.col("signal_date").dt.year().alias("_y")).group_by("_y"):
        p = _part(part.drop("_y"), ctrl)
        years.append({"year": int(y), **{k: p[k] for k in ("days", "finished", "expectancy", "win", "pf", "vs_control")}})
    split = {"oos_start": oos_start.isoformat(),
             "in": _part(trades.filter(pl.col("signal_date") < oos_start), ctrl),
             "out": _part(trades.filter(pl.col("signal_date") >= oos_start), ctrl)}
    groups = {}
    if context is not None and context.height:
        key = [c for c in ("market", "listing_id", "signal_date") if c in context.columns and c in trades.columns]
        t = trades.join(context.unique(key), on=key, how="left")
        if "above200" in t.columns:
            groups["state"] = _groups(t, ctrl, pl.when(pl.col("above200")).then(pl.lit("大盘在 200 日线上"))
                                      .when(pl.col("above200").not_()).then(pl.lit("大盘在 200 日线下")))
        if "industry" in t.columns:
            groups["industry"] = _groups(t, ctrl, pl.col("industry"))
    return {**whole, "control_expectancy": float(ctrl["v"].mean()) if ctrl.height else None,
            "years": sorted(years, key=lambda r: r["year"]), "split": split, "groups": groups, "labels": labels(plan),
            "plan": plan.model_dump(), "per_day_control": per_day, "cross": _crosses(trades, ctrl) if plan.entry == "cross" else None}


def _crosses(trades: pl.DataFrame, ctrl: pl.DataFrame) -> dict | None:
    """How a cross plan's crosses went (design 2026-10-11 §10, 一个人的视角): crosses a day, the share that opened at or
    over the top, closed at or over the break line, and were the rule's signal that close — and by how the day closed:
    the signal; over the break line but not the signal (its breakout volume, say); not over it, the 假突破 (§6
    「盘中越线不等于收盘还在线上」)."""
    if trades.is_empty() or "gap" not in trades.columns:
        return None
    share = lambda e: float(trades.select(e.cast(pl.Float64).mean()).item())   # noqa: E731
    kind = (pl.when(pl.col("signal")).then(pl.lit("收盘成了规则信号"))
            .when(pl.col("closed_above")).then(pl.lit("收盘站上突破线、没成信号")).otherwise(pl.lit("收盘没站上（假突破）")))
    return {"crosses": trades.height, "per_day": float(trades.group_by("signal_date").len()["len"].mean()),
            "at_open": share(pl.col("gap") >= 0), "closed_above": share(pl.col("closed_above")), "signal": share(pl.col("signal")),
            "kinds": _groups(trades, ctrl, kind)}


def halves_plan(trades: pl.DataFrame, oos_start: date) -> dict:
    """The signals and each half's days (effective samples), expectancy, its standard error and win rate (条件体检)."""
    out = {"signals": trades.height}
    for h, part in (("in", trades.filter(pl.col("signal_date") < oos_start)), ("out", trades.filter(pl.col("signal_date") >= oos_start))):
        s = day_samples(part)
        v = s["v"].to_numpy()
        done = part.filter(pl.col("status").is_in(FINISHED) & pl.col("ret").is_not_null())
        out |= {f"effective_{h}": s.height, f"mean_{h}": float(v.mean()) if len(v) else None,
                f"se_{h}": float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else None,
                f"win_{h}": float((done["ret"] > 0).mean()) if done.height else None}
    return out
