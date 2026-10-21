"""盯盘提醒的三层 (design 2026-10-10 §3): said before a line is reached, not after it is confirmed — prices move in a
second, a confirmed break is a loss already taken.

  竞价 auction  the day's open (the auction's price) already beyond the line, or within ZONE of it
  逼近 near     a minute within ZONE of the line, at a new high of the day (a new low on the selling side) and on the
               line's side of the day's average price, the line not reached yet: 62% of these reached the line that day,
               a median 6 minutes later (selling side 57%, 15 minutes; 2025-10–2026-10 A-share minutes)
  触线 touch    a minute's high reaches the line (its low, on the selling side)

A line's side: up, a line above the price (the buying side, a box top's break line); down, below it (a fail line)."""
import polars as pl

from wb.realtime.today import turnover

ZONE = 0.01


def auction(line: dict, open_: float, zone: float = ZONE) -> str | None:
    """'beyond' when the day's open is already past the line, 'near' when within `zone` of it, else None."""
    p = line["price"]
    if line["side"] == "up":
        return "beyond" if open_ >= p else "near" if open_ >= p * (1 - zone) else None
    return "beyond" if open_ <= p else "near" if open_ <= p * (1 + zone) else None


def with_stats(minutes: pl.DataFrame, prev: dict[str, float]) -> pl.DataFrame:
    """Each minute with what the layers read, from every minute the stock traded today: hh and ll, the day's high and
    low before it (the previous close before the first), and vwap, the day's average price to its end."""
    before = pl.col("listing_id").replace_strict(prev, default=None, return_dtype=pl.Float64)
    return (minutes.sort("listing_id", "at").with_columns(
        pl.col("high").cum_max().shift(1).over("listing_id").fill_null(before).alias("hh"),
        pl.col("low").cum_min().shift(1).over("listing_id").fill_null(before).alias("ll"),
        (turnover(minutes).cum_sum().over("listing_id") / pl.col("volume").cum_sum().over("listing_id")).alias("vwap")))


def judge(line: dict, minutes: list[dict], zone: float = ZONE, near: bool = True) -> list[tuple[str, dict]]:
    """`minutes`: the stock's minutes to judge, in order, with_stats'. ('touch', the first that reaches the line), and
    before it ('near', the first in the approach) unless `near` is False."""
    p, up, out = line["price"], line["side"] == "up", []
    for m in minutes:
        if (m["high"] >= p) if up else (m["low"] <= p):
            return [*out, ("touch", m)]
        gap = (p - m["close"]) / p if up else (m["close"] - p) / p
        rising = m["high"] > m["hh"] and m["close"] >= m["vwap"] if up else m["low"] < m["ll"] and m["close"] <= m["vwap"]
        if near and not out and 0 < gap <= zone and rising:
            out.append(("near", m))
    return out
