"""分时小箱 (spec §8.5): the same BOX primitive on the kept minutes — 1, 5 and 15-minute bars (one looks
for a box platform on the 15-minute chart, a small box on the 5-minute one and the buy point on the 1-minute one) —
each break's volume against the bars near that edge in the box, and the bar still forming marked as a process value."""
import math
from datetime import datetime, time, timedelta

import polars as pl
import pytest

from wb.realtime.micro import micro_boxes, resample
from wb.realtime.providers.yfinance_rt import MINUTES

JP_SESSIONS = [(time(9, 0), time(11, 30)), (time(12, 30), time(15, 30))]


def day_of_minutes(day=datetime(2026, 10, 2), breakout_at=120, burst=6000.0):
    """30 minutes up, a box 100.8–101.6 for 90 minutes, then up through its top with `burst` volume."""
    rows, px = [], 100.0
    for k in range(150):
        if k < 30:
            px *= 1.0006
        elif k < breakout_at:
            px = 101.2 + 0.4 * math.sin(k / 4.0)
        else:
            px = 102.5 + 0.05 * (k - breakout_at)
        at = day + timedelta(hours=9, minutes=k)
        rows.append(("JP:7203", at, px, px + 0.03, px - 0.03, px, burst if k == breakout_at else 1000.0))
    return pl.DataFrame(rows, schema=MINUTES, orient="row")


def test_minutes_resample_inside_each_session_never_across_lunch_or_days():
    m = pl.DataFrame([("JP:7203", datetime(2026, 10, 2, 11, 28), 1.0, 2.0, 0.5, 1.5, 10.0),
                      ("JP:7203", datetime(2026, 10, 2, 11, 29), 1.5, 3.0, 1.0, 2.5, 20.0),
                      ("JP:7203", datetime(2026, 10, 2, 12, 30), 2.5, 2.6, 2.4, 2.45, 5.0)], schema=MINUTES, orient="row")
    five = resample(m, 5, JP_SESSIONS)
    assert five.select("at", "open", "high", "low", "close", "volume").rows() == [
        (datetime(2026, 10, 2, 11, 25), 1.0, 3.0, 0.5, 2.5, 30.0), (datetime(2026, 10, 2, 12, 30), 2.5, 2.6, 2.4, 2.45, 5.0)]


def test_a_box_in_the_minutes_is_found_with_its_times_and_the_break_says_how_its_volume_compares():
    out = micro_boxes(day_of_minutes(), JP_SESSIONS, period=1, trading=False)
    bigs = out["boxes"].filter(pl.col("scale") != "short")["box_id"]
    brk = out["events"].filter((pl.col("event") == "break_up") & pl.col("box_id").is_in(bigs.implode())).sort("at").row(0, named=True)
    assert brk["at"] == datetime(2026, 10, 2, 11, 0) and brk["ref_ratio"] == pytest.approx(6.0)        # 6000 vs 1000 near the top
    box = out["boxes"].filter(pl.col("box_id") == brk["box_id"]).row(0, named=True)
    assert time(9, 0) < box["start"].time() < time(10, 0) and box["top"] == pytest.approx(101.6, abs=0.1)   # minute times, not days
    assert pl.Date not in out["boxes"].dtypes and pl.Date not in out["events"].dtypes                        # none left as stand-in days
    assert not out["events"]["partial"].any()


def test_the_bar_still_forming_is_a_process_value_never_scaled_up():
    # the market is trading: the last 5-minute bar has only part of its minutes, and says so
    out = micro_boxes(day_of_minutes().head(122), JP_SESSIONS, period=5, trading=True)
    assert out["bars"]["partial"].to_list()[-1] is True and out["bars"]["partial"].to_list()[:-1] == [False] * (out["bars"].height - 1)
    assert out["bars"]["volume"][-1] == 6000.0 + 1000.0                                                # 11:00 and 11:01 only, as they are


def path_minutes(legs, start=100.0, day=datetime(2026, 10, 2)):
    """legs: (minutes, end price, volume a minute) — prices move evenly; bars ±0.02 around the close."""
    rows, px, k = [], start, 0
    for n, end, vol in legs:
        step = (end - px) / n
        for _ in range(n):
            px += step
            rows.append(("JP:7203", day + timedelta(hours=9, minutes=k), px, px + 0.02, px - 0.02, px, vol))
            k += 1
    return pl.DataFrame(rows, schema=MINUTES, orient="row")


