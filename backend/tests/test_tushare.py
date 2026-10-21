from datetime import date

import httpx
import polars as pl
import pytest

from tests.fakes import tushare_server
from wb.data.http import HttpClient
from wb.data.providers.tushare import PermissionDenied, Tushare
from wb.datasets import conform

DAY = date(2026, 9, 30)
DAILY = [
    {"ts_code": "600519.SH", "trade_date": "20260930", "open": 1500.0, "high": 1520.0, "low": 1490.0,
     "close": 1510.0, "vol": 20000.0, "amount": 3020000.0},
    {"ts_code": "830799.BJ", "trade_date": "20260930", "open": 10.0, "high": 10.5, "low": 9.9,
     "close": 10.2, "vol": 500.0, "amount": 510.0},
]


def provider(tables, denied=frozenset(), calls=None, http=None, columns=None):
    http = http or HttpClient(10_000, transport=tushare_server(tables, denied, calls, columns), sleep=lambda s: None)
    return Tushare(token="t", http=http, sleep=lambda s: None)


# the fields fina_indicator_vip has (checked 2026-10-08): no f_ann_date, unlike the three statements
FINA = {"fina_indicator_vip": {"ts_code", "ann_date", "end_date", "update_flag", "roe", "netprofit_yoy", "or_yoy"}}


def test_bars_full_tier_units_and_joins():
    tables = {
        "daily": DAILY,
        "adj_factor": [{"ts_code": "600519.SH", "adj_factor": 8.5}],
        "stk_limit": [{"ts_code": "600519.SH", "up_limit": 1661.0, "down_limit": 1359.0}],
        "suspend_d": [{"ts_code": "000001.SZ", "suspend_type": "S"}],
    }
    out = conform(provider(tables).fetch("bars_1d", DAY.isoformat(), DAY, DAY)["bars_1d"], "bars_1d").sort("listing_id")
    moutai = out.row(by_predicate=pl.col("listing_id") == "CN:600519", named=True)
    assert moutai["volume"] == 2_000_000 and moutai["amount"] == 3_020_000_000  # lots / thousand yuan converted
    assert moutai["adj_factor"] == 8.5 and moutai["up_limit"] == 1661.0 and moutai["suspended"] is False
    pingan = out.row(by_predicate=pl.col("listing_id") == "CN:000001", named=True)
    assert pingan["suspended"] is True and pingan["close"] is None and pingan["volume"] is None


def test_free_tier_leaves_unknown_columns_null():
    p = provider({"daily": DAILY}, denied={"adj_factor", "stk_limit", "suspend_d"})
    out = conform(p.fetch("bars_1d", DAY.isoformat(), DAY, DAY)["bars_1d"], "bars_1d")
    assert out["adj_factor"].null_count() == 2 and out["up_limit"].null_count() == 2
    assert out["listing_id"].to_list() == ["CN:600519", "CN:830799"]
    assert p.denied == {"adj_factor", "stk_limit", "suspend_d"}


def test_pagination_uses_offset():
    calls = []
    rows = [{"ts_code": f"{600000 + i}.SH", "symbol": f"{600000 + i}", "name": "x", "market": "主板",
             "exchange": "SSE", "list_status": "L", "list_date": "20000101"} for i in range(7)]
    p = provider({"stock_basic": lambda prm: rows if prm["list_status"] == "L" else []}, calls=calls)
    import wb.data.providers.tushare as T

    T.PAGE["stock_basic"] = 3
    try:
        out = p.fetch("instruments", "all", DAY, DAY)["instruments"]
    finally:
        T.PAGE["stock_basic"] = 6000
    assert out.height == 7
    assert [c[1]["offset"] for c in calls if c[1]["list_status"] == "L"] == [0, 3, 6]


