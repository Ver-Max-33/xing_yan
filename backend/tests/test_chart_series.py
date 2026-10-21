from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.charts.series import build, page, to_epoch_ms
from wb.datasets import conform

D0 = date(2026, 1, 5)  # Monday


def cn_daily(n=130):
    """A-share series with a 1:2 bonus ex-date on day 100 (raw price halves, factor doubles)."""
    days = [D0 + timedelta(days=i + 2 * (i // 5)) for i in range(n)]  # weekdays only
    raw = [10.0 + 0.01 * i if i < 100 else (10.0 + 0.01 * i) / 2 for i in range(n)]
    adj = [1.0 if i < 100 else 2.0 for i in range(n)]
    pre = [raw[0]] + [raw[i - 1] * adj[i - 1] / adj[i] for i in range(1, n)]
    return conform(pl.DataFrame({
        "listing_id": "CN:600519", "date": days, "open": raw, "high": [r * 1.01 for r in raw],
        "low": [r * 0.99 for r in raw], "close": raw, "pre_close": pre, "volume": [1000.0] * n,
        "amount": [r * 1000 for r in raw], "adj_factor": adj, "share_factor": adj,
        "up_limit": [p * 1.1 for p in pre], "down_limit": [p * 0.9 for p in pre], "suspended": False,
        "source": "t"}), "bars_1d")


def test_qfq_latest_equals_raw_and_history_is_continuous():
    s = build(cn_daily(), "CN:600519", "CN", "day", "qfq")
    f = s.frame
    assert s.adjusted and f["close"][-1] == pytest.approx((10 + 0.01 * 129) / 2)  # today's real price
    jump = f["close"][100] / f["close"][99]
    assert jump == pytest.approx((10 + 1.00) / (10 + 0.99))  # no artificial halving on the ex-date
    assert f["volume"][99] == pytest.approx(2000.0)  # pre-bonus volume in today's share units


def test_raw_basis_shows_the_ex_date_drop():
    f = build(cn_daily(), "CN:600519", "CN", "day", "raw").frame
    assert f["close"][100] / f["close"][99] == pytest.approx(0.5, rel=0.01)


def test_unknown_factor_falls_back_to_raw_and_says_so():
    d = cn_daily().with_columns(pl.lit(None, pl.Float64).alias("adj_factor"), pl.lit(None, pl.Float64).alias("share_factor"))
    s = build(d, "CN:600519", "CN", "day", "qfq")
    assert not s.adjusted and s.frame["close"][100] / s.frame["close"][99] == pytest.approx(0.5, rel=0.01)


def test_limit_flags_cn():
    d = cn_daily().with_columns(
        pl.when(pl.col("date") == pl.col("date").max()).then(pl.col("pre_close") * 1.1).otherwise(pl.col("close")).alias("close"))
    d = d.with_columns(pl.max_horizontal("high", "close").alias("high"))
    f = build(d, "CN:600519", "CN", "day", "qfq").frame
    assert f["limit_up"][-1] and not f["limit_up"][-2]


def test_week_aggregation_and_completion():
    d = cn_daily(12)  # Mon 2026-01-05 .. Tue 2026-01-20 (weekdays)
    cal = sorted(set(d["date"].to_list()) | {date(2026, 1, 21), date(2026, 1, 22), date(2026, 1, 23)})
    f = build(d, "CN:600519", "CN", "week", "raw", cal).frame
    assert f["date"].to_list() == [date(2026, 1, 9), date(2026, 1, 16), date(2026, 1, 20)]
    assert f["complete"].to_list() == [True, True, False]  # the third week still has trading days
    assert f["open"][0] == pytest.approx(10.0) and f["close"][0] == pytest.approx(10.04)
    assert f["volume"][0] == 5000.0


def test_jp_limit_prices_from_price_band():
    days = [D0 + timedelta(days=i) for i in range(3)]
    d = conform(pl.DataFrame({"listing_id": "JP:7203", "date": days, "open": [2800.0] * 3, "high": [2900.0, 3345.0, 3350.0],
                              "low": [2790.0] * 3, "close": [2845.0, 3300.0, 3350.0], "volume": [1.0] * 3,
                              "adj_factor": [1.0] * 3, "share_factor": [1.0] * 3, "ul": [False, True, True]}), "bars_1d")
    f = build(d, "JP:7203", "JP", "day", "qfq").frame
    # 2845 + 500; day 3: the table says 3300 + 700 = 4000, but UL=1 at 3350 contradicts it, so the
    # price is unknown that day (spec §4.4) while the exchange's own flag still marks the limit close
    assert f["up_limit"].to_list() == [None, 3345.0, None]
    # day 2 touched the limit (UL=1) but closed below the high: not a limit close
    assert f["limit_up"].to_list() == [False, False, True]


def test_vol_class_and_macd_cross_present():
    f = build(cn_daily(), "CN:600519", "CN", "day", "qfq").frame
    assert {"vol_class", "macd_cross", "amp60", "new_high_250"} <= set(f.columns)
    assert f["amp60"][58] is None and f["amp60"][59] is not None


def test_page_backwards():
    s = build(cn_daily(), "CN:600519", "CN", "day", "qfq")
    rows, more = page(s, limit=50)
    assert len(rows) == 50 and more and rows[-1]["date"] == str(s.frame["date"][-1])
    first = date.fromisoformat(rows[0]["date"])
    older, more2 = page(s, end=first - timedelta(days=1), limit=500)
    assert len(older) == 80 and not more2
    assert rows[0]["t"] == to_epoch_ms(first)


def test_week_is_not_complete_when_the_calendar_ends_with_the_data():
    # right after a backfill the calendar may stop at the last bar: the week is unknown, not done
    d = cn_daily(12)  # ends Tue 2026-01-20
    f = build(d, "CN:600519", "CN", "week", "raw", d["date"].to_list()).frame
    assert f["complete"].to_list() == [True, True, False]


def test_vol_mult_is_unknown_after_a_zero_volume_day():
    d = cn_daily(5).with_columns(pl.Series("volume", [1000.0, 0.0, 1000.0, 500.0, 1000.0]))
    f = build(d, "CN:600519", "CN", "day", "raw").frame
    assert f["vol_mult"].to_list() == [None, 0.0, None, 0.5, 2.0]
    rows, _ = page(build(d, "CN:600519", "CN", "day", "raw"))
    assert all(v == v and v not in (float("inf"), float("-inf")) for r in rows for v in r.values() if isinstance(v, float))


def test_daily_scale_change_points_come_with_every_period():
    # a drawing anchored on a daily bar before a mid-week ex-date keeps that day's scale on the weekly chart
    d = cn_daily(130)  # ex-date (factor 1 -> 2) on day 100
    s = build(d, "CN:600519", "CN", "week", "qfq")
    assert s.scales == [(d["date"][0].isoformat(), 0.5), (d["date"][100].isoformat(), 1.0)]
    week = s.frame.filter(pl.col("date") >= d["date"][100]).row(0, named=True)
    assert week["scale"] == 1.0  # the week bar's own scale is its last day's, hence the separate list


def test_year_left_gap_from_half_year_breakout_to_one_year_breakout():
    n = 258
    close = [10.0] * n
    close[5] = 20.0                    # the one-year high, far left
    for i, c in zip(range(250, 254), (16.0, 17.0, 18.0, 19.0), strict=True):
        close[i] = c                   # breaks the half-year high from bar 250 on (the 笑脸)
    close[254] = 25.0                  # and the one-year high on bar 254 (the 箭头)
    days = [D0 + timedelta(days=i) for i in range(n)]
    d = conform(pl.DataFrame({"listing_id": "CN:600519", "date": days, "open": close, "high": close, "low": close,
                              "close": close, "volume": 1000.0, "adj_factor": 1.0, "share_factor": 1.0}), "bars_1d")
    f = build(d, "CN:600519", "CN", "day", "raw").frame
    assert f["year_gap"][253] is None and f["year_gap"][254] == 4 and f["year_gap"][257] == 4
    assert f["year_half"][250] == 0 and f["year_year"][254] == 0


def test_jp_limits_are_unknown_when_the_exchange_widened_them():
    # TSE widens the band after days stuck at the limit: a high above the table price means the
    # table does not apply that day, so no limit line is drawn rather than a wrong one
    days = [D0 + timedelta(days=i) for i in range(3)]
    d = conform(pl.DataFrame({"listing_id": "JP:7203", "date": days, "open": [1000.0] * 3, "high": [1000.0, 1150.0, 1500.0],
                              "low": [990.0, 1000.0, 1150.0], "close": [1000.0, 1150.0, 1500.0], "volume": [1.0] * 3,
                              "adj_factor": 1.0, "share_factor": 1.0}), "bars_1d")
    f = build(d, "JP:7203", "JP", "day", "raw").frame
    assert f["up_limit"].to_list() == [None, 1300.0, None]  # 1000 + 300; day 3 traded at 1500 > 1150 + 300


def test_golden_crosses_are_classed_by_the_zero_axis_and_by_the_60_day_line():
    f = build(cn_daily(), "CN:600519", "CN", "day", "qfq").frame
    x = f.filter(pl.col("macd_cross").is_not_null())
    assert x.height > 0
    want = [None if m is None else ("below" if c < m else "above") for c, m in zip(x["close"], x["ma60"], strict=True)]
    assert x["macd_cross_ma60"].to_list() == want                         # 按价格在 60 日线上下区分 (spec §8.6)
    assert f.filter(pl.col("macd_cross").is_null())["macd_cross_ma60"].null_count() == f.height - x.height


# ---- minute bars (the K-line page's 1 / 5 / 15 / 30 / 60 分钟, 10-12) ------------------------------------------------------
def _day_of_minutes(day: date, base: float = 10.0) -> pl.DataFrame:
    """A whole A-share day of 1-minute bars (at: the minute's start), each 0.01 higher, 100 shares and 1,000 yuan a minute."""
    from datetime import datetime
    ats = [datetime(day.year, day.month, day.day, 9, 30) + timedelta(minutes=k) for k in range(120)] + \
          [datetime(day.year, day.month, day.day, 13, 0) + timedelta(minutes=k) for k in range(120)]
    px = [base + 0.01 * k for k in range(240)]
    return pl.DataFrame({"listing_id": "CN:600000", "at": ats, "open": px, "high": [p + 0.005 for p in px], "low": [p - 0.005 for p in px],
                         "close": px, "volume": [100.0] * 240, "amount": [1000.0] * 240})


def test_minute_bars_are_cut_inside_each_session_and_stamped_with_their_end():
    """As A-share software shows them: the 5-minute bar of 9:30–9:35 is 09:35; none spans lunch (11:30 ends the morning,
    13:05 is the afternoon's first); the day's last is 15:00; 60 minutes make 10:30, 11:30, 14:00, 15:00."""
    from datetime import datetime

    from wb.charts.series import minute_bars
    from wb.markets import load_market
    sessions, d = load_market("CN").sessions, date(2026, 10, 12)
    b5 = minute_bars(_day_of_minutes(d), sessions, 5)
    assert b5.height == 48 and [b5["end"][k] for k in (0, 23, 24, 47)] == [
        datetime(2026, 10, 12, 9, 35), datetime(2026, 10, 12, 11, 30), datetime(2026, 10, 12, 13, 5), datetime(2026, 10, 12, 15, 0)]
    first = b5.row(0, named=True)
    assert (first["open"], first["close"], first["high"], first["low"], first["volume"], first["amount"]) == pytest.approx(
        (10.0, 10.04, 10.045, 9.995, 500.0, 5000.0))
    assert minute_bars(_day_of_minutes(d), sessions, 60)["end"].to_list() == [
        datetime(2026, 10, 12, 10, 30), datetime(2026, 10, 12, 11, 30), datetime(2026, 10, 12, 14, 0), datetime(2026, 10, 12, 15, 0)]


def test_a_minute_series_has_the_charts_columns_each_day_in_the_shown_basis():
    """Two days, the first scaled by its 复权 factor as the daily chart scales it (a split between them does not jump);
    the indicators a day's bar has, those of a day alone left out; times the chart reads as the exchange's clock."""
    from datetime import datetime

    from wb.charts.series import OUTPUT, minute_series
    d1, d2 = date(2026, 10, 9), date(2026, 10, 12)
    m = pl.concat([_day_of_minutes(d1, 20.0), _day_of_minutes(d2, 10.0)])
    s = minute_series(m, "CN:600000", "CN", "m30", "qfq", True, {d1: 0.5, d2: 1.0})
    assert s.frame.columns == OUTPUT and s.frame.height == 16 and s.period == "m30"
    assert s.frame["open"][0] == pytest.approx(10.0) and s.frame["open"][8] == pytest.approx(10.0)   # 20.0 × 0.5 on d1
    assert s.frame["ma5"][4] is not None and s.frame["amp60"].null_count() == s.frame.height
    rows, more = page(s)
    assert not more and rows[0]["date"] == "2026-10-09T10:00" and rows[0]["t"] == int(datetime(2026, 10, 9, 10, 0).replace(
        tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)


# review 10-13 (indicators)
def test_a_week_with_an_ex_date_has_its_vwap_inside_the_week():
    """cn_daily: a 1:2 bonus on day 100. The week's 均价 is its days' 均价 weighed by their volume, both as shown — not
    the raw 成交额 over raw shares times the last day's scale (13 against a high of 5.6)."""
    day = build(cn_daily(), "CN:600519", "CN", "day", "qfq").frame
    for period in ("week", "month"):
        f = build(cn_daily(), "CN:600519", "CN", period, "qfq").frame
        assert ((f["vwap"] <= f["high"] * (1 + 1e-9)) & (f["vwap"] >= f["low"] * (1 - 1e-9))).all(), period
    week = build(cn_daily(), "CN:600519", "CN", "week", "qfq").frame
    d = day.with_columns(pl.col("date").dt.week().alias("w"), pl.col("date").dt.iso_year().alias("y"))
    want = d.group_by("y", "w").agg(pl.col("date").max(), ((pl.col("vwap") * pl.col("volume")).sum() / pl.col("volume").sum()).alias("v"))
    j = week.join(want, on="date")
    assert j.height == week.height and np.allclose(j["vwap"], j["v"], rtol=1e-12)
