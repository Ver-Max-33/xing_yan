from datetime import date, timedelta

import polars as pl

from wb.data.quality import blocking, check_bars
from wb.datasets import conform

D0 = date(2026, 1, 5)


def bars(n=30, **overrides):
    base = {
        "listing_id": ["CN:600519"] * n,
        "date": [D0 + timedelta(days=i) for i in range(n)],
        "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n, "close": [10.5] * n,
        "volume": [1000.0] * n, "amount": [10200.0] * n, "adj_factor": [1.0] * n,
        "suspended": [False] * n,
    }
    base.update(overrides)
    return conform(pl.DataFrame(base), "bars_1d")


def checks(issues):
    return {i.check for i in issues}


def test_clean_data_has_no_issues():
    assert check_bars(bars()) == []


def test_duplicate_keys_block():
    df = pl.concat([bars(), bars().head(1)])
    assert "unique_key" in checks(blocking(check_bars(df)))


def test_volume_in_lots_is_a_unit_error():
    # Tushare `vol` is in lots of 100; forgetting x100 makes amount/volume 100x the price.
    assert "units" in checks(blocking(check_bars(bars(volume=[10.0] * 30))))


def test_estimated_volume_rejected():
    # Anti-example from spec §14: volume derived from amount must be rejected.
    assert "estimated_volume" in checks(blocking(check_bars(bars(amount=[10500.0] * 30))))


def test_ohlc_and_suspension_rows():
    df = bars(n=3, low=[9.0, 10.8, 9.0], suspended=[False, False, True], volume=[1000.0, 1000.0, 0.0])
    found = checks(check_bars(df))
    assert {"ohlc", "suspended_zero_volume"} <= found
    assert not blocking(check_bars(df))


def test_gap_detected_but_suspension_is_not_a_gap():
    # day 1 suspended (row present), day 2 missing entirely (fetch gap)
    df = bars(n=4, suspended=[False, True, False, False], close=[10.5, None, 10.5, 10.5],
              volume=[1000.0, None, 1000.0, 1000.0])
    df = df.filter(pl.col("date") != D0 + timedelta(days=2))
    days = [D0 + timedelta(days=i) for i in range(4)]
    issues = [i for i in check_bars(df, days) if i.check == "missing_day"]
    assert [i.date for i in issues] == [D0 + timedelta(days=2)]


def test_factor_jump_warns():
    df = bars(n=2, close=[10.0, 30.0], high=[11.0, 31.0], low=[9.0, 29.0], open=[10.0, 30.0],
              amount=[10000.0, 30000.0])
    assert "adj_jump" in checks(check_bars(df))


def test_limit_range_flags_impossible_close():
    df = bars(n=2, up_limit=[11.0, 11.0], down_limit=[9.0, 9.0], close=[10.5, 11.5], high=[11.0, 11.5])
    assert [i.date for i in check_bars(df) if i.check == "limit_range"] == [D0 + timedelta(days=1)]


def test_adj_identity_catches_missing_factor():
    # Day 2 is an ex-date: pre_close drops from 10.0 to 5.0 (1:2 bonus) but adj_factor did not change.
    days = [D0 + timedelta(days=i) for i in range(3)]
    ok = bars(n=3, close=[10.0, 5.0, 5.0], open=[10.0, 5.0, 5.0], high=[10.0, 5.0, 5.0], low=[10.0, 5.0, 5.0],
              pre_close=[10.0, 5.0, 5.0], adj_factor=[1.0, 2.0, 2.0], amount=[10000.0, 5000.0, 5000.0])
    assert not [i for i in check_bars(ok, days) if i.check == "adj_identity"]
    wrong = ok.with_columns(pl.lit(1.0).alias("adj_factor"))
    flagged = [i.date for i in check_bars(wrong, days) if i.check == "adj_identity"]
    assert flagged == [D0 + timedelta(days=1)]


def test_index_coverage():
    from wb.data.quality import check_index_bars

    days = [D0 + timedelta(days=i) for i in range(3)]
    df = pl.DataFrame({"index_id": ["CN:IDX:000300", "CN:IDX:000001", "CN:IDX:000001", "CN:IDX:000001"],
                       "date": [D0, D0, days[1], days[2]]})
    missing = [(i.listing_id, i.date) for i in check_index_bars(df, days)]
    assert missing == [("CN:IDX:000300", days[1]), ("CN:IDX:000300", days[2])]


def test_index_not_expected_before_it_exists():
    # 科创50 has data from its base date 2019-12-31 on; a batch starting earlier must not flag
    # the days before it existed, but a later gap still counts.
    from wb.data.quality import check_index_bars

    days = [D0 + timedelta(days=i) for i in range(4)]
    df = pl.DataFrame({"index_id": ["CN:IDX:000300"] * 4 + ["CN:IDX:000688"] * 2,
                       "date": days + [days[1], days[3]]})
    missing = [(i.listing_id, i.date) for i in check_index_bars(df, days, since={"CN:IDX:000688": days[1]})]
    assert missing == [("CN:IDX:000688", days[2])]


