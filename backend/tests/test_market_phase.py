"""情绪阶段 (design 2026-10-12 §5.2): each day's limit-up ladder from the stored daily bars and the phase it is in —
smoothed over about five days, a new phase taken on its second day (a crash at once), vetoed by a falling index;
and today's read against the same minute of yesterday (§5.3)."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.data.store import Store
from wb.devdata import write
from wb.market.phase import classify, ladder_days, summary, today_read

CALM = dict(first=40, ge2=10, height=5, promo_rate=0.20, seal_rate=0.70, limit_down=10, limit_up=50)
HOT = dict(first=60, ge2=25, height=9, promo_rate=0.35, seal_rate=0.75, limit_down=5, limit_up=90)
ICE = dict(first=20, ge2=5, height=3, promo_rate=0.20, seal_rate=0.60, limit_down=10, limit_up=30)


def days(*spans: tuple[dict, int], idx: dict[int, int] | None = None) -> pl.DataFrame:
    """Day rows from (metrics, how many days) spans; `idx`: the 上证 state on given row numbers (else 0)."""
    rows = [m for m, n in spans for _ in range(n)]
    start = date(2026, 1, 5)
    return pl.DataFrame([{"date": start + timedelta(days=k), **m, "idx_state": (idx or {}).get(k, 0)} for k, m in enumerate(rows)],
                        schema_overrides={"idx_state": pl.Int8})


def phases(d: pl.DataFrame) -> list[str]:
    return classify(d)["phase"].to_list()


def test_hot_days_confirm_a_new_phase_on_its_second_day():
    # calm, then hot: the smoothing walks in — 启动 shows up on the 6th day and holds from the 7th, 主升 from the 10th
    got = phases(days((CALM, 5), (HOT, 10)))
    assert got[:6] == ["平稳"] * 6
    assert got[6:9] == ["启动"] * 3
    assert got[9:] == ["主升"] * 6


def test_a_single_odd_day_does_not_switch_but_two_do():
    one = phases(days((HOT, 12), idx={6: -1}))            # the index falls one day: that day's label is 平稳
    assert set(one) == {"主升"}
    two = phases(days((HOT, 12), idx={6: -1, 7: -1}))     # two days running: the phase follows on the second
    assert two[6] == "主升" and two[7] == "平稳" and two[8] == "平稳" and two[9] == "主升"


def test_a_crash_day_switches_at_once_to_ebb_after_heat_and_to_ice_otherwise():
    crash = dict(HOT, limit_down=200, limit_up=20)
    after_heat = phases(days((HOT, 8), (crash, 1)))
    assert after_heat[7] == "主升" and after_heat[8] == "退潮"
    after_calm = phases(days((CALM, 8), (dict(CALM, limit_down=120, limit_up=20), 1)))
    assert after_calm[8] == "冰点"
    small = phases(days((HOT, 8), (dict(HOT, limit_down=49, limit_up=20), 1)))     # under 50: not a crash
    assert small[8] == "主升"


def test_a_falling_index_vetoes_hot_phases():
    assert set(phases(days((HOT, 6), idx=dict.fromkeys(range(6), -1)))) == {"平稳"}


def test_calm_after_ice_is_repair_until_another_phase():
    got = phases(days((ICE, 6), (CALM, 12), (HOT, 10)))
    assert got[0] == "冰点"
    first_repair = got.index("修复")
    first_ignite = got.index("启动")
    assert first_repair < first_ignite
    assert set(got[first_repair:first_ignite]) == {"修复"}                 # no 平稳 between: it is the repair
    assert "平稳" not in got


def test_without_limit_prices_the_phase_is_unknown():
    d = days((HOT, 5)).with_columns(pl.lit(None, pl.Int64).alias("limit_up"))
    assert classify(d)["phase"].to_list() == [None] * 5
    assert summary(classify(d)) is None


def test_summary_counts_the_days_and_names_a_pending_change():
    d = pl.DataFrame({"date": [date(2026, 10, k) for k in range(1, 8)],
                      "phase": ["平稳", "平稳", "启动", "启动", "启动", "主升", "主升"],
                      "raw": ["平稳", "启动", "启动", "启动", "主升", "主升", "退潮"],
                      "limit_up": [50] * 7, "up": [3000] * 7, "n": [5000] * 7, "high60": [100] * 7, "low60": [40] * 7})
    s = summary(d)
    assert (s["phase"], s["days"], s["pending"], s["as_of"]) == ("主升", 2, "退潮", "2026-10-07")
    assert s["path"] == [{"phase": "平稳", "days": 2}, {"phase": "启动", "days": 3}, {"phase": "主升", "days": 2}]
    assert s["recent"][-1] == {"date": "2026-10-07", "phase": "主升", "limit_up": 50, "up_ratio": 0.6, "net_high": 60}
    calm = summary(d.with_columns(pl.lit("修复").alias("phase"), pl.lit("平稳").alias("raw")))
    assert calm["pending"] is None                                          # 平稳 under 修复 is no change
    crash = summary(d.with_columns(pl.Series("raw", ["平稳"] * 6 + ["急跌"])))
    assert crash["pending"] is None                                         # a crash changes at once, never pending


NOW = dict(limit_up=38, broken=17, seal_rate=38 / 55, promo_rate=0.31, limit_down=7, top_sealed=False)
THEN = dict(limit_up=52, broken=9, seal_rate=52 / 61, promo_rate=0.42, limit_down=2, top_sealed=True)


def test_today_read_votes_beyond_the_margins_only():
    r = today_read(NOW, THEN, minute=72)
    assert r == {"label": "偏弱", "better": [], "worse": ["涨停少", "炸板多", "封板率低", "晋级率低", "跌停多", "最高板炸板"]}
    near = today_read(dict(NOW, limit_up=50, broken=10, seal_rate=0.83, promo_rate=0.40, limit_down=3, top_sealed=True), THEN, 72)
    assert near == {"label": "持平", "better": [], "worse": []}              # within 3 / 15% and 5 points
    up = today_read(THEN, NOW, 72)
    assert up["label"] == "偏强" and up["worse"] == []


def test_today_read_waits_15_minutes_and_needs_both_rows():
    assert today_read(NOW, THEN, minute=14) is None
    assert today_read(NOW, None, minute=60) is None
    assert today_read(dict(NOW, promo_rate=None), THEN, 60)["worse"] == ["涨停少", "炸板多", "封板率低", "跌停多", "最高板炸板"]


def _oracle(bars: pl.DataFrame) -> pl.DataFrame:
    """Per day from the raw bars alone: limit-ups, touches, limit-downs, yesterday's pool and its promoted, the height."""
    b = bars.filter(pl.col("close").is_not_null()).sort("listing_id", "date").with_columns(
        (pl.col("close") >= pl.col("up_limit") - 1e-6).alias("lu"), (pl.col("high") >= pl.col("up_limit") - 1e-6).alias("tc"),
        (pl.col("close") <= pl.col("down_limit") + 1e-6).alias("ld"))
    runs = []
    for (_,), g in b.group_by("listing_id", maintain_order=True):
        n = 0
        for lu in g["lu"].to_list():
            n = n + 1 if lu else 0
            runs.append(n)
    b = b.with_columns(pl.Series("run", runs), pl.col("lu").shift(1).over("listing_id").alias("prev"))
    return b.group_by("date").agg(pl.col("lu").sum().alias("limit_up"), pl.col("tc").sum().alias("touched"),
                                  pl.col("ld").sum().alias("limit_down"), pl.col("prev").sum().alias("pool"),
                                  (pl.col("prev") & pl.col("lu")).sum().alias("promoted"), pl.col("run").max().alias("height")).sort("date")


