from datetime import date

import polars as pl
import pytest

from wb.data.store import Store
from wb.devdata import write
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.run import evaluate


def test_frame_prices_are_hfq_and_raw_is_kept(paths):
    write(paths, 320)
    st = Store(paths, "JP")
    df = build_frame(st, "JP", "test", {"idx_close", "raw_close", "up_limit"})  # only what is asked for
    t = df.filter(pl.col("listing_id") == "JP:7203").sort("date")
    # the 1:5 split: raw close drops, the backward-adjusted close stays continuous
    raw_jump = (t["raw_close"] / t["raw_close"].shift(1)).min()
    hfq_jump = (t["close"] / t["close"].shift(1)).min()
    assert raw_jump < 0.3 and hfq_jump > 0.8
    assert (t["close"] / t["raw_close"]).max() > 4.9  # before the split: 5x in first-bar units
    # TSE limit prices from the price table, and the default index (test profile: N225) joined
    assert t["up_limit"].null_count() == 1 and t["idx_close"].null_count() == 0
    assert set(df["listing_id"]) == {"JP:7203", "JP:130A", "JP:6758"}


def test_formula_on_a_real_snapshot(paths):
    write(paths, 320)
    st = Store(paths, "CN")
    comp = Compiler("CN", available=available_columns(st))
    comp.add("hit", "C > MA(C, 20) AND LIMIT_UP_COUNT(60) >= 0")
    df = build_frame(st, "CN", "test", comp.program().columns)
    out = evaluate(df, comp.program())
    assert out.height == df.height and out["hit"].drop_nulls().len() > 0


def test_jp_limits_are_unknown_on_widened_band_days(paths):
    from datetime import timedelta

    from wb.datasets import conform

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(3)]
    bars = conform(pl.DataFrame({"listing_id": "JP:7203", "date": days, "open": 1000.0, "high": [1000.0, 1150.0, 1500.0],
                                 "low": [990.0, 1000.0, 1150.0], "close": [1000.0, 1150.0, 1500.0], "volume": 1.0,
                                 "adj_factor": 1.0, "share_factor": 1.0}), "bars_1d")
    st = Store(paths, "JP")
    st.publish({"bars_1d": bars}, as_of=days[-1])
    f = build_frame(st, "JP", "test", {"up_limit", "down_limit"})
    assert f["up_limit"].to_list() == [None, 1300.0, None]  # day 3 traded above the table's 1450


def test_formulas_see_traded_bars_only(paths):
    # a suspended day keeps its row in the data (no prices) but is not a bar: windows span traded
    # bars only, as in 通达信 and on the chart, so one suspension does not black out MA(C, N) for N bars
    from datetime import timedelta

    from wb.datasets import conform

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    close = [10.0, 11.0, None, 12.0, 13.0]
    bars = conform(pl.DataFrame({"listing_id": "CN:600000", "date": days, "open": close, "high": close, "low": close,
                                 "close": close, "volume": [1.0, 1.0, None, 1.0, 1.0], "adj_factor": 1.0,
                                 "suspended": [False, False, True, False, False]}), "bars_1d")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("x", "MA(C, 3)", want="value")
    out = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program())
    assert out["date"].to_list() == [days[0], days[1], days[3], days[4]]
    assert out["x"].to_list() == [None, None, 11.0, 12.0]


def test_a_row_with_bad_data_makes_what_it_affects_unknown(paths):
    # spec §4.6: a row-level data problem makes the related values unknown, with the reason kept —
    # never silently used. Day 2's OHLC is inconsistent (low above the close); day 3's amount cannot
    # be the day's turnover (VWAP far above the high)
    from datetime import timedelta

    from wb.datasets import conform

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(5)]
    close = [10.0, 11.0, 12.0, 12.5, 13.0]
    low = [9.9, 10.9, 12.2, 12.4, 12.9]
    bars = conform(pl.DataFrame({"listing_id": "CN:600000", "date": days, "open": close, "high": [c + 0.2 for c in close],
                                 "low": low, "close": close, "volume": 1000.0,
                                 "amount": [10000.0, 11000.0, 12000.0, 99000.0, 13000.0], "adj_factor": 1.0,
                                 "suspended": False}), "bars_1d")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("ma", "MA(C, 2)", want="value")
    comp.add("amt", "AMOUNT", want="value")
    f = build_frame(st, "CN", "test", comp.program().columns)
    out = evaluate(f, comp.program())
    assert out["ma"].to_list() == [None, 10.5, None, None, 12.75]       # windows over day 2 are unknown
    assert out["amt"].to_list()[3] is None and out["amt"].to_list()[2] == 12000.0
    assert f["quality"].to_list()[2] == ["ohlc"] and f["quality"].to_list()[3] == ["vwap_range"]
    assert f["quality"].to_list()[0] is None
    assert (st.derived / st.latest().id / "quality_rows.parquet").exists()  # computed once per snapshot


