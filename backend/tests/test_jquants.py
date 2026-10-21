from datetime import date

import polars as pl
import pytest

from tests.fakes import jquants_server
from wb.data.http import HttpClient
from wb.data.normalize import apply_factors
from wb.data.providers.jquants import JQuants
from wb.datasets import conform

DAY = date(2026, 9, 30)
BARS = [
    {"Date": "2026-09-30", "Code": "72030", "O": 2800.0, "H": 2850.0, "L": 2790.0, "C": 2840.0,
     "UL": "0", "LL": "0", "Vo": 1.0e7, "Va": 2.83e10, "AdjFactor": 1.0},
    {"Date": "2026-09-30", "Code": "130A0", "O": 500.0, "H": 580.0, "L": 500.0, "C": 580.0,
     "UL": "1", "LL": "0", "Vo": 2.0e5, "Va": 1.1e8, "AdjFactor": 0.5},
    {"Date": "2026-09-30", "Code": "99990", "O": None, "H": None, "L": None, "C": None,
     "UL": "0", "LL": "0", "Vo": None, "Va": None, "AdjFactor": 1.0},
]


def provider(routes, calls=None, page_size=10**9):
    http = HttpClient(10_000, transport=jquants_server(routes, calls, page_size), sleep=lambda s: None)
    return JQuants(api_key="k", http=http)


def test_bars_flags_no_trade_and_split_event():
    calls = []
    out = provider({"/equities/bars/daily": BARS}, calls).fetch("bars_1d", "2026-09-30", DAY, DAY)
    bars = conform(out["bars_1d"], "bars_1d").sort("listing_id")
    assert bars["listing_id"].to_list() == ["JP:130A", "JP:7203", "JP:9999"]
    assert bars["ul"].to_list() == [True, False, False]
    assert bars["suspended"].to_list() == [False, False, True]  # no-trade record, not a missing row
    assert out["corp_actions"].select("listing_id", "ratio").rows() == [("JP:130A", 2.0)]
    assert calls[0][2] == "k" and calls[0][1] == {"date": "2026-09-30"}


def test_split_event_makes_factors():
    out = provider({"/equities/bars/daily": BARS}).fetch("bars_1d", "2026-09-30", DAY, DAY)
    bars = apply_factors(conform(out["bars_1d"], "bars_1d"), conform(out["corp_actions"], "corp_actions"),
                         set_price=True)
    assert bars.filter(pl.col("listing_id") == "JP:130A")["adj_factor"][0] == 2.0


def test_pagination_follows_key():
    calls = []
    p = provider({"/equities/bars/daily": BARS}, calls, page_size=2)
    assert p.fetch("bars_1d", "2026-09-30", DAY, DAY)["bars_1d"].height == 3
    assert [c[1].get("pagination_key") for c in calls] == [None, "2"]


def test_instruments_and_calendar():
    p = provider({
        "/equities/master": [{"Date": "2026-09-30", "Code": "130A0", "CoName": "Ｖｅｒｉｔａｓ　Ｉｎ　Ｓｉｌｉｃｏ",
                              "CoNameEn": "Veritas In Silico", "S33Nm": "医薬品", "Mkt": "0113", "MktNm": "グロース"}],
        "/markets/calendar": [{"Date": "2026-09-30", "HolDiv": "1"}, {"Date": "2026-10-03", "HolDiv": "0"},
                              {"Date": "2026-12-30", "HolDiv": "2"}],
    })
    inst = conform(p.fetch("instruments", "all", DAY, DAY)["instruments"], "instruments")
    assert inst["listing_id"][0] == "JP:130A" and inst["board"][0] == "グロース" and inst["status"][0] == "L"
    cal = p.fetch("calendar", "all", date(2026, 9, 1), date(2026, 12, 31))["calendar"]
    assert cal["is_open"].to_list() == [True, False, True]