def test_rate_limit_message_slows_down_and_retries():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            # verbatim server text: it also contains "权限", which must not read as "no permission"
            return httpx.Response(200, json={"code": 40203, "data": None, "msg":
                "抱歉，您每分钟最多访问该接口200次，权限的具体详情访问：https://tushare.pro/document/1?doc_id=108。"})
        return httpx.Response(200, json={"code": 0, "data": {"fields": ["ts_code"], "items": [["600519.SH"]]}})

    http = HttpClient(10_000, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    p = provider({}, http=http)
    assert p.query("daily", "ts_code", trade_date="20260930").height == 1
    assert http.limiter.per_minute == 200


def test_no_token_is_permission_error(monkeypatch):
    monkeypatch.setattr("wb.data.providers.tushare.get_secret", lambda name: None)
    with pytest.raises(PermissionDenied):
        Tushare(http=HttpClient(10, transport=tushare_server({}))).query("daily", "ts_code")


def test_probe_reflects_free_tier():
    p = provider({"daily": DAILY}, denied={"adj_factor", "stk_limit", "suspend_d", "stock_basic", "namechange",
                                           "trade_cal", "dividend", "daily_basic", "index_classify",
                                           "index_member_all", "index_daily", "index_weight"})
    probes = {pr.dataset: pr for pr in p.probe()}
    assert probes["bars_1d"].available and "adj_factor" in probes["bars_1d"].detail
    assert not probes["valuation"].available and "权限" in probes["valuation"].detail
    assert not probes["index_bars_1d"].available and not probes["index_members"].available


def test_corp_actions_only_implemented_dividends():
    rows = [{"ts_code": "600519.SH", "div_proc": "实施", "stk_div": 0.3, "cash_div_tax": 2.0, "ex_date": "20260930"},
            {"ts_code": "000001.SZ", "div_proc": "预案", "stk_div": 0.5, "cash_div_tax": 0.0, "ex_date": "20260930"}]
    out = provider({"dividend": rows}).fetch("corp_actions", DAY.isoformat(), DAY, DAY)["corp_actions"]
    assert out.select("listing_id", "ratio").rows() == [("CN:600519", 1.3)]


def test_valuation_units():
    rows = [{"ts_code": "600519.SH", "trade_date": "20260930", "pe": 30.0, "total_share": 125619.78,
             "total_mv": 18968586.8}]
    out = provider({"daily_basic": rows}).fetch("valuation", DAY.isoformat(), DAY, DAY)["valuation"]
    assert out["total_share"][0] == pytest.approx(1_256_197_800) and out["total_mv"][0] == pytest.approx(189_685_868_000)


def test_api_ignoring_offset_does_not_loop_forever():
    rows = [["600519.SH"], ["000001.SZ"]]

    def handler(request):
        return httpx.Response(200, json={"code": 0, "data": {"fields": ["ts_code"], "items": rows}})

    import wb.data.providers.tushare as T

    http = HttpClient(10_000, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    T.PAGE["namechange"] = 2
    try:
        df = Tushare(token="t", http=http, sleep=lambda s: None).query("namechange", "ts_code")
    finally:
        del T.PAGE["namechange"]
    assert df.height == 2


def test_empty_response_is_an_empty_typed_frame():
    # Holidays (e.g. 2026-09-25) return zero rows; that must not crash the string conversions.
    out = provider({"daily": []}, denied={"adj_factor", "stk_limit", "suspend_d"}).fetch(
        "bars_1d", "2026-09-25", date(2026, 9, 25), date(2026, 9, 25))["bars_1d"]
    assert conform(out, "bars_1d").height == 0


def test_error_messages_are_classified_by_meaning():
    import wb.data.providers.tushare as T
    from wb.data.providers.base import Unavailable

    doc = "，权限的具体详情访问：https://tushare.pro/document/1?doc_id=108。"
    assert isinstance(T._error(40203, "抱歉，您每分钟最多访问该接口2次" + doc), T.RateLimited)
    assert isinstance(T._error(40203, "抱歉，您没有访问该接口的权限" + doc), T.PermissionDenied)
    daily = T._error(40203, "抱歉，您每天最多访问该接口20000次" + doc)
    # a used-up daily quota stops the run loudly; it must not mark the dataset "not on this plan"
    assert isinstance(daily, T.QuotaExceeded) and not isinstance(daily, Unavailable)


def test_token_only_travels_over_tls():
    import wb.data.providers.tushare as T

    assert T.URL.startswith("https://")


def test_index_members_are_each_indexs_month_end_weights():
    from wb.data.providers.tushare import INDEXES
    calls = []
    hs300 = [("600519.SH", 3.2), ("000001.SZ", 1.1)]

    def weights(p):
        if p["index_code"] != "000300.SH" or not ("20260901" <= "20260930" <= p["end_date"] and p["start_date"] <= "20260930"):
            return []
        return [{"index_code": "000300.SH", "con_code": c, "trade_date": "20260930", "weight": w} for c, w in hs300]
    out = provider({"index_weight": weights}, calls=calls).fetch("index_members", "all", date(2026, 9, 1), date(2026, 10, 3))
    df = out["index_members"].sort("listing_id")
    assert df.select("index_id", "date", "listing_id", "weight").rows() == [
        ("CN:IDX:000300", date(2026, 9, 30), "CN:000001", 1.1), ("CN:IDX:000300", date(2026, 9, 30), "CN:600519", 3.2)]
    asked = [p for api, p in calls if api == "index_weight"]
    assert {p["index_code"] for p in asked} == set(INDEXES.values())                  # every index, month by month
    assert {(p["start_date"], p["end_date"]) for p in asked} == {
        ("20260801", "20260831"), ("20260901", "20260930"), ("20261001", "20261031")}  # and the month before


def test_a_weekly_refresh_asks_for_the_month_that_ended_before_its_window():
    # a month's list is dated its last trading day and comes out after that close: Monday the 5th, whose window
    # starts on Saturday the 3rd, still asks for September
    calls = []
    provider({"index_weight": lambda p: []}, calls=calls).fetch("index_members", "all", date(2026, 10, 3), date(2026, 10, 5))
    assert {(p["start_date"], p["end_date"]) for api, p in calls if api == "index_weight"} == {
        ("20260901", "20260930"), ("20261001", "20261031")}


def test_an_index_is_not_asked_for_before_it_existed():
    calls = []
    provider({"index_weight": lambda p: []}, calls=calls).fetch("index_members", "all", date(2019, 11, 15), date(2019, 12, 31))
    asked = {(p["index_code"], p["start_date"]) for api, p in calls if api == "index_weight"}
    assert ("000688.SH", "20191201") in asked and ("000688.SH", "20191101") not in asked     # 科创50 from 2019-12-31
    assert not any(code == "899050.BJ" for code, _ in asked)                                  # 北证50 from 2022-04-29


def test_a_periods_financials_are_one_row_an_announcement_with_its_parts():
    # spec §4.7: by report period (the VIP endpoints give a whole period a call); a corrected copy of one announcement
    # (update_flag 1) wins; the balance sheet, cash flow and indicators join on the same announcement
    rep = {"ts_code": "600519.SH", "ann_date": "20260815", "f_ann_date": "20260815", "end_date": "20260630", "report_type": "1"}
    tables = {
        "income_vip": [{**rep, "update_flag": "0", "revenue": 9.07e10, "operate_profit": 6.1e10, "n_income_attr_p": 4.40e10, "basic_eps": 35.0},
                       {**rep, "update_flag": "1", "revenue": 9.07e10, "operate_profit": 6.1e10, "n_income_attr_p": 4.45e10, "basic_eps": 35.57}],
        "balancesheet_vip": [{**rep, "update_flag": "1", "total_assets": 3.09e11, "total_hldr_eqy_exc_min_int": 2.6e11}],
        "cashflow_vip": [{**rep, "update_flag": "1", "n_cashflow_act": 7.07e10}],
        "fina_indicator_vip": [{**rep, "update_flag": "1", "roe": 17.95, "netprofit_yoy": -1.95, "or_yoy": 3.1}],
    }
    calls = []
    out = conform(provider(tables, calls=calls, columns=FINA).fetch("financials", "2026-06-30", DAY, DAY)["financials"], "financials")
    assert out.height == 1 and {p.get("period") for _, p in calls} == {"20260630"}
    r = out.row(0, named=True)
    assert (r["listing_id"], r["period_end"], r["period_type"], r["ann_date"]) == ("CN:600519", date(2026, 6, 30), "H1", date(2026, 8, 15))
    assert (r["net_profit"], r["eps"], r["revenue"], r["op_profit"]) == (4.45e10, 35.57, 9.07e10, 6.1e10)
    assert (r["total_assets"], r["equity"], r["cfo"]) == (3.09e11, 2.6e11, 7.07e10)
    assert (r["roe"], r["profit_yoy"], r["revenue_yoy"], r["ann_time"], r["known_at"]) == (17.95, -1.95, 3.1, None, None)
    # without the indicators (a plan without them): their columns stay unknown, the rest is there
    p = provider({k: v for k, v in tables.items() if k != "fina_indicator_vip"}, denied={"fina_indicator_vip"})
    row = conform(p.fetch("financials", "2026-06-30", DAY, DAY)["financials"], "financials").row(0, named=True)
    assert row["roe"] is None and row["net_profit"] == 4.45e10


def test_a_period_without_reports_yet_is_an_empty_unit_not_an_error():
    # the quarter end just passed has no report for a week or two: its unit is empty, never a schema error that stops
    # the day's bars
    out = conform(provider({"income_vip": []}).fetch("financials", "2026-09-30", DAY, DAY)["financials"], "financials")
    assert out.height == 0


def test_a_correction_that_came_out_later_is_its_own_row_from_the_day_it_came_out():
    # real data (2026-10-04): a corrected copy can keep the first announcement's ann_date and carry the day it actually
    # came out in f_ann_date (603398.SH, Q3 2025: 2025-10-30 and 2026-04-30); its figures are known from that day only.
    # The indicators keep one row a report, dated by the first announcement (checked 2026-10-08): both copies get it
    first = {"ts_code": "603398.SH", "ann_date": "20251030", "f_ann_date": "20251030", "end_date": "20250930", "report_type": "1", "update_flag": "1"}
    fixed = {**first, "f_ann_date": "20260430"}
    tables = {"income_vip": [{**first, "revenue": 9.0, "operate_profit": 2.0, "n_income_attr_p": 1.0, "basic_eps": 0.1},
                             {**fixed, "revenue": 9.0, "operate_profit": 2.0, "n_income_attr_p": 0.5, "basic_eps": 0.05}],
              "fina_indicator_vip": [{"ts_code": "603398.SH", "ann_date": "20251030", "end_date": "20250930", "update_flag": "1", "roe": -44.0}]}
    out = conform(provider(tables, columns=FINA).fetch("financials", "2025-09-30", DAY, DAY)["financials"], "financials").sort("ann_date")
    assert out.select("ann_date", "net_profit", "roe").rows() == [(date(2025, 10, 30), 1.0, -44.0), (date(2026, 4, 30), 0.5, -44.0)]


def test_a_field_the_api_does_not_have_is_said_in_words():
    # Tushare leaves such a field out of its answer without an error (fina_indicator_vip and f_ann_date, 2026-10-08):
    # the rows no longer fit the names asked for, and polars only said "data does not match the number of columns"
    from wb.data.providers.tushare import TushareError
    p = provider({"fina_indicator_vip": [{"ts_code": "600519.SH", "roe": 1.0}]}, columns=FINA)
    with pytest.raises(TushareError, match="fina_indicator_vip.*f_ann_date"):
        p.query("fina_indicator_vip", "ts_code,f_ann_date,roe", period="20260630")


def test_industry_rows_carry_level_two_beside_level_one():
    # design 2026-10-12 §10: 申万二级 as two more columns of the same rows — the same index_member_all call gives it;
    # `name` stays 一级, one row a membership spell as before
    classify = [{"index_code": "801080.SI", "industry_name": "电子"}]

    def members(p):
        if p["is_new"] == "Y":
            return [{"l1_code": "801080.SI", "l1_name": "电子", "l2_code": "801081.SI", "l2_name": "半导体",
                     "ts_code": "002156.SZ", "in_date": "20070815", "out_date": None}]
        return [{"l1_code": "801080.SI", "l1_name": "电子", "l2_code": "801082.SI", "l2_name": "元件",
                 "ts_code": "002156.SZ", "in_date": "20040101", "out_date": "20070814"}]
    out = conform(provider({"index_classify": classify, "index_member_all": members}).fetch("industry", "all", DAY, DAY)["industry"],
                  "industry").sort("in_date")
    assert out.select("listing_id", "scheme", "name", "l2_code", "l2_name", "in_date", "out_date").rows() == [
        ("CN:002156", "SW2021_L1", "电子", "801082.SI", "元件", date(2004, 1, 1), date(2007, 8, 14)),
        ("CN:002156", "SW2021_L1", "电子", "801081.SI", "半导体", date(2007, 8, 15), None)]


def test_moneyflow_amounts_in_yuan_and_every_board_kept():
    rows = [{"ts_code": "600519.SH", "trade_date": "20260930", **{f"{s}_{k}_amount": 100.0 for s in ("buy", "sell") for k in ("sm", "md", "lg", "elg")},
             "net_mf_amount": -12.5},
            {"ts_code": "830799.BJ", "trade_date": "20260930", **{f"{s}_{k}_amount": 1.0 for s in ("buy", "sell") for k in ("sm", "md", "lg", "elg")},
             "net_mf_amount": 0.0}]
    out = conform(provider({"moneyflow": rows}).fetch("moneyflow", DAY.isoformat(), DAY, DAY)["moneyflow"], "moneyflow").sort("listing_id")
    assert out["listing_id"].to_list() == ["CN:600519", "CN:830799"]                    # 北交所 kept: readiness counts every listing
    m = out.row(by_predicate=pl.col("listing_id") == "CN:600519", named=True)
    assert m["buy_sm"] == 1_000_000 and m["net_mf"] == -125_000                         # 万元 → yuan


def test_unlocks_ask_each_day_around_the_update_and_keep_the_first_announcement():
    calls = []

    def rows(params):
        if params["float_date"] != "20261020":
            return []
        return [{"ts_code": "688001.SH", "ann_date": "20261012", "float_date": "20261020", "float_share": 2.666667e8, "float_ratio": 12.0,
                 "holder_name": "某创投", "share_type": "首发原始股"},
                {"ts_code": "688001.SH", "ann_date": "20250918", "float_date": "20261020", "float_share": 2.66666667e8, "float_ratio": 12.0,
                 "holder_name": "某创投", "share_type": "首发原始股"},
                {"ts_code": "688001.SH", "ann_date": "20250918", "float_date": "20261020", "float_share": 1e6, "float_ratio": 0.05,
                 "holder_name": None, "share_type": None}]
    out = conform(provider({"share_float": rows}, calls=calls).fetch("unlocks", "all", DAY, DAY)["unlocks"], "unlocks").sort("holder")
    asked = sorted(p["float_date"] for api, p in calls if api == "share_float")
    assert len(asked) == 68 and asked[0] == "20260923" and asked[-1] == "20261129"   # a week back, two months ahead, one day a call
    assert out.select("holder", "share_type").rows() == [("", ""), ("某创投", "首发原始股")]   # nameless holder kept, not dropped
    assert out.filter(pl.col("holder") == "某创投")["ann_date"].to_list() == [date(2025, 9, 18)]   # the first announcement wins


def test_probe_covers_moneyflow_and_unlocks():
    probes = {p.dataset: p for p in provider({}, denied={"moneyflow"}).probe()}
    assert not probes["moneyflow"].available and probes["unlocks"].available


def test_float_holders_ask_one_report_period_of_every_company_each_holder_once_a_list():
    # 多个外资持股 (10-17): the top-10 float holders of every company by report period — a Saturday's reports too (a fifth
    # of them are dated Saturday: asking by trading day would miss them); one holder's two accounts in a list are one row
    calls = []
    row = {"ts_code": "300001.SZ", "ann_date": "20260822", "end_date": "20260630", "holder_type": "一般企业"}
    rows = [{**row, "holder_name": "UBSAG", "hold_float_ratio": 0.6496},
            {**row, "holder_name": "香港中央结算有限公司", "hold_float_ratio": 1.2},
            {**row, "ts_code": "000002.SZ", "ann_date": "20260826", "holder_name": "某自然人", "hold_float_ratio": 2.4001, "holder_type": "自然人"},
            {**row, "ts_code": "000002.SZ", "ann_date": "20260826", "holder_name": "某自然人", "hold_float_ratio": 1.2698, "holder_type": "自然人"}]
    out = conform(provider({"top10_floatholders": rows}, calls=calls).fetch("float_holders", "2026-06-30", DAY, DAY)["float_holders"],
                  "float_holders")
    assert {p["period"] for api, p in calls if api == "top10_floatholders"} == {"20260630"} and "ts_code" not in calls[0][1]
    assert out.height == 3 and sorted(out["listing_id"].unique()) == ["CN:000002", "CN:300001"]
    li = out.filter(pl.col("holder") == "某自然人").row(0, named=True)
    assert li["float_ratio"] == pytest.approx(3.6699) and li["period_end"] == date(2026, 6, 30) and li["ann_date"] == date(2026, 8, 26)
    assert li["holder_type"] == "自然人"


def test_probe_covers_float_holders():
    probes = {p.dataset: p for p in provider({}, denied={"top10_floatholders"}).probe()}
    assert not probes["float_holders"].available and probes["moneyflow"].available