def test_a_bonus_issue_is_continuous_in_formulas_and_on_the_chart(paths):
    # spec §14 regression case (reuse appendix #10): 300750.SZ 宁德时代, 10 转 8 and a 2.52 dividend on
    # 2023-04-26 — raw −41.82%, really +5.40% (the exchange's pre_close). Values as stored by Tushare
    from wb.charts.series import build
    from wb.datasets import conform

    days = [date(2023, 4, 24), date(2023, 4, 25), date(2023, 4, 26), date(2023, 4, 27)]
    close, pre = [396.88, 385.9, 224.5, 231.0], [404.99, 396.88, 212.99, 224.5]
    bars = conform(pl.DataFrame({
        "listing_id": "CN:300750", "date": days, "open": close, "high": close, "low": close, "close": close,
        "pre_close": pre, "volume": [17377384.0, 16898929.0, 39707529.0, 33969078.0], "amount": None,
        "adj_factor": [1.006, 1.006, 1.822, 1.822], "share_factor": [1.0, 1.0, 1.8, 1.8], "suspended": False}),
        "bars_1d")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("adj", "C / REF(C, 1) - 1", want="value")
    comp.add("raw", "RAW_C / REF(RAW_C, 1) - 1", want="value")
    comp.add("vol", "V / REF(V, 1)", want="value")
    out = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program()).row(2, named=True)
    assert out["raw"] == pytest.approx(224.5 / 385.9 - 1) and round(out["raw"], 4) == -0.4182
    assert out["adj"] == pytest.approx(224.5 / 212.99 - 1, abs=0.001)          # +5.40% within factor rounding
    assert out["vol"] == pytest.approx(39707529 / 1.8 / 16898929)               # shares made comparable
    chart = build(bars, "CN:300750", "CN", "day", "qfq").frame                # the chart: same continuity
    assert chart["close"][2] / chart["close"][1] - 1 == pytest.approx(out["adj"])


def test_the_days_average_price_is_in_the_same_basis_as_the_close(paths):
    # VWAP = the day's amount ÷ its actual shares, adjusted like C; a day without volume has none
    from wb.datasets import conform

    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]
    bars = conform(pl.DataFrame({
        "listing_id": "CN:600000", "date": days, "open": [10.0, 10.2, 5.2], "high": [10.5, 10.9, 5.6], "low": [9.8, 10.1, 5.1],
        "close": [10.2, 10.8, 5.5], "pre_close": [10.0, 10.2, 5.4], "volume": [1e6, 2e6, 0.0],
        "amount": [1.01e7, 2.1e7, 0.0], "adj_factor": [1.0, 1.0, 2.0], "share_factor": [1.0, 1.0, 2.0], "suspended": False}), "bars_1d")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("gap", "(C - VWAP) / REF(C, 1)", want="value")
    comp.add("avg", "VWAP", want="value")
    out = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program())
    assert out["avg"].to_list()[:2] == [pytest.approx(10.1), pytest.approx(10.5)]
    assert out["gap"][1] == pytest.approx((10.8 - 10.5) / 10.2) and out["avg"][2] is None


def test_a_day_whose_amount_and_volume_disagree_has_no_average_price(paths):
    # spec §4.6: the amount implies an average outside the day's range (vwap_range) — the average is unknown that day
    from wb.datasets import conform

    days = [date(2026, 3, 2), date(2026, 3, 3)]
    bars = conform(pl.DataFrame({
        "listing_id": "CN:600000", "date": days, "open": [10.0, 10.2], "high": [10.5, 10.9], "low": [9.8, 10.1],
        "close": [10.2, 10.8], "volume": [1e6, 2e6], "amount": [1.01e7, 9.0e7], "adj_factor": 1.0, "suspended": False}), "bars_1d")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("avg", "VWAP", want="value")
    out = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program())
    assert out["avg"][0] == pytest.approx(10.1) and out["avg"][1] is None          # 45 a share on a 10.1–10.9 day


