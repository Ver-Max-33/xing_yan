"""The event study's numbers (spec §9.1, §9.5). Signals of one market on one day are one effective sample — their
mean — so a day that set off a hundred signals counts once; the win rate is the share of samples whose HOLD-day excess
is positive, the median and quartiles are of the samples. Signals that could not be bought are counted and left out;
a window past the data or a listing that stopped trading (`gone`, counted apart) has no value at that horizon. The
split into 样本内 / 样本外 is a date fixed before the run; under FEW samples there are numbers but no conclusion."""
import math
from datetime import date

import polars as pl

from wb.validate.outcomes import HOLD, HORIZONS

FEW = 30
STEP = 0.02     # the histogram's bins
WORST = 20


def _samples(rows: pl.DataFrame, col: str) -> pl.DataFrame:
    return (rows.filter(pl.col("blocked").is_null() & pl.col(col).is_not_null())
            .group_by("market", "signal_date").agg(pl.col(col).mean().alias("v"), pl.len().alias("n")))


def _stats(rows: pl.DataFrame, col: str | None = None) -> dict:
    """`col`: the excess a sample is judged by — the rule's own judgement (xj) when it has one, else the HOLD-day excess."""
    v = _samples(rows, col or f"x{HOLD}")["v"]
    # the mean as well: a rule that cuts its losers at its line and lets the rest run can win less than half and still
    # gain on average — the median alone would hide that
    return {"signals": rows.height, "effective": v.len(), "win": float((v > 0).mean()) if v.len() else None,
            "median": v.median() if v.len() else None, "mean": v.mean() if v.len() else None}


def _quantiles(v: pl.Series) -> dict:
    if not v.len():
        return {"median": None, "q25": None, "q75": None, "n": 0}
    return {"median": v.median(), "q25": v.quantile(0.25, "linear"), "q75": v.quantile(0.75, "linear"), "n": v.len()}


def _hist(v: pl.Series) -> dict:
    if not v.len():
        return {"step": STEP, "lo": 0.0, "counts": []}
    lo = math.floor(max(v.min(), v.quantile(0.01, "linear"), -0.4) / STEP) * STEP
    hi = math.ceil(min(v.max(), v.quantile(0.99, "linear"), 0.4) / STEP + 1e-9) * STEP
    n = max(1, round((hi - lo) / STEP))
    k = ((v.clip(lo, hi - 1e-12) - lo) / STEP).floor().cast(pl.Int64).clip(0, n - 1)
    return {"step": STEP, "lo": round(lo, 10), "counts": [int((k == i).sum()) for i in range(n)]}


def _gap(rows: pl.DataFrame) -> dict:
    """The next day's opening gap by sample (a day's signals averaged), bought or not: its median and the share of
    samples opening lower than the signal day closed."""
    if "gap" not in rows.columns:
        return {"median": None, "down": None, "n": 0}
    v = rows.filter(pl.col("gap").is_not_null()).group_by("market", "signal_date").agg(pl.col("gap").mean())["gap"]
    return {"median": v.median() if v.len() else None, "down": float((v < 0).mean()) if v.len() else None, "n": v.len()}


def _groups(rows: pl.DataFrame, key: pl.Expr, col: str | None = None) -> list[dict]:
    out = []
    for (g,), part in rows.with_columns(key.alias("_g")).filter(pl.col("_g").is_not_null()).group_by("_g"):
        out.append({"key": g, **_stats(part, col)})
    return sorted(out, key=lambda r: (-r["signals"], str(r["key"])))


def summarize(rows: pl.DataFrame, oos_start: date, col: str | None = None) -> dict:
    """The signals' samples, win rate, split and groups by the HOLD-day excess (`col`: another column of `rows`); a
    rule's trade plan is reported apart (wb.validate.research)."""
    col = col or f"x{HOLD}"
    ret = f"r{HOLD}"
    hold = _samples(rows, col)
    v = hold["v"]
    means = rows.filter(pl.col("blocked").is_null()).group_by("market", "signal_date").agg(pl.col("mfe").mean(), pl.col("mae").mean())
    worst = (rows.filter(pl.col("blocked").is_null() & pl.col(col).is_not_null()).sort(col).head(WORST)
             .select("market", "listing_id", "signal_date", "entry_date", pl.col(ret).alias(f"r{HOLD}"), pl.col(col).alias(f"x{HOLD}"),
                     "mae").to_dicts())
    return {
        **_stats(rows, col),
        "metric": "hold",
        "blocked": {k: int((rows["blocked"] == k).sum()) for k in ("limit", "halt")},
        "gone": int(rows["gone"].fill_null(False).sum()),
        "gap": _gap(rows),
        **{k: q for k, q in _quantiles(v).items() if k in ("q25", "q75")},
        "mfe": means["mfe"].mean(), "mae": means["mae"].mean(),
        "horizons": [{"h": h, **_quantiles(_samples(rows, f"x{h}")["v"])} for h in HORIZONS],
        "hist": _hist(v),
        "split": {"oos_start": oos_start, "in": _stats(rows.filter(pl.col("signal_date") < oos_start), col),
                  "out": _stats(rows.filter(pl.col("signal_date") >= oos_start), col)},
        "groups": {"market": _groups(rows, pl.col("market"), col),
                   "year": sorted(_groups(rows, pl.col("signal_date").dt.year(), col), key=lambda r: r["key"]),
                   "industry": _groups(rows, pl.col("industry"), col),
                   "state": _groups(rows, pl.when(pl.col("above200")).then(pl.lit("大盘在 200 日线上"))
                                    .when(pl.col("above200").not_()).then(pl.lit("大盘在 200 日线下")), col)},
        "worst": worst,
        "few": len(v) < FEW,
    }


def halves(rows: pl.DataFrame, oos_start: date, col: str | None = None) -> dict:
    """The signals and each half's effective samples, win rate and median, as `summarize`'s split (条件体检's comparisons)."""
    a, b = _stats(rows.filter(pl.col("signal_date") < oos_start), col), _stats(rows.filter(pl.col("signal_date") >= oos_start), col)
    return {"signals": rows.height, "effective_in": a["effective"], "effective_out": b["effective"], "win_in": a["win"],
            "win_out": b["win"], "median_in": a["median"], "median_out": b["median"], "mean_in": a["mean"], "mean_out": b["mean"]}
