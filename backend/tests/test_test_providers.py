import io
from datetime import date

import httpx
import pandas as pd
import polars as pl

from wb.data.http import HttpClient, RateLimiter
from wb.data.providers.exchange_list_cn import ExchangeListCN, parse_szse_xlsx
from wb.data.providers.jpx_list import JpxList, parse_jpx_xlsx
from wb.data.providers.xcals import XCals
from wb.data.providers.yfinance_test import YFinance, sample_pool
from wb.datasets import conform


def xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


JPX = pd.DataFrame({
    "日付": [20260831] * 4, "コード": [1301, "130A", 1305, "131A"],
    "銘柄名": ["極洋", "Ｖｅｒｉｔａｓ　Ｉｎ　Ｓｉｌｉｃｏ", "ｉＦｒｅｅＥＴＦ", "ＣＣＮグループ"],
    "市場・商品区分": ["プライム（内国株式）", "グロース（内国株式）", "ETF・ETN", "PRO Market"],
    "33業種コード": ["50", "3250", "-", "5250"], "33業種区分": ["水産・農林業", "医薬品", "-", "情報・通信業"],
    "17業種コード": ["1", "5", "-", "10"], "17業種区分": ["食品", "医薬品", "-", "x"],
    "規模コード": ["6", "-", "-", "-"], "規模区分": ["TOPIX Small 1", "-", "-", "-"],
})


def test_jpx_xlsx_keeps_letter_codes_and_all_boards():
    df = conform(parse_jpx_xlsx(xlsx(JPX)), "instruments")
    assert df["listing_id"].to_list() == ["JP:1301", "JP:130A", "JP:1305", "JP:131A"]
    assert df["industry"].to_list() == ["水産・農林業", "医薬品", None, "情報・通信業"]