def test_a_reports_figures_are_read_from_the_first_close_that_knew_them(paths):
    # 财务 PIT (spec §4.7): FIN_ fields are the latest report period known by each day's close; a restatement of that
    # period counts from its own day; a late restatement of an older period never replaces the newer period's figures
    from wb.datasets import conform

    days = [date(2026, 4, 27), date(2026, 4, 28), date(2026, 4, 29), date(2026, 4, 30), date(2026, 5, 6)]
    bars = conform(pl.DataFrame({"listing_id": "CN:600000", "date": days, "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                                 "volume": 1e6, "amount": 1e7, "adj_factor": 1.0, "suspended": False}), "bars_1d")
    fin = conform(pl.DataFrame({
        "listing_id": "CN:600000", "period_end": [date(2025, 12, 31), date(2026, 3, 31), date(2025, 12, 31), date(2026, 3, 31)],
        "period_type": ["FY", "Q1", "FY", "Q1"], "ann_date": [date(2026, 4, 27), date(2026, 4, 28), date(2026, 4, 29), date(2026, 5, 5)],
        "known_at": [days[1], days[2], days[3], days[4]], "net_profit": [100.0, 30.0, 90.0, 31.0], "roe": [12.0, 3.0, 11.0, 3.1],
        "source": "t"}), "financials")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars, "financials": fin}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("np", "FIN_NET_PROFIT", want="value")
    comp.add("roe", "FIN_ROE", want="value")
    out = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program()).sort("date")
    assert out["np"].to_list() == [None, 100.0, 30.0, 30.0, 31.0] and out["roe"].to_list() == [None, 12.0, 3.0, 3.0, 3.1]


def test_foreign_holders_are_the_latest_list_announced_before_the_day(paths):
    # 多个外资持股 (10-17): FOREIGN_HOLDERS counts 外资 among the top-10 float holders as the
    # 资料 page groups them (陆股通 and 香港 count; a public fund, the H-share nominee and a company do not), from the trading
    # day after its announcement (a Saturday's on Monday); a late list of an older period never replaces a newer one's
    from wb.datasets import conform

    days = [date(2026, 8, 21), date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26), date(2026, 10, 12)]
    lids = ["CN:300001", "CN:600000"]
    bars = conform(pl.DataFrame({"listing_id": [lid for lid in lids for _ in days], "date": days * 2, "open": 10.0, "high": 10.5,
                                 "low": 9.5, "close": 10.0, "volume": 1e6, "amount": 1e7, "adj_factor": 1.0, "suspended": False}), "bars_1d")
    q1 = [("高盛国际-自有资金", "一般企业"), ("某集团有限公司", "一般企业")]
    h1 = [("高盛国际-自有资金", "一般企业"), ("UBSAG", "一般企业"), ("中信证券资产管理(香港)有限公司-客户资金", "其他金融产品"),
          ("MORGANSTANLEY&CO.INTERNATIONALPLC.", "一般企业"), ("香港中央结算有限公司", "一般企业"),
          ("上海浦东发展银行股份有限公司-财通资管数字经济混合型发起式证券投资基金", "其他金融产品"),
          ("香港中央结算代理人有限公司", "一般企业"), ("某集团有限公司", "一般企业"),
          ("LISHERYNZHANMING", "自然人")]   # a person spelled in Latin letters: not 外资 (10-19, 深赛格)
    late = [("某集团有限公司", "一般企业")]
    rows = ([(date(2026, 3, 31), date(2026, 4, 23), n, k) for n, k in q1] + [(date(2026, 6, 30), date(2026, 8, 22), n, k) for n, k in h1]
            + [(date(2025, 12, 31), date(2026, 8, 25), n, k) for n, k in late])
    fh = conform(pl.DataFrame({"listing_id": "CN:300001", "period_end": [r[0] for r in rows], "ann_date": [r[1] for r in rows],
                               "holder": [r[2] for r in rows], "holder_type": [r[3] for r in rows], "float_ratio": 1.0, "source": "t"}),
                 "float_holders")
    st = Store(paths, "CN")
    st.publish({"bars_1d": bars, "float_holders": fh}, as_of=days[-1])
    comp = Compiler("CN", available=available_columns(st))
    comp.add("fx", "FOREIGN_HOLDERS", want="value")
    comp.add("many", "FOREIGN_HOLDERS >= 2", want="cond")
    out = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program()).sort("listing_id", "date")
    assert out.filter(pl.col("listing_id") == "CN:300001")["fx"].to_list() == [1, 5, 5, 5, 5]
    assert out.filter(pl.col("listing_id") == "CN:600000")["fx"].to_list() == [None] * 5      # no list known: unknown, not 0
    assert out.filter(pl.col("listing_id") == "CN:300001")["many"].to_list() == [False, True, True, True, True]


