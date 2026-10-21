"""TickFlow as the prod profile's CN realtime source (spec §4.8), when its key is set. Shapes below are as measured on
2026-10-09 with a Pro key: GET /v1/klines/batch answers ≤100 symbols a call with each symbol's columns; 1-minute bars
are labelled by their end, and the day starts with a 09:30 bar that is the opening auction alone (241 a day); volume
is in lots (科创板 too), amount in yuan, and prices carry float noise."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import polars as pl
import pytest

from wb import registry
from wb.realtime.providers.tickflow_rt import TickFlowError, TickFlowRealtime, symbol

DAY = date(2026, 10, 9)
CST = timezone(timedelta(hours=8))


def ms(h: int, m: int, day: date = DAY) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=CST).timestamp() * 1000)


def columns(bars):
    """(time, open, high, low, close, volume in lots, amount in yuan) -> the API's columns."""
    keys = ("timestamp", "open", "high", "low", "close", "volume", "amount")
    return {k: [b[i] for b in bars] for i, k in enumerate(keys)}


def serve(answer, seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return answer(request)
    return httpx.MockTransport(handle)


def test_listing_ids_and_indexes_become_tickflow_symbols():
    assert [symbol(x) for x in ("CN:600000", "CN:000001", "CN:688981", "CN:300750", "CN:920021", "CN:830799",
                                "CN:IDX:000001", "CN:IDX:399001", "CN:IDX:399006", "CN:IDX:000300", "CN:IDX:899050")] == [
        "600000.SH", "000001.SZ", "688981.SH", "300750.SZ", "920021.BJ", "830799.BJ",
        "000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "899050.BJ"]   # 北证50 is the Beijing exchange's (2026-10-11)


def test_minutes_are_labelled_by_their_start_with_the_opening_auction_in_the_first_and_volume_in_shares():
    bars = [(ms(9, 30), 9.7, 9.7, 9.7, 9.7, 4256, 4128320.0),                 # the opening auction alone
            (ms(9, 31), 9.69, 9.739999999999998, 9.67, 9.7, 22370, 21714083.0),
            (ms(9, 32), 9.69, 9.699999999999998, 9.68, 9.68, 7597, 7359813.0),
            (ms(11, 30), 9.68, 9.679999999999998, 9.67, 9.67, 1019, 985682.0),
            (ms(13, 1), 9.68, 9.68, 9.67, 9.68, 8994, 8700343.0),
            (ms(15, 0), 9.540000000000004, 9.54, 9.54, 9.54, 12725, 12140060.0)]  # the closing auction
    src = TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(200, json={"data": {"600000.SH": columns(bars)}})),
                           key=lambda: "k")
    m = src.minutes(["CN:600000"], DAY)
    assert m.select("at", "open", "high", "low", "close", "volume", "amount").rows() == [
        (datetime(2026, 10, 9, 9, 30), 9.7, 9.74, 9.67, 9.7, 2662600.0, 25842403.0),   # its open is the day's open
        (datetime(2026, 10, 9, 9, 31), 9.69, 9.7, 9.68, 9.68, 759700.0, 7359813.0),
        (datetime(2026, 10, 9, 11, 29), 9.68, 9.68, 9.67, 9.67, 101900.0, 985682.0),
        (datetime(2026, 10, 9, 13, 0), 9.68, 9.68, 9.67, 9.68, 899400.0, 8700343.0),
        (datetime(2026, 10, 9, 14, 59), 9.54, 9.54, 9.54, 9.54, 1272500.0, 12140060.0)]
    assert m["listing_id"].unique().to_list() == ["CN:600000"]