def test_a_fund_is_named_by_its_product_not_by_its_segment():
    # 商品区分 (ProdCat, checked 2026-10-08): 011 domestic and 021 foreign stocks keep their segment as the board; ETFs,
    # REITs and 出資証券 are named as JPX's list names them, so the stock pool can leave them out (PRO Market keeps its
    # segment, left out too)
    rows = [{"Code": c + "0", "CoName": c, "MktNm": m, "ProdCat": k} for c, m, k in (
        ("7203", "プライム", "011"), ("9999", "グロース", "021"), ("1306", "その他", "014"), ("1557", "その他", "023"),
        ("8951", "その他", "013"), ("8301", "スタンダード", "012"), ("131A", "TOKYO PRO MARKET", "011"))]
    inst = conform(provider({"/equities/master": rows}).fetch("instruments", "all", DAY, DAY)["instruments"], "instruments")
    assert dict(inst.select("code", "board").iter_rows()) == {
        "7203": "プライム", "9999": "グロース", "1306": "ETF・ETN", "1557": "ETF・ETN",
        "8951": "REIT・ベンチャーファンド・カントリーファンド・インフラファンド", "8301": "出資証券", "131A": "TOKYO PRO MARKET"}


def test_a_delisted_listing_is_named_as_the_master_listed_it_on_its_last_day():
    # the master of a past day (checked 2026-10-08: /equities/master?date=2019-12-18 lists 13100, delisted that month):
    # one request a month, on the month's earliest last day (the month's others were still listed then); one the master
    # did not have yet that day (listed later that month) on its own day; one it never had is left out
    master = {
        "2016-10-13": [{"Code": "96800", "CoName": "共成レンテム", "MktNm": "東証二部", "S33Nm": "サービス業", "ProdCat": "011"},
                       {"Code": "51960", "CoName": "鬼怒川ゴム工業", "MktNm": "東証一部", "ProdCat": "011"}],
        "2016-10-26": [{"Code": "82480", "CoName": "ニッセン", "MktNm": "東証一部", "ProdCat": "011"}],
        "2019-12-18": [{"Code": "13100", "CoName": "ダイワ上場投信−トピックス・コア３０", "MktNm": "その他", "S33Nm": "その他",
                        "ProdCat": "014"}],
    }
    calls = []
    p = provider({"/equities/master": lambda q: master.get(q["date"], [])}, calls)
    out = conform(p.describe({"JP:9680": date(2016, 10, 13), "JP:5196": date(2016, 10, 20), "JP:8248": date(2016, 10, 26),
                              "JP:9898": date(2016, 10, 31), "JP:1310": date(2019, 12, 18)}), "instruments").sort("listing_id")
    assert [c[1]["date"] for c in calls] == ["2016-10-13", "2016-10-26", "2016-10-31", "2019-12-18"]
    assert out.select("listing_id", "name", "board", "status", "source").rows() == [
        ("JP:1310", "ダイワ上場投信−トピックス・コア３０", "ETF・ETN", "D", "jquants"),
        ("JP:5196", "鬼怒川ゴム工業", "東証一部", "D", "jquants"),
        ("JP:8248", "ニッセン", "東証一部", "D", "jquants"),
        ("JP:9680", "共成レンテム", "東証二部", "D", "jquants")]
    assert out.filter(pl.col("listing_id") == "JP:9680")["industry"][0] == "サービス業"


def test_valuation_market_cap_in_yen():
    p = provider({"/equities/valuation": [{"Date": "2026-09-30", "Code": "72030", "PER": 9.5, "PBR": 1.1,
                                           "EPS": 300.0, "BPS": 2500.0, "MktCap": 45000000.0}]})
    v = p.fetch("valuation", "2026-09-30", DAY, DAY)["valuation"]
    assert v["total_mv"][0] == 45_000_000_000_000


def test_index_codes_mapped_and_others_dropped():
    p = provider({"/indices/bars/daily": [{"Date": "2026-09-30", "Code": "0000", "O": 1, "H": 2, "L": 1, "C": 2},
                                          {"Date": "2026-09-30", "Code": "0040", "O": 1, "H": 2, "L": 1, "C": 2}]})
    df = p.fetch("index_bars_1d", "2026-09-30", DAY, DAY)["index_bars_1d"]
    assert df["index_id"].to_list() == ["JP:IDX:TOPIX"]


