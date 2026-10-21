"""交易时段时钟 (spec §4.8): where a market is in its day — before the open, in the morning or afternoon session, at
lunch, after the close, or closed all day (a weekend or holiday from the stored calendar) — the minutes traded so far
and in the whole day (JP 330, CN 240), which the intraday volume ratio reads."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from wb.realtime.session import session_at

TK, SH = ZoneInfo("Asia/Tokyo"), ZoneInfo("Asia/Shanghai")
OPEN = {date(2026, 10, 2)}  # a Friday


def at(market, h, m, day=date(2026, 10, 2)):
    tz = TK if market == "JP" else SH
    return session_at(market, datetime(day.year, day.month, day.day, h, m, tzinfo=tz), is_open=lambda d: d in OPEN)


@pytest.mark.parametrize("market, hm, state, elapsed", [
    ("JP", (8, 59), "pre", 0), ("JP", (9, 0), "trading", 0), ("JP", (10, 42), "trading", 102),
    ("JP", (11, 45), "lunch", 150), ("JP", (12, 30), "trading", 150), ("JP", (15, 30), "after", 330),
    ("CN", (9, 29), "pre", 0), ("CN", (10, 0), "trading", 30), ("CN", (12, 0), "lunch", 120),
    ("CN", (14, 0), "trading", 180), ("CN", (15, 1), "after", 240),
])
def test_where_a_market_is_in_its_day(market, hm, state, elapsed):
    s = at(market, *hm)
    assert (s.state, s.elapsed) == (state, elapsed) and s.total == (330 if market == "JP" else 240)


def test_a_day_the_calendar_has_closed_is_closed_all_day():
    s = at("JP", 10, 0, day=date(2026, 10, 3))
    assert s.state == "closed" and s.elapsed == 0 and s.label == "休市"
    assert at("JP", 10, 42).label == "前场" and at("JP", 13, 0).label == "后场" and at("CN", 10, 0).label == "上午盘"
