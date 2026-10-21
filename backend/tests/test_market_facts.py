"""市场看板的「当天的底」(design 2026-10-12 §8.1): what each A share is before the open — its close before as traded,
today's limit prices, its limit-up run, its industries, the closes its 5- and 20-bar returns need — and the indexes'
lines; what leans on yesterday is unknown when the data snapshot does not reach yesterday."""
from datetime import date, timedelta

import polars as pl
import pytest

from tests.fakes import tushare_server
from wb.data.http import HttpClient
from wb.data.providers.tushare import Tushare
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l1.compiler import Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.run import evaluate
from wb.market.facts import build_facts, fetch_limits, limits_complete

AS_OF = date(2026, 9, 30)      # the demo data's last day
NEXT = date(2026, 10, 1)


@pytest.fixture
def demo(paths):
    write(paths, 320)
    st = Store(paths, "CN")
    days = [AS_OF + timedelta(days=k) for k in range(1, 12)]   # the calendar ahead, as the real one is
    st.publish({"calendar": pl.DataFrame({"date": days, "is_open": [d.weekday() < 5 for d in days], "source": ["fake"] * len(days)})},
               as_of=AS_OF)
    return paths, st


def test_facts_carry_each_stocks_close_run_and_returns(demo):
    paths, st = demo
    f = build_facts(paths, "CN", NEXT)
    assert (f.day, f.as_of, f.prev_day, f.stale) == (NEXT, AS_OF, AS_OF, False)
    bars = build_frame(st, "CN", "test", {"close", "raw_close", "amount"}).sort("listing_id", "date")
    comp = Compiler("CN", available=available_columns(st))
    comp.add("streak", "LIMIT_UP_STREAK")
    streak = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program()).sort("listing_id", "date")
    for lid in ("CN:600519", "CN:000001"):
        b = bars.filter(pl.col("listing_id") == lid)
        row = f.stocks.filter(pl.col("listing_id") == lid).row(0, named=True)
        assert row["prev"] == b["raw_close"][-1] and row["adj_last"] == b["close"][-1]
        assert row["c4"] == b["close"][-5] and row["c5"] == b["close"][-6]
        assert row["c19"] == b["close"][-20] and row["c20"] == b["close"][-21]
        assert row["amount5"] == pytest.approx(b["amount"].tail(5).mean())
        assert row["streak"] == streak.filter(pl.col("listing_id") == lid)["streak"][-1]
        assert row["last_day"] == AS_OF and row["name"] is not None
    assert f.amount_before == pytest.approx(st.read("bars_1d").filter(pl.col("date") == AS_OF)["amount"].sum())


def test_today_limits_and_stored_limits_and_none(demo):
    paths, st = demo
    lim = pl.DataFrame({"listing_id": ["CN:600519", "CN:000001"], "exchange": ["SH", "SZ"], "up_limit": [1700.0, 12.0],
                        "down_limit": [1400.0, 10.0]})
    today = build_facts(paths, "CN", NEXT, limits=lim)
    assert today.limits == "today"
    assert today.stocks.filter(pl.col("listing_id") == "CN:600519")["up_limit"][0] == 1700.0
    stored = build_facts(paths, "CN", AS_OF)                    # a stored day: its own stk_limit prices
    assert stored.limits == "stored" and stored.prev_day < AS_OF
    day_bars = st.read("bars_1d").filter(pl.col("date") == AS_OF).select("listing_id", "up_limit").sort("listing_id")
    assert stored.stocks.select("listing_id", "up_limit").sort("listing_id").equals(day_bars)
    none = build_facts(paths, "CN", NEXT)
    assert none.limits == "none" and none.stocks["up_limit"].null_count() == none.stocks.height
    jp = build_facts(paths, "JP", NEXT)
    assert jp.limits == "none" and jp.stocks.height == 3


def test_a_stale_snapshot_leaves_yesterday_unknown(demo):
    paths, _ = demo
    f = build_facts(paths, "CN", date(2026, 10, 6))            # the update of 10-01 … 10-05 never came
    assert f.stale and f.prev_day == date(2026, 10, 5)
    assert f.stocks["streak"].null_count() == f.stocks.height and f.stocks["lu_prev"].null_count() == f.stocks.height
    assert f.amount_before is None and f.top_ids == ()
    assert f.stocks["prev"].null_count() == 0                    # the last close known still is one
    # the windows and the 5-day turnover need the missing days: unknown, not shifted by them
    assert all(f.stocks[c].null_count() == f.stocks.height for c in ("c4", "c5", "c19", "c20", "amount5"))
    assert f.indexes and all(x["prev"] is None and x["sum59"] is None and x["sum199"] is None and x["ma60_prev"] is None
                             and x["ma60_5"] is None for x in f.indexes.values())