def test_bars_before_the_first_trade_are_placeholders_and_never_make_the_open_or_the_range():
    # 2026-09-18 000819: no auction match, yet a 09:30 bar at the close before (11.8, volume 0); the day opened 11.86
    # at the first trade. 603869 on 09-15 had placeholders in its first minute too. A minute without trades after the
    # first one carries the last price and stays (the 分时 line goes on).
    bars = [(ms(9, 30), 11.8, 11.8, 11.8, 11.8, 0, 0.0), (ms(9, 31), 11.8, 11.8, 11.8, 11.8, 0, 0.0),
            (ms(9, 32), 11.86, 11.91, 11.72, 11.84, 1061, 1256000.0), (ms(9, 33), 11.84, 11.84, 11.84, 11.84, 0, 0.0),
            (ms(9, 34), 11.85, 11.9, 11.8, 11.88, 546, 648000.0)]
    src = TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(200, json={"data": {"000819.SZ": columns(bars)}})),
                           key=lambda: "k")
    m = src.minutes(["CN:000819"], DAY)
    assert m.select("at", "open", "volume").rows() == [(datetime(2026, 10, 9, 9, 31), 11.86, 106100.0),
                                                       (datetime(2026, 10, 9, 9, 32), 11.84, 0.0),
                                                       (datetime(2026, 10, 9, 9, 33), 11.85, 54600.0)]


def test_a_stocks_days_of_minutes_come_in_one_call_each_day_on_its_own():
    # a minute K-line's history (10-12): one call for a stock's days — measured, one call brought 1-minute bars of 15
    # days — each day with its own opening auction and its own placeholders before its first trade
    d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
    bars = [(ms(9, 30, d1), 10.0, 10.0, 10.0, 10.0, 100, 1000.0), (ms(9, 31, d1), 10.0, 10.1, 9.9, 10.05, 200, 2010.0),
            (ms(9, 30, d2), 9.9, 9.9, 9.9, 9.9, 0, 0.0),   # no auction match on d2: a placeholder, left out on that day only
            (ms(9, 31, d2), 9.95, 10.0, 9.9, 9.98, 300, 2994.0)]
    seen = []
    src = TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(200, json={"data": {"600000.SH": columns(bars)}}), seen),
                           key=lambda: "k")
    m = src.minutes_between(["CN:600000"], d1, d2)
    assert len(seen) == 1 and seen[0].url.params["period"] == "1m" and seen[0].url.params["adjust"] == "none"
    assert m.select("at", "open", "close", "volume").rows() == [
        (datetime(2026, 10, 8, 9, 30), 10.0, 10.05, 30000.0),       # the auction joins the first minute
        (datetime(2026, 10, 9, 9, 30), 9.95, 9.98, 30000.0)]


def test_the_day_is_asked_for_by_its_hours_and_more_than_100_symbols_go_in_turns():
    seen = []
    ids = [f"CN:{600000 + k:06d}" for k in range(150)]
    src = TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(200, json={"data": {}}), seen), key=lambda: "k")
    assert src.minutes(ids, DAY, indexes=["CN:IDX:000001"]).is_empty()
    asked = [r.url.params["symbols"].split(",") for r in seen]
    assert [len(a) for a in asked] == [100, 51] and asked[1][-1] == "000001.SH"
    p = seen[0].url.params
    assert (seen[0].url.path, p["period"], int(p["start_time"]), int(p["end_time"])) == (
        "/v1/klines/batch", "1m", ms(9, 0), ms(15, 30))
    # the exchange's prices: its K-lines are 比例前复权 unless asked — a day before a later ex-date came out ×0.989
    # (000680, measured 2026-10-10) against the daily bars and the close before, which are as traded
    assert p["adjust"] == "none"
    assert seen[0].headers["x-api-key"] == "k"


def test_calls_keep_under_the_k_line_batchs_30_a_minute_however_many_sources_ask():
    # measured 2026-10-09: "请求频率超限 (30/min)" — a poll, the days before and a restart all share one budget of 25
    from wb.data.http import RateLimiter
    now, slept = [0.0], []
    budget = RateLimiter(25, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    answer = serve(lambda r: httpx.Response(200, json={"data": {}}))
    ids = [f"CN:{600000 + k:06d}" for k in range(1300)]                     # 13 calls each time
    TickFlowRealtime("CN", transport=answer, key=lambda: "k", limiter=budget).minutes(ids, DAY)
    TickFlowRealtime("CN", transport=answer, key=lambda: "k", limiter=budget).minutes(ids, DAY)
    assert slept == [60.0]                                                  # the 26th call waits out the minute


def test_bars_of_another_day_are_left_out():
    bars = [(ms(15, 0, date(2026, 10, 8)), 9.7, 9.7, 9.7, 9.7, 100, 970.0), (ms(9, 31), 9.71, 9.72, 9.7, 9.72, 10, 97.1)]
    src = TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(200, json={"data": {"600000.SH": columns(bars)}})),
                           key=lambda: "k")
    assert src.minutes(["CN:600000"], DAY)["at"].to_list() == [datetime(2026, 10, 9, 9, 30)]