def test_the_minutes_waves_with_each_ones_volume_against_the_one_before():
    # up with volume, back on less; a new high on less volume than the wave before (后量不超前量) is a
    # warning; a dip that makes no new low on less volume may be a pause in a fall (临时止跌) — research definitions
    m = path_minutes([(10, 102.0, 1000.0), (5, 101.0, 500.0), (10, 103.0, 1500.0), (5, 102.2, 400.0), (10, 103.5, 900.0),
                      (5, 102.6, 300.0)])
    w = micro_boxes(m, JP_SESSIONS, period=1, trading=True)["waves"]
    assert w["dir"].to_list()[:5] == ["up", "down", "up", "down", "up"]
    up2, down2, up3 = w.row(2, named=True), w.row(3, named=True), w.row(4, named=True)
    assert up2["peak_ratio"] == pytest.approx(1.5) and up2["new_extreme"] is True and up2["flag"] is None
    assert down2["new_extreme"] is False and down2["peak_ratio"] == pytest.approx(0.8) and down2["flag"] == "不创新低、量缩"
    assert up3["new_extreme"] is True and up3["peak_ratio"] == pytest.approx(0.6) and up3["flag"] == "新高量不足"
    assert up3["total_ratio"] == pytest.approx(900 * 10 / (1500 * 10))
    assert w["open"].to_list()[-1] is True and not any(w["open"].to_list()[:-1])          # the market trades: the last is going
    assert not any(micro_boxes(m, JP_SESSIONS, period=1)["waves"]["open"])               # a day that is over: none


def test_waves_are_counted_within_each_day():
    # a day's waves are counted on its own 分时: the first wave of a day has nothing before it, whatever the day before did
    one = path_minutes([(10, 102.0, 1000.0), (5, 101.0, 500.0), (10, 103.0, 1500.0)])
    two = path_minutes([(10, 104.0, 300.0), (5, 103.0, 200.0), (10, 105.0, 900.0)], start=103.0, day=datetime(2026, 10, 5))
    w = micro_boxes(pl.concat([one, two]), JP_SESSIONS, period=1)["waves"]
    second = w.filter(pl.col("start").dt.date() == datetime(2026, 10, 5).date())
    assert second.row(0, named=True)["peak_ratio"] is None and second.row(0, named=True)["dir"] == "up"
    assert second.row(2, named=True)["peak_ratio"] == pytest.approx(3.0)                 # against the day's own first wave


def test_a_days_first_minute_is_in_no_waves_volume():
    # the first minute carries the opening auction: counted, it would make the day's first wave look heavier than any after
    m = path_minutes([(10, 102.0, 1000.0), (5, 101.0, 500.0), (10, 103.0, 1500.0)])
    vol = m["volume"].to_list()
    vol[0] = 50000.0
    w = micro_boxes(m.with_columns(pl.Series("volume", vol)), JP_SESSIONS, period=1)["waves"]
    assert w.row(2, named=True)["peak_ratio"] == pytest.approx(1.5)


def test_a_day_inside_the_turn_has_no_waves_and_a_one_way_day_has_one_going():
    flat = path_minutes([(30, 100.3, 1000.0)])                                   # 0.3%: inside the 1-minute turn (0.6%)
    assert micro_boxes(flat, JP_SESSIONS, period=1, trading=True)["waves"].is_empty()
    w = micro_boxes(path_minutes([(30, 103.0, 1000.0)]), JP_SESSIONS, period=1, trading=True)["waves"]
    assert w["dir"].to_list() == ["up"] and w["open"].to_list() == [True] and w["peak_ratio"][0] is None


def test_a_bigger_minute_box_s_last_small_box_is_its_gold():
    # 新元科技's 10-day 分时 shows a 下品 at the right end: the daily 金品 on the minute boxes — each bigger
    # box's last small box, named by where it sits; an earlier small box in it is replaced
    rows, day = [], datetime(2026, 10, 2)
    for k in range(240):
        px = 100.0 + 0.05 * k if k < 20 else 101.1 + 0.5 * math.sin((k - 20) / 5.0) if k < 200 else 101.9 + 0.03 * (k - 200)
        at = day + timedelta(hours=9, minutes=k) if k < 150 else day + timedelta(hours=12, minutes=30 + k - 150)
        rows.append(("JP:7203", at, px, px + 0.02, px - 0.02, px, 1000.0))
    g = micro_boxes(pl.DataFrame(rows, schema=MINUTES, orient="row"), JP_SESSIONS, period=1)["gold"]
    assert g.height and g.schema["known_at"] == pl.Datetime and set(g["kind"]) <= {"天品", "上品", "下品"}
    now = g.filter(pl.col("superseded_at").is_null())
    assert now["big_box_id"].n_unique() == now.height < g.height                         # one per bigger box: its last