def test_two_threads_reading_a_new_snapshots_quality_rows_at_once_both_get_them(paths, monkeypatch):
    # the daily scan and a page at the same moment: each writer its own temporary file
    import threading

    from wb.engine.l1.frame import quality_rows
    write(paths, 320)
    both, real, got, errors = threading.Barrier(2), pl.DataFrame.write_parquet, [], []

    def write_then_wait(self, path, *a, **k):
        real(self, path, *a, **k)
        both.wait(10)
    monkeypatch.setattr(pl.DataFrame, "write_parquet", write_then_wait)

    def run():
        try:
            got.append(quality_rows(Store(paths, "CN")).height)   # each request its own store, as in the app
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert errors == [] and len(got) == 2


def test_rules_read_level_one_of_the_industry_rows_whatever_level_two_says(paths):
    # design 2026-10-12 §10: 申万二级 rides on the same rows as two more columns; rules keep reading the 一级 name, so
    # IN_INDUSTRY and INDUSTRY_RANK give the same as before the columns were filled
    write(paths, 320)
    st = Store(paths, "CN")

    def rows(level_two: bool) -> pl.DataFrame:
        return pl.DataFrame({
            "listing_id": ["CN:600519", "CN:000001"], "scheme": ["SW2021_L1"] * 2, "code": ["801120.SI", "801780.SI"],
            "name": ["食品饮料", "银行"], "in_date": [date(2001, 8, 27), date(1991, 4, 3)], "out_date": [None, None],
            "l2_code": ["801125.SI", "801783.SI"] if level_two else [None, None],
            "l2_name": ["白酒Ⅱ", "股份制银行Ⅱ"] if level_two else [None, None], "source": ["tushare"] * 2},
            schema_overrides={"out_date": pl.Date, "l2_code": pl.Utf8, "l2_name": pl.Utf8})

    def judged():
        snap = st.latest()
        comp = Compiler("CN", available=available_columns(st, snap))
        comp.add("food", 'IN_INDUSTRY("食品饮料")')
        comp.add("rank", "INDUSTRY_RANK(5)")
        prog = comp.program()
        frame = build_frame(st, "CN", "test", prog.columns | {"industry"}, snap=snap)
        return evaluate(frame, prog).join(frame.select("listing_id", "date", "industry"), on=["listing_id", "date"]).sort("listing_id", "date")

    st.publish({"industry": rows(False)}, as_of=st.latest().as_of)
    before = judged()
    st.publish({"industry": rows(True)}, as_of=st.latest().as_of)
    after = judged()
    assert before.equals(after)
    assert set(after["industry"].drop_nulls().unique().to_list()) == {"食品饮料", "银行"}
    assert after.filter(pl.col("listing_id") == "CN:600519")["food"].all()


def beijing(paths, lid: str, listed) -> None:
    """`lid` made a 北交所 listing that listed there on `listed` (its bars before: 新三板's)."""
    store = Store(paths, "CN")
    ins = store.read("instruments")
    mine = pl.col("listing_id") == lid
    store.publish({"instruments": ins.with_columns(pl.when(mine).then(pl.lit("XBSE")).otherwise("exchange").alias("exchange"),
                                                   pl.when(mine).then(pl.lit(listed)).otherwise("list_date").alias("list_date"))},
                  as_of=store.latest().as_of)


def test_a_beijing_listings_bars_before_it_listed_there_are_not_read(paths):
    """You chose (10-13): 238 北交所 stocks carry their 新三板 years (61,553 bars from 2016, then months without one) —
    another market; rules and structures read them from their first day in Beijing."""
    write(paths, 320)
    store = Store(paths, "CN")
    lid = store.read("instruments")["listing_id"][0]
    days = sorted(store.read("bars_1d").filter(pl.col("listing_id") == lid)["date"].to_list())
    beijing(paths, lid, days[100])
    f = build_frame(store, "CN", paths.profile, {"close"}, [lid], snap=store.latest())
    assert f["date"].min() == days[100] and f.height == len(days) - 100
    whole = build_frame(store, "CN", paths.profile, {"close"}, snap=store.latest())   # the whole market too
    assert whole.filter(pl.col("listing_id") == lid)["date"].min() == days[100]
