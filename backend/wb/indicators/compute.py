"""An indicator on a chart's own bars (design 2026-10-10 §4): the series the K 线 draws (its basis, its period, the
bar still forming) is the frame the formula reads, so a line meets the candles to the cent. Daily charts also have
the fields the rules read (估值, ST, the index …) joined by date — not the 后复权 prices among them, which would not
be the chart's. DRAWZONE / DRAWLEVEL are carried right until the bars after them meet the end the formula asks for."""
from datetime import date

import numpy as np
import polars as pl

from wb.charts.series import Series, to_epoch_ms
from wb.engine.l1.fields import FIELDS
from wb.engine.l1.run import evaluate
from wb.engine.l1.script import Compiled, Item, compile_script
from wb.engine.l1.units import U
from wb.indicators import Indicator

# the chart's own columns a formula reads, by period
BASE = {"open", "high", "low", "close", "volume", "amount", "raw_open", "raw_high", "raw_low", "raw_close", "pre", "vwap"}
PRICE_UNITS = (U.PRICE,)
CAP = 400   # zones / levels kept per drawing: the latest (older ones lie far off the screen)


def chart_frame(series: Series, listing_id: str) -> pl.DataFrame:
    """The chart's bars as a formula frame: prices as shown, raw prices (shown ÷ the basis' scale), 昨收 (the
    exchange's, as shown; else the bar before), 均价 and, on a daily chart, 换手率."""
    f = series.frame
    cols = [pl.lit(listing_id).alias("listing_id"), "date", "open", "high", "low", "close", "volume", "amount", "vwap",
            *[(pl.col(p) / pl.col("scale")).alias(f"raw_{p}") for p in ("open", "high", "low", "close")],
            pl.coalesce("pre_close", pl.col("close").shift(1)).alias("pre")]
    if series.period == "day":
        cols.append("turnover_rate")
    return f.select(cols)


def joinable(available: set[str]) -> set[str]:
    """The rule frame's columns a daily chart may take as they are (not prices: those are the rules' 后复权)."""
    price = {f.column for f in FIELDS.values() if f.unit in PRICE_UNITS}
    return {c for c in available if c not in price} - BASE


def compile_for(ind: Indicator, market: str, available: set[str] | None, params: dict | None = None) -> Compiled:
    """The indicator compiled for one chart: `available` the columns it can read (None: any, for a check)."""
    return compile_script(ind.formula, market, ind.param_values(params), available)


def compute(ind: Indicator, series: Series, listing_id: str, market: str, store_frame=None,
            store_columns: set[str] = frozenset(), calendar_open: list[date] | None = None,
            params: dict | None = None) -> dict:
    """{pane, items, t, values, zones, levels} or {error}: the indicator over the series. `store_frame(columns)`:
    the listing's rule frame with those columns (daily only); `store_columns`: what it can give."""
    frame = chart_frame(series, listing_id)
    available = set(frame.columns) | (joinable(store_columns) if series.period == "day" else set())
    c = compile_for(ind, market, available, params)
    if c.program is None:
        if series.period != "day":   # a field only a day's bar has (换手率, 估值 …): said as that, not as missing data
            whole = compile_for(ind, market, None, params)
            lacking = whole.program.columns - set(frame.columns) - {"listing_id"} if whole.program is not None else set()
            if lacking:
                names = sorted({f.doc for f in FIELDS.values() if f.column in lacking})
                return {"error": f"{'、'.join(names)}只有日线有，周线、月线上画不了这个指标（日线上可以）"}
        lines = [e["message"] for e in c.errors] + [f"{why}：{'、'.join(n)}" for why, n in c.unsupported.items()]
        return {"error": "；".join(lines) or "算不出来"}
    extra = sorted(c.program.columns - set(frame.columns) - {"listing_id"})
    if extra and store_frame is not None:
        joined = store_frame(set(extra)).select("date", *extra)
        frame = frame.join(joined, on="date", how="left", maintain_order="left")
    out = evaluate(frame, c.program, calendar_open)
    t = [to_epoch_ms(d) for d in out["date"].to_list()]
    used = sorted({col for it in c.items for col in it.cols.values()})
    values = {col: _json(out[col]) for col in used}
    h, lo, cl = (frame[k].to_numpy().astype(float) for k in ("high", "low", "close"))
    zones, levels = [], []
    for k, it in enumerate(c.items):
        if it.kind == "zone":
            zones += _zones(k, it, out, h, lo, cl, t)
        elif it.kind == "level":
            levels += _levels(k, it, out, h, lo, cl, t)
    pane = ind.pane if ind.pane != "auto" else ("main" if c.price_only else "sub")
    return {"pane": pane, "items": [_item(it) for it in c.items], "t": t, "values": values, "zones": zones, "levels": levels,
            "levels_ref": list(ind.levels)}