def test_probe_reads_subscription_window_from_error():
    p = provider({
        "/equities/master": [{"Code": "72030"}],
        "/equities/bars/daily": (400, "Your subscription covers the following dates: 2021-10-02 ~ 2026-07-10."),
        "/equities/valuation": [], "/indices/bars/daily": (403, "not in plan"), "/markets/calendar": [], "/fins/summary": [],
    })
    probes = {x.dataset: x for x in p.probe()}
    assert probes["financials"].available                                              # the statement summaries (§4.7)
    assert probes["bars_1d"].available and probes["bars_1d"].history_start == date(2021, 10, 2)
    assert probes["bars_1d"].latest_date == date(2026, 7, 10)
    assert probes["bars_1d"].rate_per_min == 60  # ~5 years of history -> Light plan rate
    assert not probes["index_bars_1d"].available


def test_bad_key_marks_everything_unavailable():
    p = provider({"/equities/master": (403, "invalid api key")})
    assert not any(x.available for x in p.probe())


def test_probe_queries_inside_free_plan_window():
    # Real free-plan message (2026-10-02). Datasets must be probed inside the window, not "the last 2 weeks".
    msg = ("Your subscription covers the following dates: 2024-07-10 ~ 2026-07-10. "
           "If you want more data, please check other plans:https://jpx-jquants.com/#dataset")

    def windowed(params):
        if params.get("to", "") > "2026-07-10":
            raise AssertionError("probe asked outside the subscription window")
        return [{"Date": "2026-07-10", "Code": "72030"}]

    def routes_valuation(params):
        return windowed(params)

    p = provider({
        "/equities/master": [{"Code": "72030"}],
        "/equities/bars/daily": (400, msg),
        "/equities/valuation": routes_valuation,
        "/indices/bars/daily": (403, "This API is not available on your subscription."),
        "/markets/calendar": windowed,
    })
    probes = {x.dataset: x for x in p.probe()}
    assert probes["bars_1d"].rate_per_min == 5  # 2 years -> Free plan
    assert probes["valuation"].available and probes["calendar"].available
    assert probes["valuation"].latest_date == date(2026, 7, 10)
    assert not probes["index_bars_1d"].available


def test_a_separate_issue_keeps_its_fifth_digit_in_the_code():
    # '25935' is another issue of 2593 (e.g. preferred shares): code and listing id must agree
    p = provider({"/equities/master": [{"Code": "130A0", "CoName": "x"}, {"Code": "25935", "CoName": "y"}]})
    inst = conform(p.fetch("instruments", "all", DAY, DAY)["instruments"], "instruments")
    assert inst["listing_id"].to_list() == ["JP:130A", "JP:25935"] and inst["code"].to_list() == ["130A", "25935"]


def test_a_days_financial_statements_come_with_their_time_and_without_forecast_revisions():
    # spec §4.7: J-Quants' summaries by disclosure day; a forecast revision has no figures of its own; ROE in % as CN's
    rows = [{"Code": "72030", "DiscDate": "2026-05-08", "DiscTime": "13:55:00", "DocType": "FYFinancialStatements_Consolidated_IFRS",
             "CurPerType": "FY", "CurPerEn": "2026-03-31", "Sales": "50684952000000", "OP": "3766216000000", "NP": "3848098000000",
             "EPS": "295.25", "TA": "105522331000000", "Eq": "41020068000000", "CFO": "5472920000000", "ROE": "0.101"},
            {"Code": "67580", "DiscDate": "2026-05-08", "DiscTime": "15:30:00", "DocType": "1QFinancialStatements_Consolidated_JP",
             "CurPerType": "1Q", "CurPerEn": "2026-03-31", "Sales": "1000", "OP": "", "NP": "50", "EPS": "", "TA": "", "Eq": "",
             "CFO": "", "ROE": ""},
            {"Code": "99840", "DiscDate": "2026-05-08", "DiscTime": "15:00:00", "DocType": "EarnForecastRevision",
             "CurPerType": "FY", "CurPerEn": "2027-03-31", "Sales": "", "NP": ""}]
    calls = []
    out = conform(provider({"/fins/summary": rows}, calls).fetch("financials", "2026-05-08", date(2026, 5, 8), date(2026, 5, 8))["financials"],
                  "financials").sort("listing_id")
    assert out["listing_id"].to_list() == ["JP:6758", "JP:7203"] and calls[0][1] == {"date": "2026-05-08"}
    toyota = out.row(1, named=True)
    assert (toyota["period_type"], toyota["period_end"], toyota["ann_time"], toyota["net_profit"]) == ("FY", date(2026, 3, 31), "13:55:00", 3848098000000.0)
    assert toyota["roe"] == pytest.approx(10.1) and toyota["doc"] == "FYFinancialStatements_Consolidated_IFRS"
    sony = out.row(0, named=True)
    assert (sony["period_type"], sony["op_profit"], sony["eps"], sony["roe"]) == ("Q1", None, None, None)   # '' is unknown, not 0


