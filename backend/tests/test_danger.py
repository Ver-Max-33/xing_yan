"""危险提醒 (design 2026-10-15): the definitions on made-up cross-sections and series, the kept history on a small store."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb import danger as D

DAY = date(2026, 3, 2)


def cross(n, **cols):
    """One day's cross-section of n stocks; each feature defaults to rising with the stock's number."""
    base = {"listing_id": [f"CN:6{i:05d}" for i in range(n)], "date": [DAY] * n}
    for k in ("r120", "ma20", "to5rel", "r20", "mfs20"):
        base[k] = cols.get(k, [float(i) for i in range(n)])
    return pl.DataFrame(base)


def flagged(df, flag):
    return sorted(D.flags(df).filter(pl.col(flag))["listing_id"].to_list())


def test_strong_needs_all_three_in_the_top_tenth():
    assert flagged(cross(10), "strong") == ["CN:600008", "CN:600009"]          # ranks 0.9 and 1.0
    low = [float(i) for i in range(10)]
    low[9] = -1.0                                                               # the top stock's turnover ratio now the lowest
    assert flagged(cross(10, to5rel=low), "strong") == ["CN:600008"]


def test_weak_is_a_low_120_day_stock_spiking_on_volume():
    df = cross(10, r120=[float(9 - i) for i in range(10)])                      # the top r20 / volume stocks are the weakest
    assert flagged(df, "weak") == ["CN:600008", "CN:600009"] and flagged(df, "strong") == []


def test_retail_is_the_flow_the_20_day_move_does_not_explain():
    mfs = [float(i) for i in range(19)]
    mfs[5] = 100.0                                                              # stock 5 bought far more than its move explains
    assert flagged(cross(19, mfs20=mfs), "retail") == ["CN:600005"]
    out = D.flags(cross(19, mfs20=[None] * 3 + mfs[3:]))
    assert out.filter(pl.col("listing_id") == "CN:600000")["retail"].to_list() == [None]   # unknown, not False


def test_a_run_counts_once_until_20_bars_pass_without_it():
    s = pl.DataFrame({"listing_id": ["CN:600000"] * 23, "date": [DAY + timedelta(days=i) for i in range(23)],
                      "strong": [True, True] + [False] * 20 + [True]})
    assert D._first(s, "strong")["strong1"].to_list() == [True] + [False] * 21 + [True]
    s2 = s.with_columns(pl.Series("strong", [True] + [False] * 19 + [True, False, False]))
    assert D._first(s2, "strong")["strong1"].to_list()[20] is False                # the 20 bars before held one


def series(n=130, start=date(2025, 6, 2)):
    """Open days Mon–Fri from `start`, numbered."""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return pl.DataFrame({"date": days}).with_row_index("td").with_columns(pl.col("td").cast(pl.Int64))


def test_limit_up_closes_count_their_boards_and_wait_60_days_after_listing():
    days = series(70)
    lid, n = "CN:600100", 70
    up = [10.0] * n
    close = [9.0] * 60 + [10.0 - 0.004, 9.994, 9.5] + [9.0] * 7               # day 60 within half a fen; 61 six thousandths under
    close[64], close[65] = 10.0, 10.0                                            # two in a row
    bars = pl.DataFrame({"listing_id": [lid] * n, "date": days["date"], "close": close, "raw_close": close, "up_limit": up,
                         "amount": [1e8] * n, "turnover_rate": [1.0] * n, "circ_mv": [1e9] * n})
    stocks = pl.DataFrame({"listing_id": [lid], "list_date": [days["date"][0]], "td0": [0]}, schema_overrides={"td0": pl.Int64})
    no_flow = pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, **{c: pl.Float64 for c in D.FLOW_COLS}})
    ev = D.events(D.flags(D.features(bars, no_flow, stocks, days))).filter(pl.col("kind") == "limit_up")
    assert ev.select("date", "boards").rows() == [(days["date"][60], 1), (days["date"][64], 1), (days["date"][65], 2)]   # ages 60, 64, 65
    young = stocks.with_columns(pl.lit(20).cast(pl.Int64).alias("td0"))        # ages 40, 44, 45 at the three closes: all too young
    assert D.events(D.flags(D.features(bars, no_flow, young, days))).filter(pl.col("kind") == "limit_up").height == 0


def test_money_flow_needs_20_sound_days_and_a_broken_row_is_unknown():
    days = series(25)
    lid = "CN:600200"
    bars = pl.DataFrame({"listing_id": [lid] * 25, "date": days["date"], "close": [10.0] * 25, "raw_close": [10.0] * 25,
                         "up_limit": [11.0] * 25, "amount": [400.0] * 25, "turnover_rate": [1.0] * 25, "circ_mv": [1e6] * 25})
    flow = pl.DataFrame({"listing_id": [lid] * 25, "date": days["date"], "buy_sm": [110.0] * 25, "sell_sm": [100.0] * 25,
                         **{f"{s}_{k}": [100.0] * 25 for s in ("buy", "sell") for k in ("md", "lg", "elg")}})
    flow = flow.with_columns(pl.when(pl.int_range(pl.len()) == 22).then(1e6).otherwise(pl.col("buy_lg")).alias("buy_lg"))   # day 22 broken
    stocks = pl.DataFrame({"listing_id": [lid], "list_date": [date(2020, 1, 2)], "td0": [-10_000]}, schema_overrides={"td0": pl.Int64})
    f = D.features(bars, flow, stocks, days)
    m = f["mfs20"].to_list()
    assert m[18] is None and m[19] == pytest.approx(20 * 10 / 1e6 * 1e4)       # 20 days of +10 yuan on a 1e6 float, in bp
    assert m[22] is None and m[24] is None                                      # a broken day leaves the next 20 bars unknown


def store_with(paths, n=140):
    """A small 沪深 store: two stocks and one 北交所 one, n open days, flat prices except where a test changes them."""
    from wb.data.store import Store
    days = series(n, start=date(2025, 1, 2))["date"].to_list()
    st = Store(paths, "CN")
    ins = pl.DataFrame({"listing_id": ["CN:600001", "CN:000002", "CN:830003"], "market": "CN", "code": ["600001", "000002", "830003"],
                        "exchange": ["XSHG", "XSHE", "XBSE"], "status": "L", "list_date": [date(2020, 1, 2)] * 3, "source": "t"})
    rows = [(lid, d) for lid in ins["listing_id"] for d in days]
    bars = pl.DataFrame({"listing_id": [r[0] for r in rows], "date": [r[1] for r in rows], "open": 10.0, "high": 10.0, "low": 10.0,
                         "close": 10.0, "volume": 1e6, "amount": 1e7, "adj_factor": 1.0, "up_limit": 11.0, "down_limit": 9.0,
                         "suspended": False, "source": "t"})
    val = bars.select("listing_id", "date", pl.lit(1.0).alias("turnover_rate"), pl.lit(1e9).alias("circ_mv"), pl.lit("t").alias("source"))
    cal = pl.DataFrame({"date": days, "is_open": True, "source": "t"})
    st.publish({"calendar": cal, "instruments": ins, "bars_1d": bars, "valuation": val}, as_of=days[-1])
    return st, days


def test_inputs_read_adjusted_closes_and_raw_limits(paths):
    st, days = store_with(paths)
    after = pl.col("date") >= days[100]                                          # a 10送10: prices and limits halve, the factor doubles
    b = st.read("bars_1d").with_columns(*[pl.when(after).then(5.0).otherwise(pl.col(c)).alias(c) for c in ("open", "high", "low", "close")],
                                        pl.when(after).then(2.0).otherwise(1.0).alias("adj_factor"),
                                        pl.when(after).then(5.5).otherwise(11.0).alias("up_limit"),
                                        pl.when(after).then(4.5).otherwise(9.0).alias("down_limit"))
    st.publish({"bars_1d": b}, as_of=days[-1])
    bars, flow, stocks, open_ = D._inputs(st, st.latest())
    one = bars.filter((pl.col("listing_id") == "CN:600001") & (pl.col("date") == days[100])).row(0, named=True)
    assert one["close"] == 10.0 and one["raw_close"] == 5.0 and one["up_limit"] == 5.5   # 后复权 close, raw limit
    assert "CN:830003" not in bars["listing_id"].to_list()                      # 北交所 out of the ranks


def test_history_is_computed_once_and_kept_with_the_snapshot(paths):
    st, days = store_with(paths)
    hit = (pl.col("listing_id") == "CN:600001") & (pl.col("date") == days[130])   # a bar closing at its limit up, as high
    b = st.read("bars_1d").with_columns(*[pl.when(hit).then(11.0).otherwise(pl.col(c)).alias(c) for c in ("high", "close")])
    st.publish({"bars_1d": b}, as_of=days[-1])
    ev = D.history(st)
    assert ev.filter(pl.col("kind") == "limit_up").select("listing_id", "date", "boards").rows() == [("CN:600001", days[130], 1)]
    kept = st.derived / st.latest().id / f"danger-v{D.VERSION}.parquet"
    assert kept.exists() and pl.read_parquet(kept).equals(ev)


def unl(rows):
    return pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "float_date": pl.Date, "holder": pl.Utf8, "share_type": pl.Utf8,
                                      "ann_date": pl.Date, "float_share": pl.Float64, "float_ratio": pl.Float64, "source": pl.Utf8},
                        orient="row")


def test_an_unlock_shows_from_its_20th_open_day_before_through_its_day():
    days = series(80, start=date(2026, 1, 5))
    stocks = pl.DataFrame({"listing_id": ["CN:600010"], "list_date": [date(2020, 1, 2)], "td0": [-10_000]}, schema_overrides={"td0": pl.Int64})
    u = unl([("CN:600010", days["date"][50], "甲", "定增股份", days["date"][0], 1e6, 3.0, "t"),
             ("CN:600010", days["date"][50], "乙", "定增股份", days["date"][0], 1e6, 3.0, "t")])   # 3% + 3%: over 5%
    at = lambda k: D.unlock_windows(u, stocks, days, days["date"][k])   # noqa: E731
    assert at(29).height == 0 and at(30)["left"].to_list() == [20] and at(50)["left"].to_list() == [0] and at(51).height == 0
    assert at(30)["kind"].to_list() == ["placement"] and at(30)["ratio"].to_list() == [6.0]   # 定增 named before an old stock's
    small = unl([("CN:600010", days["date"][50], "甲", "定增股份", days["date"][0], 1e6, 4.0, "t")])
    assert D.unlock_windows(small, stocks, days, days["date"][40]).height == 0


def test_an_unlock_announced_inside_the_window_is_new_that_day(paths):
    st, days = store_with(paths)
    u = unl([("CN:600001", days[120], "甲", "首发原始股", days[110], 1e7, 8.0, "t")])
    st.publish({"unlocks": u}, as_of=days[-1])
    before = D.active(st, ["CN:600001"], days[109])["warnings"]
    on = D.active(st, ["CN:600001"], days[110])["warnings"]["CN:600001"]
    after = D.active(st, ["CN:600001"], days[111])["warnings"]["CN:600001"]
    assert "CN:600001" not in before                                            # never before its announcement
    assert [(w["kind"], w["new"], w["days"]) for w in on] == [("unlock", True, 10)]
    assert after[0]["new"] is False


def test_a_one_year_ipo_unlock_is_told_from_the_listing_day_until_announced():
    days = series(400, start=date(2025, 1, 6))
    listing = date(2025, 3, 8)                                                  # + 365 = 2026-03-08, a Sunday → Monday 03-09
    stocks = pl.DataFrame({"listing_id": ["CN:688001"], "list_date": [listing], "td0": [44]}, schema_overrides={"td0": pl.Int64})
    monday = date(2026, 3, 9)
    k = days["date"].to_list().index(monday)
    w = D.unlock_windows(unl([]), stocks, days, days["date"][k - 20])
    assert w.select("unlock_day", "left", "predicted", "kind").rows() == [(monday, 20, True, "ipo")]
    assert D.unlock_windows(unl([]), stocks, days, days["date"][k - 21]).height == 0
    assert D.unlock_windows(unl([]), stocks, days, monday + timedelta(days=7)).height == 1          # said until 7 days past
    assert D.unlock_windows(unl([]), stocks, days, monday + timedelta(days=8)).height == 0
    told = unl([("CN:688001", date(2026, 3, 9), "某创投", "首发原始股", days["date"][k - 15], 1e7, 3.0, "t")])
    assert D.unlock_windows(told, stocks, days, days["date"][k - 10]).height == 0   # announced: 3% is under 5%, nothing to say


def test_a_warning_ages_by_open_days_while_the_stock_is_halted(paths):
    st, days = store_with(paths)
    hit = (pl.col("listing_id") == "CN:600001") & (pl.col("date") == days[100])
    b = st.read("bars_1d").with_columns(*[pl.when(hit).then(11.0).otherwise(pl.col(c)).alias(c) for c in ("high", "close")])
    b = b.filter(~((pl.col("listing_id") == "CN:600001") & pl.col("date").is_between(days[101], days[115])))   # halted 15 days
    st.publish({"bars_1d": b}, as_of=days[-1])
    shown = lambda k: [w["kind"] for w in D.active(st, ["CN:600001"], days[k])["warnings"].get("CN:600001", [])]   # noqa: E731
    assert shown(100) == ["limit_up"] and shown(119) == ["limit_up"] and shown(120) == []   # 20 open days, halt or not


def test_retail_is_unknown_when_the_last_days_flow_is_missing(paths):
    st, days = store_with(paths)
    ids = ["CN:600001", "CN:000002"]
    flow = pl.DataFrame({"listing_id": [i for i in ids for _ in days], "date": days * 2, "source": "t",
                         **{f"{s}_{k}": 2.5e6 for s in ("buy", "sell") for k in ("sm", "md", "lg", "elg")}})
    flow = flow.filter(~((pl.col("listing_id") == "CN:600001") & (pl.col("date") == days[-1])))   # its flow not in yet
    st.publish({"moneyflow": flow}, as_of=days[-1])
    res = D.active(st, ids)
    assert res["unknown"] == {"CN:600001": ["retail"]}


def test_other_markets_and_beijing_are_not_covered(paths):
    st, days = store_with(paths)
    res = D.active(st, ["CN:830003", "JP:7203", "CN:600001"])
    assert res["uncovered"] == {"CN:830003": D.NOT_TESTED, "JP:7203": D.NOT_TESTED}


def test_unlocks_from_is_the_first_stored_unlock_day(paths):
    st, days = store_with(paths)
    assert D.active(st, ["CN:600001"])["unlocks_from"] is None
    st.publish({"unlocks": unl([("CN:000002", days[90], "甲", "定增股份", days[80], 1e6, 1.0, "t")])}, as_of=days[-1])
    assert D.active(st, ["CN:600001"])["unlocks_from"] == days[90].isoformat()


def test_a_warning_says_why_and_what_may_follow_in_plain_words():
    # 10-17: each warning says its reason and the risk in plain words
    from wb import danger_words as W
    v = {"r120": 0.85, "ma20": 0.18, "to5rel": 2.6, "r20": 0.3, "mfs20": 120.0}
    lu = W.event("limit_up", DAY, 1, 0, v)
    assert lu["label"] == "涨停后易回落" and lu["reason"] == "03-02 收盘涨停。" and lu["new"]
    assert "之后约三个月" in lu["risk"] and "少涨约 4%" in lu["risk"] and "约两成少涨 20% 以上" in lu["risk"]
    three = W.event("limit_up", DAY, 3, 1, v)
    assert three["label"] == "连板后易回落" and "已连续 3 天" in three["reason"] and "少涨 7–10%" in three["risk"]
    assert W.label("limit_up", 1) == "涨停后易回落" and W.label("limit_up", 4) == "连板后易回落"
    sb = W.event("strong_burst", DAY, None, 2, v)
    assert sb["label"] == "高位放量易回落"
    assert "近半年涨了 85%" in sb["reason"] and "高出 20 日均线 18%" in sb["reason"] and "2.6 倍" in sb["reason"]
    ws = W.event("weak_spike", DAY, None, 2, {**v, "r120": -0.12, "r20": 0.35, "to5rel": 3.1})
    assert "近半年跌了 12%" in ws["reason"] and "近 20 天却涨了 35%" in ws["reason"] and "3.1 倍" in ws["reason"]
    assert "近半年只涨了 3%" in W.event("weak_spike", DAY, None, 2, {**v, "r120": 0.03})["reason"]
    rt = W.event("retail", DAY, None, 2, {**v, "r20": -0.05, "mfs20": 670.0})
    assert "流通市值的 6.7%" in rt["reason"] and "同期股价跌了 5%" in rt["reason"] and "从中单、大单手里买来的" in rt["reason"]
    assert "同期股价涨了 3%" in W.event("retail", DAY, None, 2, {**v, "r20": 0.03})["reason"]
    assert "流通市值的 0.45%" in W.event("retail", DAY, None, 2, {**v, "mfs20": 45.0})["reason"]
    un = W.unlock({"kind": "ipo", "unlock_day": DAY, "left": 7, "ratio": 8.5, "predicted": False, "types": "首发原始股"}, False)
    assert un["label"] == "解禁前易走弱" and "03-02 有占总股本 8.5% 的首发原始股解除限售（还有 7 个交易日）" in un["reason"]
    assert "少涨 5–7%" in un["risk"] and "可以上市交易" in un["risk"]
    pred = W.unlock({"kind": "ipo", "unlock_day": DAY, "left": 0, "ratio": None, "predicted": True, "types": ""}, True)
    assert "按推算是今天" in pred["reason"] and "还没公告" in pred["reason"]   # a reckoned day is not a known one
    gone = W.event("strong_burst", DAY, None, 2, {k: None for k in v})            # no numbers kept: still a reason
    assert gone["reason"].startswith("03-02") and "最高的一成" in gone["reason"]
    every = [lu, three, sb, ws, rt, un, pred, gone]
    words = [w[k] for w in every for k in ("label", "reason", "risk", "brief")]
    assert all(words) and not any(x in t for t in words for x in ("卖出", "止损", "建议", "加仓", "减仓", "清仓"))   # history, not advice
    assert not any(x in t for t in words for x in ("中位", "同市值", "首板", "两段", "2016–2020"))   # none of the words you found unclear


def test_an_event_keeps_the_numbers_its_reason_quotes():
    days = series(70)
    lid, n = "CN:600100", 70
    close = [9.0] * 60 + [10.0] + [9.0] * 9
    bars = pl.DataFrame({"listing_id": [lid] * n, "date": days["date"], "close": close, "raw_close": close, "up_limit": [10.0] * n,
                         "amount": [1e8] * n, "turnover_rate": [1.0] * n, "circ_mv": [1e9] * n})
    stocks = pl.DataFrame({"listing_id": [lid], "list_date": [days["date"][0]], "td0": [0]}, schema_overrides={"td0": pl.Int64})
    no_flow = pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, **{c: pl.Float64 for c in D.FLOW_COLS}})
    one = D.events(D.flags(D.features(bars, no_flow, stocks, days))).row(0, named=True)
    assert one["date"] == days["date"][60] and one["r20"] == pytest.approx(10.0 / 9.0 - 1) and one["mfs20"] is None
    assert set(D.EVENT_SCHEMA) == set(D.events(D.flags(D.features(bars, no_flow, stocks, days))).columns)


def test_a_warning_quotes_the_numbers_of_its_day(paths):
    st, days = store_with(paths)
    ev = pl.DataFrame({"listing_id": ["CN:600001"], "date": [days[-2]], "kind": ["strong_burst"], "boards": [None], "r20": [0.3],
                       "r120": [0.85], "ma20": [0.18], "to5rel": [2.6], "mfs20": [None]}, schema=D.EVENT_SCHEMA)
    kept = st.derived / st.latest().id / f"danger-v{D.VERSION}.parquet"
    kept.parent.mkdir(parents=True, exist_ok=True)
    ev.write_parquet(kept)
    w = D.active(st, ["CN:600001"])["warnings"]["CN:600001"][0]
    assert w["label"] == "高位放量易回落" and "近半年涨了 85%" in w["reason"] and w["days"] == 1


def test_a_stock_without_a_known_listing_day_is_covered_and_waits_only_for_its_limit_up(paths):
    st, days = store_with(paths)
    ins = st.read("instruments").with_columns(pl.when(pl.col("listing_id") == "CN:000002").then(None).otherwise(pl.col("list_date"))
                                              .alias("list_date"))
    hit = (pl.col("listing_id") == "CN:000002") & (pl.col("date") == days[130])
    b = st.read("bars_1d").with_columns(*[pl.when(hit).then(11.0).otherwise(pl.col(c)).alias(c) for c in ("high", "close")])
    st.publish({"instruments": ins, "bars_1d": b}, as_of=days[-1])
    res = D.active(st, ["CN:000002"], days[130])
    assert res["uncovered"] == {} and res["warnings"] == {}                    # covered; its age unknown: no limit-up said


def test_the_kept_history_is_the_last_two_years_and_says_from_when(paths):
    st, days = store_with(paths, n=700)                                         # about two years and nine months of open days
    for k in (50, 650):                                                         # two limit-up closes: one too old, one recent
        hit = (pl.col("listing_id") == "CN:600001") & (pl.col("date") == days[k])
        st.publish({"bars_1d": st.read("bars_1d").with_columns(*[pl.when(hit).then(11.0).otherwise(pl.col(c)).alias(c) for c in ("high", "close")])},
                   as_of=days[-1])
    ev = D.history(st)
    assert ev.filter(pl.col("kind") == "limit_up")["date"].to_list() == [days[650]]   # the whole ten years peaked at 6–8 GB
    since = D.history_from(st.latest())
    assert since == days[-1] - timedelta(days=D.HISTORY_DAYS) and D.active(st, ["CN:600001"])["history_from"] == since.isoformat()


def test_a_run_right_after_unknown_days_is_not_new():
    s = pl.DataFrame({"listing_id": ["CN:600000"] * 25, "date": [DAY + timedelta(days=i) for i in range(25)],
                      "retail": [False, None, None, True] + [False] * 20 + [True]}, schema_overrides={"retail": pl.Boolean})
    assert D._first(s, "retail")["retail1"].to_list() == [False] * 24 + [True]   # a gap may have held it; 20 known days later: new
    young = pl.DataFrame({"listing_id": ["CN:600000"] * 3, "date": [DAY + timedelta(days=i) for i in range(3)],
                          "retail": [None, None, True]}, schema_overrides={"retail": pl.Boolean})
    assert D._first(young, "retail")["retail1"].to_list() == [False, False, True]   # no history yet is no gap: its first day counts


def test_concurrent_first_requests_compute_the_history_once(paths, monkeypatch):
    import threading
    import time as _time
    st, days = store_with(paths)
    calls, real = [], D._inputs

    def slow(store, snap):
        calls.append(1)
        _time.sleep(0.3)                                                        # two requests overlap while it computes
        return real(store, snap)
    monkeypatch.setattr(D, "_inputs", slow)
    out = []
    from wb.data.store import Store
    ts = [threading.Thread(target=lambda: out.append(D.history(Store(paths, "CN")))) for _ in range(2)]   # a store per request
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and len(out) == 2 and out[0].equals(out[1])           # one computation (2.7 GB on real data), not two


def test_retail_is_unknown_without_any_money_flow(paths):
    st, days = store_with(paths)                                                # a profile or plan without moneyflow at all
    assert D.active(st, ["CN:600001", "CN:830003"])["unknown"] == {"CN:600001": ["retail"]}   # unknown, not silently nothing