def _json(s: pl.Series) -> list:
    if s.dtype == pl.Boolean:
        return s.to_list()
    v = s.cast(pl.Float64)
    return pl.select(pl.when(v.is_finite()).then(v)).to_series().to_list()   # JSON has no NaN / inf: unknown


def _item(it: Item) -> dict:
    return {k: getattr(it, k) for k in ("kind", "name", "cols", "color", "color2", "width", "dash", "hidden", "icon", "text",
                                        "empty", "end", "maxn", "price")}


def _arr(out: pl.DataFrame, col: str | None, default: float = np.nan) -> np.ndarray:
    if col is None:
        return np.full(out.height, default)
    return out[col].cast(pl.Float64).to_numpy().astype(float)


def _starts(it: Item, out: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """The bars where the drawing begins (its condition true) and how many bars before it it is drawn from."""
    cond = out[it.cols["cond"]].fill_null(False).to_numpy()
    offset = np.nan_to_num(_arr(out, it.cols.get("offset"), 0.0)).round().astype(int)
    return np.flatnonzero(cond), offset


def _end(end: int, above: bool, top: float, bottom: float, h, lo, cl, i: int) -> int | None:
    """The first bar after i that ends a zone [bottom, top] lying above (or below) the price where it began — 1 traded
    through it, 2 touched it, 3 closed beyond it, 0 never — or None: it still stands."""
    if end == 0:
        return None
    if end == 1:
        hit = h[i + 1:] >= top if above else lo[i + 1:] <= bottom
    elif end == 2:
        hit = h[i + 1:] >= bottom if above else lo[i + 1:] <= top
    else:
        hit = cl[i + 1:] > top if above else cl[i + 1:] < bottom
    j = np.flatnonzero(hit)
    return int(i + 1 + j[0]) if j.size else None


def _zones(k: int, it: Item, out, h, lo, cl, t) -> list[dict]:
    top, bottom = _arr(out, it.cols["top"]), _arr(out, it.cols["bottom"])
    rows = []
    starts, offset = _starts(it, out)
    for i in starts[-(it.maxn or CAP):]:
        a, b = max(top[i], bottom[i]), min(top[i], bottom[i])
        if np.isnan(a) or np.isnan(b):
            continue
        above = cl[i] <= (a + b) / 2          # the zone over the price where it began: a resistance; else a support
        e = _end(it.end, above, a, b, h, lo, cl, i)
        rows.append({"item": k, "s": t[max(0, i - int(offset[i]))], "at": t[i], "e": t[e] if e is not None else None,
                     "top": float(a), "bottom": float(b)})
    return rows


def _levels(k: int, it: Item, out, h, lo, cl, t) -> list[dict]:
    price = _arr(out, it.cols["price"])
    rows = []
    starts, offset = _starts(it, out)
    for i in starts[-(it.maxn or CAP):]:
        p = price[i]
        if np.isnan(p):
            continue
        above = p >= cl[i]                    # over the price: taken when a high reaches it; under: a low
        e = _end(it.end, above, p, p, h, lo, cl, i)
        rows.append({"item": k, "s": t[max(0, i - int(offset[i]))], "at": t[i], "e": t[e] if e is not None else None,
                     "price": float(p)})
    return rows