def test_a_statement_without_its_period_end_or_day_is_left_out_not_an_error():
    # '' is J-Quants' unknown: a statement that cannot be placed in time is left out; the day's others still come
    rows = [{"Code": "72030", "DiscDate": "2026-05-08", "DiscTime": "13:55:00", "DocType": "FYFinancialStatements_Consolidated_IFRS",
             "CurPerType": "FY", "CurPerEn": "2026-03-31", "NP": "1"},
            {"Code": "67580", "DiscDate": "2026-05-08", "DiscTime": "15:30:00", "DocType": "1QFinancialStatements_Consolidated_JP",
             "CurPerType": "1Q", "CurPerEn": "", "NP": "2"},
            {"Code": "99840", "DiscDate": "", "DiscTime": "", "DocType": "FYFinancialStatements_Consolidated_JP",
             "CurPerType": "FY", "CurPerEn": "2026-03-31", "NP": "3"}]
    day = date(2026, 5, 8)
    out = conform(provider({"/fins/summary": rows}).fetch("financials", "2026-05-08", day, day)["financials"], "financials")
    assert out["listing_id"].to_list() == ["JP:7203"]


# ---- history by the month (J-Quants' bulk files, Light plan and up) -------------------------------------------------
import gzip  # noqa: E402

from polars.testing import assert_frame_equal  # noqa: E402


def csv_gz(rows: list[dict]) -> bytes:
    """A bulk file as J-Quants writes it: gzip'd CSV with a header, nothing between two commas for no value."""
    cols = list(dict.fromkeys(c for r in rows for c in r))
    lines = [",".join(cols)] + [",".join("" if r.get(c) is None else str(r[c]) for c in cols) for r in rows]
    return gzip.compress(("\n".join(lines) + "\n").encode())


def test_a_past_month_comes_from_its_bulk_file_one_request_a_month():
    # J-Quants' advice for history: a 10-year backfill asked 2,440 days one or more requests each (an hour or more at
    # Standard's 120 a minute for the bars alone); a month's file is one request. The day's rows are the daily endpoint's
    month = [{**r, "Date": d} for d in ("2026-09-29", "2026-09-30") for r in BARS]
    calls = []
    p = provider({"/bulk/get": {"url": "https://files.example/bars_202609.csv.gz"}, "/bars_202609.csv.gz": csv_gz(month)}, calls)
    end = date(2026, 10, 8)                       # the range goes on into October: September is a past month
    got = [p.fetch("bars_1d", d, date(2016, 10, 8), end) for d in ("2026-09-29", "2026-09-30")]
    api = provider({"/equities/bars/daily": BARS}).fetch("bars_1d", "2026-09-30", DAY, DAY)
    assert_frame_equal(got[1]["bars_1d"].sort("listing_id"), api["bars_1d"].sort("listing_id"))
    assert_frame_equal(got[1]["corp_actions"], api["corp_actions"])
    assert got[0]["bars_1d"]["date"].unique().to_list() == [date(2026, 9, 29)]
    assert [c[0] for c in calls] == ["/bulk/get", "/bars_202609.csv.gz"]                 # nothing day by day
    assert calls[0][1] == {"endpoint": "/equities/bars/daily", "date": "2026-09"} and calls[0][2] == "k"
    assert calls[1][2] is None                                                          # the key never leaves J-Quants