def test_yesterdays_highest_board_and_whether_a_stock_closed_at_the_limit(demo):
    paths, st = demo
    bars = st.read("bars_1d").filter(pl.col("listing_id") == "CN:600519").sort("date").tail(2)
    st.publish({"bars_1d": bars.with_columns(pl.col("up_limit").alias("close"), pl.col("up_limit").alias("high"))}, as_of=AS_OF)
    f = build_facts(paths, "CN", NEXT)
    row = f.stocks.filter(pl.col("listing_id") == "CN:600519").row(0, named=True)
    assert (row["streak"], row["lu_prev"]) == (2, True)
    assert f.top_ids == ("CN:600519",)
    assert f.stocks.filter(pl.col("listing_id") == "CN:000001")["lu_prev"][0] is False


def test_level_two_comes_from_the_industry_rows_when_they_have_it(demo):
    paths, st = demo
    assert sorted(build_facts(paths, "CN", NEXT).stocks["l2"].to_list()) == ["白酒Ⅱ", "股份制银行Ⅱ"]   # the demo's 申万二级
    st.publish({"industry": pl.DataFrame({
        "listing_id": ["CN:600519", "CN:600519"], "scheme": ["SW2021_L1"] * 2, "code": ["801120.SI"] * 2, "name": ["食品饮料"] * 2,
        "in_date": [date(2001, 8, 27), date(2026, 9, 29)], "out_date": [date(2026, 9, 28), None],
        "l2_code": ["801124.SI", "801125.SI"], "l2_name": ["食品加工", "白酒Ⅱ"], "source": ["tushare"] * 2})}, as_of=AS_OF)
    row = build_facts(paths, "CN", NEXT).stocks.filter(pl.col("listing_id") == "CN:600519").row(0, named=True)
    assert (row["l1"], row["l2"]) == ("食品饮料", "白酒Ⅱ")                         # the spell its last bar is in


def test_index_facts_give_the_live_lines(demo):
    paths, st = demo
    closes = st.read("index_bars_1d").filter(pl.col("index_id") == "CN:IDX:000001").sort("date")["close"].to_list()
    day = st.read("index_bars_1d").filter(pl.col("index_id") == "CN:IDX:000001").sort("date")["date"][-1]
    f = build_facts(paths, "CN", day)                           # bars before the last day: the line through it
    i = f.indexes["CN:IDX:000001"]
    assert i["prev"] == closes[-2]
    assert (i["sum59"] + closes[-1]) / 60 == pytest.approx(sum(closes[-60:]) / 60)
    assert (i["sum199"] + closes[-1]) / 200 == pytest.approx(sum(closes[-200:]) / 200)
    assert i["ma60_prev"] == pytest.approx(sum(closes[-61:-1]) / 60)
    assert i["ma60_5"] == pytest.approx(sum(closes[-65:-5]) / 60)          # the line five bars before `day`
    assert "box" in i


def test_fetch_limits_maps_codes_and_keeps_the_exchange():
    rows = [{"ts_code": "600000.SH", "up_limit": 10.49, "down_limit": 8.59}, {"ts_code": "920001.BJ", "up_limit": 99999.99, "down_limit": 0.0},
            {"ts_code": "200011.SZ", "up_limit": 1.1, "down_limit": 0.9}]
    t = Tushare(token="t", http=HttpClient(10_000, transport=tushare_server({"stk_limit": rows}), sleep=lambda s: None), sleep=lambda s: None)
    got = fetch_limits(date(2026, 10, 12), t)
    assert got.rows() == [("CN:600000", "SH", 10.49, 8.59), ("CN:920001", "BJ", 99999.99, 0.0), ("CN:200011", "SZ", 1.1, 0.9)]


def test_limits_complete_needs_three_exchanges_and_two_equal_fetches():
    def lim(exchanges, up=10.0):
        return pl.DataFrame({"listing_id": [f"CN:{k:06d}" for k in range(len(exchanges))], "exchange": exchanges,
                             "up_limit": [up] * len(exchanges), "down_limit": [9.0] * len(exchanges)})
    full = lim(["SH", "SZ", "BJ"])
    assert not limits_complete(None, full)                      # once is not enough
    assert limits_complete(full, full)
    assert not limits_complete(lim(["SH", "SZ"]), lim(["SH", "SZ"]))   # 北交所 not there yet
    assert not limits_complete(full, lim(["SH", "SZ", "BJ"], up=11.0))  # still changing