def test_ladder_days_counts_match_the_bars(paths):
    write(paths, 320)
    st = Store(paths, "CN")
    bars = st.read("bars_1d").sort("listing_id", "date")
    moutai = bars.filter(pl.col("listing_id") == "CN:600519").tail(12)
    pingan = bars.filter(pl.col("listing_id") == "CN:000001").tail(12)
    # three limit closes in a row for one, one touch that does not hold for the other
    limit = pl.concat([moutai.slice(5, 3).with_columns(pl.col("up_limit").alias("close"), pl.col("up_limit").alias("high")),
                       pingan.slice(6, 1).with_columns(pl.col("up_limit").alias("high"))])
    st.publish({"bars_1d": limit}, as_of=st.latest().as_of)
    snap = st.latest()
    got = ladder_days(st, "test", snap, start=moutai["date"][0])
    want = _oracle(st.read("bars_1d", snap).filter(pl.col("date") >= moutai["date"][0]))
    cols = ["date", "limit_up", "touched", "limit_down", "pool", "promoted", "height"]
    assert got.select(cols).cast({c: pl.Int64 for c in cols[1:]}).equals(want.select(cols).cast({c: pl.Int64 for c in cols[1:]}))
    assert got["height"].max() == 3 and got["ge2"].max() == 1
    assert got["idx_state"].dtype == pl.Int8


def test_without_limit_prices_the_ladder_is_unknown(paths):
    write(paths, 320)
    st = Store(paths, "CN")
    bars = st.read("bars_1d")
    st.publish({"bars_1d": bars.with_columns(pl.lit(None, pl.Float64).alias("up_limit"), pl.lit(None, pl.Float64).alias("down_limit"))},
               as_of=st.latest().as_of)
    got = ladder_days(st, "test", st.latest(), start=bars["date"].max() - timedelta(days=30))
    assert got.height > 0 and got["limit_up"].null_count() == got.height and got["height"].null_count() == got.height
    assert got["up"].null_count() == 0                                       # the plain counts are still there


@pytest.mark.parametrize("key", ["limit_up", "first", "ge2", "height", "pool", "promoted"])
def test_ladder_days_has_every_column(paths, key):
    write(paths, 320)
    st = Store(paths, "CN")
    got = ladder_days(st, "test", st.latest(), start=st.latest().as_of - timedelta(days=20))
    assert key in got.columns
