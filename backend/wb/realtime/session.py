"""交易时段时钟 (spec §4.8): where a market is in its day at a moment — before the open, trading, at lunch, after the
close, or closed all day — and the minutes traded so far and in the whole day (JP 9:00–11:30 and 12:30–15:30: 330;
CN 9:30–11:30 and 13:00–15:00: 240), which the intraday volume ratio and the time-scaled volume read."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import polars as pl

from wb.markets import load_market

LABELS = {"JP": ("前场", "后场"), "CN": ("上午盘", "下午盘")}


@dataclass(frozen=True)
class Session:
    market: str
    day: date          # the exchange's date
    state: str         # pre / trading / lunch / after / closed
    elapsed: int       # minutes traded so far today
    total: int         # minutes in the whole day
    label: str         # what the page shows: 前场、后场、上午盘、下午盘、午休、未开盘、已收盘、休市
    now: datetime      # the exchange's local time


def minute_span(a: time, b: time) -> int:
    return (b.hour - a.hour) * 60 + b.minute - a.minute


def in_session(sessions: list[tuple[time, time]], at: pl.Expr) -> pl.Expr:
    """A minute inside a session, its close included (11:30 and 15:30 carry the closing auctions): a source's bars over
    lunch (Yahoo sends flat ones for Shenzhen) are not the market's."""
    m = at.dt.hour().cast(pl.Int32) * 60 + at.dt.minute().cast(pl.Int32)
    return pl.any_horizontal([m.is_between(a.hour * 60 + a.minute, b.hour * 60 + b.minute) for a, b in sessions])


def minute_index(sessions: list[tuple[time, time]], at: pl.Expr) -> pl.Expr:
    """Where the minute starting at `at` (exchange time) falls in the trading day: 0 for the open's, lunch left out."""
    m = at.dt.hour().cast(pl.Int32) * 60 + at.dt.minute().cast(pl.Int32)
    return pl.sum_horizontal([(m - (a.hour * 60 + a.minute)).clip(0, minute_span(a, b)) for a, b in sessions])


def session_at(market: str, now: datetime, is_open: Callable[[date], bool | None]) -> Session:
    """`is_open(day)`: the stored calendar's answer (None: not covered — a weekday is taken as open)."""
    m = load_market(market)
    t = now.astimezone(ZoneInfo(m.timezone))
    day, cur = t.date(), t.time().replace(second=0, microsecond=0)
    total = sum(minute_span(a, b) for a, b in m.sessions)
    open_ = is_open(day)
    if open_ is False or (open_ is None and day.weekday() >= 5):
        return Session(market, day, "closed", 0, total, "休市", t)
    elapsed = 0
    for i, (a, b) in enumerate(m.sessions):
        if cur < a:
            return Session(market, day, "pre" if i == 0 else "lunch", elapsed, total, "未开盘" if i == 0 else "午休", t)
        if cur < b:
            return Session(market, day, "trading", elapsed + minute_span(a, cur), total, LABELS[market][i], t)
        elapsed += minute_span(a, b)
    return Session(market, day, "after", total, total, "已收盘", t)
