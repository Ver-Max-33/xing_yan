"""分时小箱 (spec §8.5): the same BOX primitive on a stock's kept minutes — 1, 5 and 15-minute
bars (a box platform on the 15-minute chart, a small box on the 5-minute one, the buy point on the
1-minute one) — and each break's volume against the bars near that edge inside the box, as BAND_VOL_RATIO reads a
day's box. The bar still forming while the market trades is a process value: marked, never scaled up to a whole bar.
Minutes are kept for a few weeks only, so this layer is for watching, never for backtests.

The waves are a zigzag on the bars' highs and lows — a turn once the price is WAVE[period] back
from the wave's extreme (a research definition: waves are counted by eye, not argued over) —
each with its volume against the previous wave of the same direction: the largest bar (the bar heights
compared) and the whole wave's. 后量不超前量 at a new high is a warning; a dip that makes no new low on less volume
may be a pause in a fall (临时止跌); a fall on more volume is not done.

分时金品 (新元科技's 10-day 分时 shows a 下品 at the right end of its box): the daily 金品 on the boxes of one
period — each bigger box's last small box, 天品 / 上品 / 下品 by where it sits."""
from datetime import date, time, timedelta

import polars as pl

from wb.engine.l2.periods import settings, tick_of
from wb.engine.l2.spec import compute

PERIODS = (1, 5, 15)
DAYS = 20   # the minutes a minute K-line's structures and the 分时小箱 read: the last 20 trading days, today's as polled
WAVE = {1: 0.006, 5: 0.01, 15: 0.015}   # a turn in the waves: this far back from the wave's extreme (research definition)
WAVES = {"start": pl.Datetime, "end": pl.Datetime, "dir": pl.Utf8, "start_price": pl.Float64, "end_price": pl.Float64,
         "volume": pl.Float64, "peak": pl.Float64, "total_ratio": pl.Float64, "peak_ratio": pl.Float64,
         "new_extreme": pl.Boolean, "flag": pl.Utf8, "open": pl.Boolean}
BARS = {"at": pl.Datetime, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
        "volume": pl.Float64, "partial": pl.Boolean}