def test_tushare_declares_index_base_dates():
    from datetime import date

    from wb.data.providers.tushare import Tushare

    since = Tushare.index_since
    assert since["CN:IDX:000688"] == date(2019, 12, 31) and since["CN:IDX:899050"] == date(2022, 4, 29)


def test_vwap_allows_amount_rounding_on_tiny_trades():
    # NEEQ/BSE odd lots: 1 share traded at 9.00 can be published as amount 8 (rounded to 1 yuan),
    # which is not an error; a real mismatch on a normal-sized trade is still flagged.
    n = 60
    vol, amt = [1000.0] * n, [10200.0] * n
    vol[5], amt[5] = 1.0, 8.0        # vwap 8.0 < low 9.0 only because of rounding
    vol[7], amt[7] = 1000.0, 5000.0  # vwap 5.0: far outside [9, 11]
    flagged = [i.date for i in check_bars(bars(n, volume=vol, amount=amt)) if i.check == "vwap_range"]
    assert flagged == [D0 + timedelta(days=7)]


def test_a_day_where_most_rows_are_suspended_blocks_publishing():
    # null close/volume on a half-published day reads as "suspended": every other check would then
    # see nothing to check, so the day itself must be stopped
    n = 40
    rows = {"listing_id": [f"JP:{1000 + i}" for i in range(n)], "date": [D0] * n,
            "open": [None] * 30 + [10.0] * 10, "high": [None] * 30 + [11.0] * 10,
            "low": [None] * 30 + [9.0] * 10, "close": [None] * 30 + [10.5] * 10,
            "volume": [None] * 30 + [1000.0] * 10, "amount": [None] * n, "adj_factor": [1.0] * n,
            "suspended": [True] * 30 + [False] * 10}
    df = conform(pl.DataFrame(rows, schema_overrides={"open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                                                      "close": pl.Float64, "volume": pl.Float64, "amount": pl.Float64}),
                 "bars_1d")
    assert "mass_suspension" in {i.check for i in blocking(check_bars(df, [D0]))}


def halt_days(shares, n=100):
    """n listings on consecutive days; on day k the first round(shares[k] · n) are suspended (no prices). The volume
    differs from day to day: a market repeating yesterday exactly is a re-dated day (day_repeat), never a real one."""
    rows = []
    for k, share in enumerate(shares):
        for i in range(n):
            off = i < round(share * n)
            rows.append({"listing_id": f"CN:{600000 + i}", "date": D0 + timedelta(days=k), "open": None if off else 10.0,
                         "high": None if off else 11.0, "low": None if off else 9.0, "close": None if off else 10.5,
                         "volume": None if off else 1000.0 + k, "amount": None, "adj_factor": 1.0, "suspended": off})
    num = {c: pl.Float64 for c in ("open", "high", "low", "close", "volume", "amount")}
    return conform(pl.DataFrame(rows, schema_overrides=num), "bars_1d")


def test_a_market_wide_halt_that_builds_up_over_days_is_not_a_broken_day():
    # 2015-07-06…07-10 (A 股): suspensions rose from 562 to 1,438 of 2,801 (51%) and fell back over days — a real
    # halt, so it is published with a warning; a day that jumps to mass "suspension" from a normal one still blocks
    days = [D0 + timedelta(days=k) for k in range(6)]
    wave = check_bars(halt_days([0.20, 0.27, 0.47, 0.51, 0.49, 0.37]), days)
    assert not blocking(wave) and any(i.check == "mass_suspension" and i.severity == "warn" for i in wave)
    lone = check_bars(halt_days([0.03, 0.03, 0.75, 0.03]), days[:4])
    assert "mass_suspension" in {i.check for i in blocking(lone)}


def test_a_whole_day_halt_the_exchange_announced_is_published_with_a_warning():
    # 2020-10-01: the Tokyo Stock Exchange's system failed and nothing traded all day (all 4,047 J-Quants rows
    # without prices). It jumps there from an ordinary day, so only the market's list of such days tells it apart
    # from a half-published one; a day not on that list still blocks
    days = [D0 + timedelta(days=k) for k in range(4)]
    df = halt_days([0.03, 0.03, 1.0, 0.03])
    halted = check_bars(df, days, halts={days[2]})
    assert not blocking(halted)
    assert [(i.severity, i.date) for i in halted if i.check == "mass_suspension"] == [("warn", days[2])]
    assert "mass_suspension" in {i.check for i in blocking(check_bars(df, days, halts={days[1]}))}


def test_the_tokyo_market_lists_its_whole_day_halt():
    from wb.markets import load_market
    assert date(2020, 10, 1) in load_market("JP").halts and load_market("CN").halts == ()


# ---- 分钟线和日线对账: the day's kept minutes (a source with each minute's turnover) against the exchange's bar ----
def day_minutes(listing="CN:600000", day=D0, closes=(10.0, 10.2, 10.5), volume=1000.0, amount=None, last=(14, 59)):
    """Three minutes ending on the session's last minute (14:59, the 15:00 bar's), with their turnover."""
    from datetime import datetime
    times = [datetime(day.year, day.month, day.day, 9, 30), datetime(day.year, day.month, day.day, 9, 31),
             datetime(day.year, day.month, day.day, *last)]
    return pl.DataFrame({"listing_id": listing, "at": times, "open": [10.0, 10.0, 10.2], "high": [10.1, 10.3, 11.0],
                         "low": [9.0, 9.9, 10.2], "close": list(closes), "volume": volume,
                         "amount": amount if amount is not None else [c * volume for c in closes]})


def day_bar(day=D0, **over):
    row = {"listing_id": "CN:600000", "date": day, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
           "volume": 3000.0, "amount": 30700.0, **over}
    return pl.DataFrame([row])


def test_a_days_minutes_that_add_up_to_its_bar_say_nothing():
    from datetime import time
    from wb.data.quality import check_intraday
    assert check_intraday(day_minutes(), day_bar(), time(15, 0)) == []


def test_minutes_that_do_not_add_up_to_the_days_bar_are_named_with_what_differs():
    from datetime import time
    from wb.data.quality import check_intraday
    issues = check_intraday(day_minutes(closes=(10.0, 10.2, 10.4), volume=1100.0), day_bar(), time(15, 0))
    assert [(i.check, i.severity, i.listing_id, i.date) for i in issues] == [("intraday", "warn", "CN:600000", D0)]
    assert "收盘 10.4 / 10.5" in issues[0].detail and "成交量 +10.0%" in issues[0].detail


def test_a_day_the_minutes_do_not_reach_the_close_or_carry_no_turnover_is_not_judged():
    from datetime import time
    from wb.data.quality import check_intraday
    early = day_minutes(volume=500.0, last=(13, 30))                         # the app closed before the close
    assert check_intraday(early, day_bar(), time(15, 0)) == []
    approx = day_minutes(volume=500.0).drop("amount")                         # a source without each minute's turnover
    assert check_intraday(approx, day_bar(), time(15, 0)) == []


def test_a_quiet_stocks_lot_rounded_volume_is_within_the_allowance():
    # TickFlow counts lots: a day's minutes add up to the exchange's shares rounded to a lot (≤ 50 off, measured on 20
    # days of 300 stocks) — for a stock trading 20,000 shares that is 0.25%, no discrepancy; 200 off is one
    from datetime import time
    from wb.data.quality import check_intraday
    bar = day_bar(volume=20_000.0, amount=205_000.0)
    near = day_minutes(volume=20_050.0 / 3, amount=[205_000.0 / 3] * 3)
    assert check_intraday(near, bar, time(15, 0)) == []
    off = day_minutes(volume=20_200.0 / 3, amount=[205_000.0 / 3] * 3)
    assert [i.check for i in check_intraday(off, bar, time(15, 0))] == ["intraday"]


def test_a_recent_day_of_money_flow_that_disagrees_with_its_bars_is_refused_and_history_only_warned():
    from wb.data.quality import check_moneyflow
    d_old, d_new = date(2017, 7, 25), date(2026, 10, 14)
    ids = [f"CN:6000{i:02d}" for i in range(10)]

    def flow(d, buy_scale):
        return pl.DataFrame({"listing_id": ids, "date": [d] * 10, **{f"buy_{k}": [25.0 * buy_scale] * 10 for k in ("sm", "md", "lg", "elg")},
                             **{f"sell_{k}": [25.0] * 10 for k in ("sm", "md", "lg", "elg")}})
    bars = pl.DataFrame({"listing_id": ids * 2, "date": [d_old] * 10 + [d_new] * 10, "amount": [100.0] * 20})
    bad = pl.concat([flow(d_old, 0.5), flow(d_new, 0.5)])
    issues = check_moneyflow(bad, bars, recent_from=date(2026, 10, 4))
    assert [(i.date, i.severity) for i in sorted(issues, key=lambda i: i.date)] == [(d_old, "warn"), (d_new, "block")]
    assert check_moneyflow(pl.concat([flow(d_new, 1.0)]), bars, recent_from=date(2026, 10, 4)) == []
    bj = flow(d_new, 0.5).with_columns(pl.lit("CN:830799").alias("listing_id")).head(1)
    assert check_moneyflow(pl.concat([flow(d_new, 1.0), bj]), bars, recent_from=date(2026, 10, 4)) == []   # 北交所 not judged