def test_a_refusal_says_why_in_its_words_and_never_carries_the_key():
    no = {"code": "NO_INTRADAY_BATCH_PERMISSION", "message": "无日内分时查询批量查询权限"}
    src = TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(403, json=no)), key=lambda: "secret-key")
    with pytest.raises(TickFlowError, match="无日内分时查询批量查询权限") as e:
        src.minutes(["CN:600000"], DAY)
    assert "secret-key" not in str(e.value)
    with pytest.raises(TickFlowError, match="key"):
        TickFlowRealtime("CN", transport=serve(lambda r: httpx.Response(401, json={})), key=lambda: "k").minutes(["CN:600000"], DAY)
    with pytest.raises(TickFlowError, match="TickFlow"):
        TickFlowRealtime("CN", key=lambda: None).minutes(["CN:600000"], DAY)


def test_it_is_registered_as_a_minute_source_that_fills_the_days_before():
    registry.load_all()
    tf = registry.REALTIME["tickflow_rt"]
    assert (tf.kind, tf.delay, tf.approx_volume, tf.history_days) == ("minutes", 0, False, 19)
    # a poll is three calls with the indexes, every 30 s; the days before take three a minute: well under 30 a minute
    assert (tf.max_listings, tf.poll_seconds) == (297, 30)


def test_the_prod_cn_source_is_tickflow_once_its_key_is_set(monkeypatch, tmp_path):
    from wb.config import Paths
    from wb.jobs import live
    prod, test = Paths(root=tmp_path, profile="prod"), Paths(root=tmp_path, profile="test")
    monkeypatch.setattr(live, "secret_source", lambda name: None)
    assert live.source_for(prod, "CN").name == "tushare_rt"
    monkeypatch.setattr(live, "secret_source", lambda name: ".env" if name == "tickflow_api_key" else None)
    assert [live.source_for(prod, "CN").name, live.source_for(prod, "JP").name, live.source_for(test, "CN").name] == [
        "tickflow_rt", "tachibana", "yfinance_rt"]


def test_quotes_are_the_day_so_far_as_traded_100_a_call_off_the_k_line_budget():
    # POST /v1/quotes (design 2026-10-10 §5, the fast path): the day's open, high and low and the price now, stamped by
    # the exchange's clock; a price of 0 is one not traded yet (before the auction)
    seen = []

    def answer(r):
        return httpx.Response(200, json={"data": [
            {"symbol": s, "region": "CN", "last_price": 10.0, "prev_close": 9.8, "open": 0.0 if s == "600001.SH" else 9.9,
             "high": 10.1, "low": 9.85, "volume": 1, "amount": 1.0, "timestamp": ms(10, 15) + 3000} for s in json.loads(r.content)["symbols"]]})

    class Refuse:
        def acquire(self):
            raise AssertionError("the quotes endpoint is not the K-line batch's: not on its 30 a minute")
    src = TickFlowRealtime("CN", transport=serve(answer, seen), key=lambda: "k", limiter=Refuse())
    q = src.quotes([f"CN:{600000 + k:06d}" for k in range(150)])
    assert [(r.method, r.url.path, len(json.loads(r.content)["symbols"])) for r in seen] == [("POST", "/v1/quotes", 100), ("POST", "/v1/quotes", 50)]
    assert q.height == 150 and q.row(0, named=True) == {"listing_id": "CN:600000", "at": datetime(2026, 10, 9, 10, 15, 3), "open": 9.9,
                                                        "high": 10.1, "low": 9.85, "last": 10.0, "prev_close": 9.8,
                                                        "volume": 100.0, "amount": 1.0}   # lots × 100: shares; yuan
    assert q.row(1, named=True)["open"] is None


class Refuse:
    def acquire(self):
        raise AssertionError("the quotes endpoint is not the K-line batch's: not on its 30 a minute")