def test_a_month_without_its_file_goes_day_by_day_and_asks_once():
    # a month's file comes a few days after it ends (September's on 5 October), and plans below Light have none
    calls = []
    p = provider({"/bulk/get": (400, "The specified file does not exist. Please check your request parameters."),
                  "/equities/bars/daily": BARS}, calls)
    for d in ("2026-09-29", "2026-09-30"):
        assert p.fetch("bars_1d", d, date(2016, 10, 8), date(2026, 10, 2))["bars_1d"].height == 3
    assert [c[0] for c in calls] == ["/bulk/get", "/equities/bars/daily", "/equities/bars/daily"]


def test_the_last_month_of_the_range_goes_day_by_day():
    calls = []
    provider({"/equities/bars/daily": BARS}, calls).fetch("bars_1d", "2026-09-30", date(2026, 9, 1), DAY)
    assert [c[0] for c in calls] == ["/equities/bars/daily"]


FINS = [{"Code": "72030", "DiscDate": "2026-05-08", "DiscTime": "13:55:00", "DocType": "FYFinancialStatements_Consolidated_IFRS",
         "CurPerType": "FY", "CurPerEn": "2026-03-31", "Sales": "50684952000000", "OP": "3766216000000", "NP": "3848098000000",
         "EPS": "295.25", "TA": "105522331000000", "Eq": "41020068000000", "CFO": "5472920000000", "ROE": "0.101"},
        {"Code": "67580", "DiscDate": "2026-05-08", "DiscTime": "15:30:00", "DocType": "1QFinancialStatements_Consolidated_JP",
         "CurPerType": "1Q", "CurPerEn": "2026-03-31", "Sales": "1000", "OP": "", "NP": "50", "EPS": "", "TA": "", "Eq": "",
         "CFO": "", "ROE": ""},
        {"Code": "99840", "DiscDate": "2026-05-08", "DiscTime": "15:00:00", "DocType": "EarnForecastRevision",
         "CurPerType": "FY", "CurPerEn": "2027-03-31", "Sales": "", "OP": "", "NP": "", "EPS": "", "TA": "", "Eq": "", "CFO": "", "ROE": ""}]


@pytest.mark.parametrize("dataset,path,rows,day", [
    ("valuation", "/equities/valuation", [{"Date": "2026-05-08", "Code": "72030", "PER": 9.5, "PBR": 1.1, "EPS": 300.0,
                                           "BPS": 2500.0, "MktCap": 45000000.0}], "2026-05-08"),
    ("index_bars_1d", "/indices/bars/daily", [{"Date": "2026-05-08", "Code": "0000", "O": 1.5, "H": 2.0, "L": 1.0, "C": 2.0},
                                              {"Date": "2026-05-08", "Code": "0040", "O": 1.0, "H": 2.0, "L": 1.0, "C": 2.0}], "2026-05-08"),
    ("financials", "/fins/summary", FINS, "2026-05-08"),   # its file's day is the disclosure day
])
def test_each_daily_dataset_reads_the_same_from_its_months_file(dataset, path, rows, day):
    d = date.fromisoformat(day)
    api = provider({path: rows}).fetch(dataset, day, d, d)[dataset]
    other = [{**r, ("DiscDate" if dataset == "financials" else "Date"): "2026-05-07"} for r in rows]
    p = provider({"/bulk/get": {"url": "https://files.example/m.csv.gz"}, "/m.csv.gz": csv_gz(rows + other)})
    got = p.fetch(dataset, day, date(2016, 10, 8), date(2026, 10, 8))[dataset]
    key = got.columns[0]
    assert_frame_equal(got.sort(key), api.sort(key))