def resample(minutes: pl.DataFrame, period: int, sessions: list[tuple[time, time]]) -> pl.DataFrame:
    """Each `period`-minute bar inside its session (never across lunch or days; a session's closing auction minute
    joins its last bar): at (the bar's first minute on the clock), open, high, low, close, volume."""
    m = minutes.sort("at").with_columns(pl.col("at").dt.date().alias("_d"), (pl.col("at").dt.hour().cast(pl.Int32) * 60
                                                                             + pl.col("at").dt.minute().cast(pl.Int32)).alias("_m"))
    parts = []
    for k, (a, b) in enumerate(sessions):
        a_m, b_m = a.hour * 60 + a.minute, b.hour * 60 + b.minute
        last = (b_m - a_m - 1) // period
        s = m.filter(pl.col("_m").is_between(a_m, b_m)).with_columns(
            pl.min_horizontal((pl.col("_m") - a_m) // period, pl.lit(last)).alias("_b"), pl.lit(k).alias("_s"))
        parts.append(s.with_columns((pl.col("_d").cast(pl.Datetime) + pl.duration(minutes=pl.col("_b") * period + a_m)).alias("_at")))
    if not parts or all(p.is_empty() for p in parts):
        return pl.DataFrame(schema={"listing_id": pl.Utf8, **{k: v for k, v in BARS.items() if k != "partial"}})
    return (pl.concat(parts).sort("at").group_by("listing_id", "_d", "_s", "_b", maintain_order=True)
            .agg(pl.col("_at").first().alias("at"), pl.col("open").first(), pl.col("high").max(), pl.col("low").min(),
                 pl.col("close").last(), pl.col("volume").sum())
            .sort("listing_id", "at").select("listing_id", "at", "open", "high", "low", "close", "volume"))


def waves(bars: pl.DataFrame, threshold: float) -> pl.DataFrame:
    """The bars' waves, oldest first: an up wave ends at its high once a low comes `threshold` below it, a down wave at
    its low once a high comes `threshold` above; each against the previous wave of its direction. A wave's volume is
    its bars after the one it turned on (that one ends the wave before), so a day's first bar, which carries the opening
    auction, is in none. The last is `open`."""
    if bars.height < 2:
        return pl.DataFrame(schema=WAVES)
    at, h, lo, v = bars["at"].to_list(), bars["high"].to_list(), bars["low"].to_list(), bars["volume"].to_list()
    legs, start, way, ext = [], 0, None, 0   # (dir, start index, end index)
    hi_i = lo_i = 0
    for i in range(1, len(at)):
        if way is None:   # the first turn decides the first wave's direction
            hi_i, lo_i = (i if h[i] > h[hi_i] else hi_i), (i if lo[i] < lo[lo_i] else lo_i)
            if h[i] >= lo[lo_i] * (1 + threshold) and lo_i < i:
                way, start, ext = "up", lo_i, i
            elif lo[i] <= h[hi_i] * (1 - threshold) and hi_i < i:
                way, start, ext = "down", hi_i, i
        elif way == "up":
            if h[i] > h[ext]:
                ext = i
            elif lo[i] <= h[ext] * (1 - threshold):
                legs.append(("up", start, ext))
                way, start, ext = "down", ext, i
        else:
            if lo[i] < lo[ext]:
                ext = i
            elif h[i] >= lo[ext] * (1 + threshold):
                legs.append(("down", start, ext))
                way, start, ext = "up", ext, i
    if way is not None:
        legs.append((way, start, ext))
    rows, last = [], {}
    for k, (d, s, e) in enumerate(legs):
        vol, peak = sum(v[s + 1:e + 1]), max(v[s + 1:e + 1], default=0.0)
        sp, ep = (lo[s], h[e]) if d == "up" else (h[s], lo[e])
        before = last.get(d)
        tr = vol / before[0] if before and before[0] else None
        pr = peak / before[1] if before and before[1] else None
        new = (ep > before[2] if d == "up" else ep < before[2]) if before else None
        flag = None
        if before and d == "up":
            flag = "新高量不足" if new and pr is not None and pr < 1 else ("未创新高" if not new else None)
        elif before:
            flag = "不创新低、量缩" if not new and pr is not None and pr < 1 else ("下跌放量" if pr is not None and pr > 1 else None)
        rows.append((at[s], at[e], d, sp, ep, vol, peak, tr, pr, new, flag, k == len(legs) - 1))
        last[d] = (vol, peak, ep)
    return pl.DataFrame(rows, schema=WAVES, orient="row")


def day_waves(bars: pl.DataFrame, threshold: float, trading: bool) -> pl.DataFrame:
    """Each day's waves on its own (a day's waves are counted on its own 分时); only the last day's last wave can still be
    going, and only while the market trades."""
    if bars.is_empty():
        return pl.DataFrame(schema=WAVES)
    days = sorted(set(bars["at"].dt.date().to_list()))
    parts = [waves(bars.filter(pl.col("at").dt.date() == d), threshold).with_columns(
        pl.col("open") & pl.lit(trading and k == len(days) - 1)) for k, d in enumerate(days)]
    return pl.concat(parts)


def micro_boxes(minutes: pl.DataFrame, sessions: list[tuple[time, time]], period: int = 1, trading: bool = False,
                market: str = "CN") -> dict:
    """One stock's kept minutes as `period`-minute bars (`partial`: the bar still forming), the BOX boxes found in them
    (start, end, known_at, ended_at as minute times) and their events, each break with `ref_ratio`: its volume over the
    largest volume of the box's bars near that edge (the reference bar near that edge, on the same period). The settings are the
    period's (wb.engine.l2.periods: the minute K-line finds the same boxes; no band was given for minutes)."""
    bars = resample(minutes, period, sessions)
    bars = bars.with_columns(pl.Series("partial", [trading and k == bars.height - 1 for k in range(bars.height)], dtype=pl.Boolean))
    if bars.height < 5:
        return {"bars": bars.drop("listing_id"), "boxes": pl.DataFrame(), "events": pl.DataFrame(), "gold": pl.DataFrame(),
                "waves": day_waves(bars, WAVE[period], trading)}
    fake = [date(2000, 1, 1) + timedelta(days=k) for k in range(bars.height)]   # BOX counts bars; its tables keep days
    ats = bars["at"].to_list()
    back = dict(zip(fake, ats, strict=True))
    price = float(bars["close"].median())
    params = settings(f"m{period}", price, tick_of(market, price))
    tol = params["BOX"]["tol"]
    l2 = bars.with_columns(pl.Series("date", fake, dtype=pl.Date), pl.lit(False).alias("limit_up_close"))
    tables = compute(l2.select("listing_id", "date", "open", "high", "low", "close", "volume", "limit_up_close"),
                     ["BOX", "SLANT_BOX", "BOX_DERIVED"], params)
    out = tables["BOX"]
    as_at = lambda c: pl.col(c).replace_strict(back, default=None, return_dtype=pl.Datetime).alias(c)  # noqa: E731
    golds = tables["BOX_DERIVED"]["gold"].select("kind", "variant", "big_box_id", "small_box_id", as_at("known_at"), as_at("superseded_at"),
                                                 "inner_distance", "above_top")
    boxes = out["boxes"].with_columns([as_at(c) for c, t in out["boxes"].schema.items() if t == pl.Date])   # every day back to its minute
    events = out["events"].with_columns(as_at("date").alias("at"), as_at("known_at")).drop("date")
    index = {a: k for k, a in enumerate(ats)}
    vol, high, low, partial = bars["volume"].to_list(), bars["high"].to_list(), bars["low"].to_list(), bars["partial"].to_list()
    edges = {r["box_id"]: r for r in boxes.iter_rows(named=True)}
    ratios, parts = [], []
    for e in events.iter_rows(named=True):
        j, box = index.get(e["at"]), edges.get(e["box_id"])
        ratio = None
        if j is not None and box and e["event"] in ("break_up", "break_down"):
            near = [vol[k] for k in range(index.get(box["start"], 0), j)
                    if (high[k] >= box["top"] * (1 - tol) if e["event"] == "break_up" else low[k] <= box["bottom"] * (1 + tol))]
            ratio = vol[j] / max(near) if near and max(near) > 0 else None
        ratios.append(ratio)
        parts.append(bool(j is not None and partial[j]))
    events = events.with_columns(pl.Series("ref_ratio", ratios, dtype=pl.Float64), pl.Series("partial", parts, dtype=pl.Boolean))
    return {"bars": bars.drop("listing_id"), "boxes": boxes, "events": events.select("box_id", "at", "event", "value", "ref_ratio", "partial", "known_at"),
            "gold": golds, "waves": day_waves(bars, WAVE[period], trading)}