def test_the_whole_market_comes_in_one_call_off_the_k_line_budget():
    # POST /v1/quotes by the universe CN_Equity_A (design 2026-10-12 §1: 5,572 A shares a call, measured 2026-10-12):
    # prices carry float noise (rounded to 4 places), a price of 0 is one not traded yet (an auction not matched),
    # volume is in lots; a stock not traded since the day before keeps its old stamp; anything not an A share is left out
    seen = []

    def answer(r):
        return httpx.Response(200, json={"data": [
            {"symbol": "600000.SH", "last_price": 9.739999999999998, "prev_close": 9.54, "open": 9.59, "high": 9.739999999999998,
             "low": 9.55, "volume": 1234, "amount": 1.2e6, "timestamp": ms(10, 15) + 3000, "ext": {"name": "浦发银行"}},
            {"symbol": "920001.BJ", "last_price": 10.9, "prev_close": 10.87, "open": 0.0, "high": 0.0, "low": 0.0, "volume": 0,
             "amount": 0.0, "timestamp": ms(9, 20)},
            {"symbol": "000001.SZ", "last_price": 11.59, "prev_close": 11.78, "open": 11.78, "high": 11.89, "low": 11.58,
             "volume": 1078106, "amount": 1.26e9, "timestamp": ms(15, 0, date(2026, 10, 8))},
            {"symbol": "IF2510.CFE", "last_price": 4500.0, "timestamp": ms(10, 15)}]})
    src = TickFlowRealtime("CN", transport=serve(answer, seen), key=lambda: "k", limiter=Refuse())
    q = src.whole_market()
    assert [(r.method, r.url.path, json.loads(r.content)) for r in seen] == [("POST", "/v1/quotes", {"universes": ["CN_Equity_A"]})]
    assert q["listing_id"].to_list() == ["CN:600000", "CN:920001", "CN:000001"]
    a = q.row(0, named=True)
    assert (a["last"], a["high"], a["volume"], a["amount"], a["at"]) == (9.74, 9.74, 123400.0, 1.2e6, datetime(2026, 10, 9, 10, 15, 3))
    b = q.row(1, named=True)
    assert (b["open"], b["high"], b["low"], b["last"], b["volume"]) == (None, None, None, 10.9, 0.0)
    assert q.row(2, named=True)["at"] == datetime(2026, 10, 8, 15, 0)


def test_symbols_map_back_to_listings_only_for_a_shares():
    from wb.realtime.providers.tickflow_rt import listing_of
    assert [listing_of(s) for s in ("600000.SH", "000001.SZ", "920001.BJ", "IF2510.CFE", "00700.HK", "6000.SH", "")] == [
        "CN:600000", "CN:000001", "CN:920001", None, None, None, None]


def test_days_are_the_daily_bars_as_traded_in_shares_100_symbols_a_call():
    # GET /v1/klines/batch at 1d, adjust=none: verified 2026-10-11 against the Tushare daily bars — 25,228 Beijing
    # exchange stock-days agree on every price, volume (lots) × 100 within 100 shares on 99.45%
    seen = []

    def answer(r):
        syms = r.url.params["symbols"].split(",")
        return httpx.Response(200, json={"data": {s: columns([(ms(0, 0, date(2026, 10, 8)), 10.0, 10.5, 9.9, 10.2, 1234, 1.26e6),
                                                             (ms(0, 0, date(2026, 10, 9)), 10.2, 10.4, 10.1, 10.3, 1000, 1.03e6)])
                                                  for s in syms}})
    tf = TickFlowRealtime(transport=serve(answer, seen), key=lambda: "k")
    ids = [f"CN:{600000 + i}" for i in range(149)] + ["CN:920021"]
    got = tf.days(ids, date(2026, 10, 9), date(2026, 10, 9))
    assert [(r.url.params["period"], r.url.params["adjust"], len(r.url.params["symbols"].split(","))) for r in seen] == [
        ("1d", "none", 100), ("1d", "none", 50)]
    assert got.height == 150 and got["date"].unique().to_list() == [date(2026, 10, 9)]   # the asked days only
    row = got.filter(pl.col("listing_id") == "CN:920021").row(0, named=True)
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"], row["amount"]) == (10.2, 10.4, 10.1, 10.3, 100_000, 1.03e6)