def test_jpx_provider_downloads_xlsx():
    body = xlsx(JPX)
    http = HttpClient(100, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    assert JpxList(http=http).fetch("instruments", "all", date.today(), date.today())["instruments"].height == 4


def test_szse_codes_zero_padded_and_names_cleaned():
    pdf = pd.DataFrame({"板块": ["主板", "创业板"], "A股代码": [1, 300750], "A股简称": ["平安银行", "万  科Ａ"],
                        "A股上市日期": ["1991-04-03", "2018-06-11"], "所属行业": ["J 金融业", "C 制造业"]})
    df = parse_szse_xlsx(xlsx(pdf))
    assert df["listing_id"].to_list() == ["CN:000001", "CN:300750"]
    assert df["name"].to_list() == ["平安银行", "万科Ａ"]
    assert df["list_date"][0] == date(1991, 4, 3)


def test_exchange_list_cn_combines_sse_and_szse():
    szse = xlsx(pd.DataFrame({"板块": ["主板"], "A股代码": [1], "A股简称": ["平安银行"],
                              "A股上市日期": ["1991-04-03"], "所属行业": ["J 金融业"]}))

    def handler(request):
        if "sse.com.cn" in request.url.host:
            code = "600000" if request.url.params["STOCK_TYPE"] == "1" else "688001"
            assert request.headers["Referer"].startswith("https://www.sse.com.cn/")
            return httpx.Response(200, json={"result": [{"A_STOCK_CODE": code, "SEC_NAME_CN": "x", "LIST_DATE": "19991110"}]})
        return httpx.Response(200, content=szse)

    p = ExchangeListCN(http=HttpClient(100, transport=httpx.MockTransport(handler)))
    df = conform(p.fetch("instruments", "all", date.today(), date.today())["instruments"], "instruments")
    assert sorted(df["listing_id"].to_list()) == ["CN:000001", "CN:600000", "CN:688001"]
    assert dict(zip(df["code"], df["board"], strict=True)) == {"600000": "主板", "688001": "科创板", "000001": "主板"}


def toyota_history(ticker, start, end):
    idx = pd.DatetimeIndex(["2021-09-28", "2021-09-29", "2021-09-30"]).tz_localize("Asia/Tokyo")
    return pd.DataFrame({"Open": [2084.0, 2052.0, 2088.0], "High": [2092.0, 2080.5, 2088.0],
                         "Low": [2050.0, 2040.5, 2000.0], "Close": [2077.0, 2073.0, 2000.0],
                         "Adj Close": [1772.9, 1790.2, 1727.1], "Volume": [189085000, 34887300, 33149100],
                         "Dividends": [0.0, 24.0, 0.0], "Stock Splits": [0.0, 5.0, 0.0]}, index=idx)


def test_yfinance_bars_are_raw_and_split_recorded():
    p = YFinance("JP", history=toyota_history, limiter=RateLimiter(10_000))
    out = p.fetch("bars_1d", "7203.T", date(2021, 9, 28), date(2021, 9, 30))
    bars = conform(out["bars_1d"], "bars_1d")
    assert bars["listing_id"].unique().to_list() == ["JP:7203"]
    assert bars["close"].to_list() == [10385.0, 2073.0, 2000.0]
    assert bars["amount"].null_count() == 3  # Yahoo has no turnover: unknown, not estimated
    assert out["corp_actions"].select("ex_date", "ratio").rows() == [(date(2021, 9, 29), 5.0)]


def test_yfinance_empty_history_is_empty_frame():
    p = YFinance("JP", history=lambda *a: pd.DataFrame(), limiter=RateLimiter(10_000))
    assert p.fetch("bars_1d", "130A.T", date(2026, 1, 5), date(2026, 1, 9))["bars_1d"].height == 0


def test_sample_pool_is_deterministic_and_keeps_letter_code():
    inst = conform(pl.DataFrame({
        "listing_id": [f"JP:{c}" for c in ["1301", "130A", "7203", "1305"]],
        "code": ["1301", "130A", "7203", "1305"],
        "board": ["プライム（内国株式）", "グロース（内国株式）", "プライム（内国株式）", "ETF・ETN"]}), "instruments")
    assert sample_pool(inst, n=2) == ["1301", "130A", "7203"]


def test_xcals_calendar_marks_holidays():
    cal = XCals("JP").fetch("calendar", "all", date(2026, 9, 21), date(2026, 9, 24))["calendar"]
    assert dict(zip(cal["date"], cal["is_open"], strict=True)) == {
        date(2026, 9, 21): False, date(2026, 9, 22): False, date(2026, 9, 23): False, date(2026, 9, 24): True}


def test_xcals_stops_at_package_bound():
    cal = XCals("CN").fetch("calendar", "all", date(2026, 12, 30), date(2027, 1, 10))["calendar"]
    assert cal["date"].max() <= date(2026, 12, 31)


def test_yfinance_undoes_splits_after_requested_range():
    # Regression: a split after `end` still distorts Yahoo's prices inside [start, end].
    seen = {}

    def history(ticker, start, end):
        seen["end"] = end
        return toyota_history(ticker, start, end)

    p = YFinance("JP", history=history, limiter=RateLimiter(10_000), today=lambda: date(2026, 10, 2))
    out = p.fetch("bars_1d", "7203.T", date(2021, 9, 28), date(2021, 9, 28))
    assert seen["end"] == date(2026, 10, 2)
    assert out["bars_1d"]["close"].to_list() == [10385.0]  # raw, although the split is after `end`
    assert out["corp_actions"].height == 0  # the split itself is outside the requested range


def test_yfinance_missing_values_are_missing_not_nan():
    # Yahoo returns NaN for a day it has no price for: that is unknown, and must look like it
    def history(ticker, start, end):
        h = toyota_history(ticker, start, end)
        h.loc[h.index[1], ["Open", "High", "Low", "Close", "Volume"]] = float("nan")
        return h

    p = YFinance("JP", history=history, limiter=RateLimiter(10_000), today=lambda: date(2021, 9, 30))
    bars = p.fetch("bars_1d", "7203.T", date(2021, 9, 28), date(2021, 9, 30))["bars_1d"]
    assert bars["close"].null_count() == 1 and not bars["close"].is_nan().any()


def test_topix_members_come_from_jpxs_list_with_their_size_class():
    from wb.data.providers.jpx_topix import parse_topix
    csv = ("日付,銘柄名,コード,業種,TOPIXに占める個別銘柄のウエイト,ニューインデックス区分\n"
           "20260831,トヨタ自動車,7203,輸送用機器,3.9112%,TOPIX Core30\n"
           "20260831,極洋,1301,水産・農林業,0.0048%,TOPIX Small 1\n"
           "20260831,テスト,130A,情報・通信業,0.0100%,TOPIX Mid400\n"
           ",,,,,\n・当社は、本資料の内容について責任を負いません。,,,,,\n").encode("cp932")
    df = parse_topix(csv).sort("listing_id")
    assert df.select("index_id", "date", "listing_id", "weight", "segment").rows() == [
        ("JP:IDX:TOPIX", date(2026, 8, 31), "JP:1301", 0.0048, "TOPIX Small 1"),
        ("JP:IDX:TOPIX", date(2026, 8, 31), "JP:130A", 0.01, "TOPIX Mid400"),
        ("JP:IDX:TOPIX", date(2026, 8, 31), "JP:7203", 3.9112, "TOPIX Core30")]
