"""市场看板的数 (design 2026-10-12 §3–§6) from one cross-section of the market and the day's base: limit-ups by the
engine's own definitions, the ladder, the distribution, industries and their quadrant, the indexes' lines — the
same from a quote at the close as from the day's bars."""
from datetime import date, datetime
from statistics import median

import polars as pl
import pytest

from wb.market.board import TREND, compute, from_bars, possible, tape_row, traded, trend
from wb.market.facts import STOCKS, Facts

DAY, PREV = date(2026, 10, 12), date(2026, 10, 9)
AT = datetime(2026, 10, 12, 10, 42, 30)
SH = "CN:IDX:000001"
CHIP = dict(c4=1 / 1.05, c19=1 / 1.10, c5=1 / 1.04, c20=1 / 1.12)     # closes back as shares of the last one
BANK = dict(c4=1 / 0.98, c19=1 / 0.95, c5=1 / 0.99, c20=1 / 0.96)
MEDIA = dict(c4=1.0, c19=1.0, c5=1.0, c20=1.0)
# id: (prev, up, down, high, last, volume, streak, lu_prev, industry (l1, l2), back)
MARKET = {
    "CN:000001": (10.0, 11.0, 9.0, 11.0, 11.0, 1e6, 2, True, ("电子", "半导体"), CHIP),      # a third board, sealed
    "CN:000002": (20.0, 22.0, 18.0, 22.0, 22.0, 1e6, 0, False, ("电子", "半导体"), CHIP),    # a first board
    "CN:000003": (30.0, 33.0, 27.0, 33.0, 33.0, 1e6, 1, True, ("传媒", "出版"), MEDIA),      # promoted to two
    "CN:000004": (5.0, 5.5, 4.5, 5.5, 5.3, 1e6, 0, False, ("电子", "半导体"), CHIP),         # touched, broken
    "CN:000005": (8.0, 8.8, 7.2, 8.8, 8.4, 1e6, 1, True, ("传媒", "出版"), MEDIA),           # yesterday's, broken
    "CN:000006": (10.0, 11.0, 9.0, 9.5, 9.0, 1e6, 0, False, ("银行", "股份制银行"), BANK),   # limit down
    "CN:000007": (7.0, 7.7, 6.3, 7.1, 7.0, 1e6, 0, False, ("银行", "股份制银行"), BANK),     # flat
    "CN:000008": (50.0, 55.0, 45.0, 51.0, 51.0, 1e6, 0, False, ("电子", "半导体"), CHIP),    # +2%
    "CN:000009": (25.0, 27.5, 22.5, 25.0, 24.0, 1e6, 0, False, ("银行", "股份制银行"), BANK),  # −4%
    "CN:000010": (12.0, 13.2, 10.8, 12.0, 12.0, 0.0, 0, False, ("电子", "半导体"), CHIP),    # not traded (volume 0)
    "CN:000011": (9.0, 9.9, 8.1, 9.2, 9.2, 1e6, 0, False, ("电子", "半导体"), CHIP),         # yesterday's stamp
    "CN:000012": (40.0, 999999.999, 0.01, 48.0, 48.0, 1e6, 0, False, ("传媒", "出版"), MEDIA),  # a new listing, no limit
    "CN:000013": (15.0, 16.5, 13.5, 15.0, 14.55, 1e6, 1, True, ("银行", "股份制银行"), BANK),  # yesterday's, −3% today
    "CN:000014": (6.0, 6.6, 5.4, 6.0, 6.0, 0.0, 1, True, ("银行", "股份制银行"), BANK),       # yesterday's, suspended
}
INDEX = {SH: {"prev": 3800.0, "sum59": 59 * 3900.0, "sum199": 199 * 3700.0, "ma60_prev": 3900.0, "ma60_5": 3910.0,
              "box": {"top": 3967.6, "bottom": 3781.6}}}


def facts(limits: str = "today", **over) -> Facts:
    rows = []
    for lid, (prev, up, down, _h, _l, _v, streak, lu, (l1, l2), back) in MARKET.items():
        rows.append({"listing_id": lid, "name": f"股{lid[-2:]}", "last_day": PREV, "prev": prev, "adj_last": prev * 2,
                     **{k: prev * 2 * v for k, v in back.items()}, "streak": streak, "lu_prev": lu, "l1": l1, "l2": l2,
                     "amount5": 2e8, "up_limit": up if limits != "none" else None, "down_limit": down if limits != "none" else None})
    base = dict(market="CN", day=DAY, snapshot="s", as_of=PREV, prev_day=PREV, stale=False,
                stocks=pl.DataFrame(rows, schema=STOCKS), limits=limits, amount_before=2e12, indexes=INDEX, top_ids=("CN:000001",))
    return Facts(**(base | over))


def quotes() -> pl.DataFrame:
    rows = []
    for lid, (prev, _u, _d, high, last, volume, *_rest) in MARKET.items():
        at = datetime(2026, 10, 9, 15, 0) if lid == "CN:000011" else AT
        rows.append({"listing_id": lid, "at": at, "open": prev, "high": high, "low": min(prev, last), "last": last,
                     "prev_close": prev, "volume": volume, "amount": 1e8 if volume else 0.0})
    return pl.DataFrame(rows)


def board(**kw):
    return compute(traded(quotes(), DAY), facts(**kw), AT, share=0.5, indexes_now={SH: 3812.4})


def test_limit_counts_rates_and_the_ladder():
    b = board()
    lim = b["limits"]
    assert (lim["known"], lim["limit_up"], lim["broken"], lim["limit_down"]) == (True, 3, 2, 1)
    assert lim["seal_rate"] == pytest.approx(0.6)
    assert (lim["pool"], lim["promoted"], lim["promo_rate"]) == (5, 2, None)            # under 10: no rate
    assert (lim["height"], lim["top_sealed"]) == (3, True)
    assert [(r["level"], r["sealed"], r["broken"]) for r in b["ladder"]] == [(3, 1, 0), (2, 1, 1), (1, 1, 1)]
    assert [n["listing_id"] for n in b["ladder"][0]["names"]] == ["CN:000001"] and b["ladder"][1]["names"] == []
    assert {r["listing_id"] for r in b["limit_list"]} == {"CN:000001", "CN:000002", "CN:000003", "CN:000004", "CN:000005"}
    assert sorted(b["sealed_ids"]) == ["CN:000001", "CN:000002", "CN:000003"] and sorted(b["broken_ids"]) == ["CN:000004", "CN:000005"]


def test_a_rate_needs_a_pool_of_ten():
    many = facts().stocks.with_columns(pl.lit(True).alias("lu_prev"))                  # all 14 were limit-ups yesterday
    b = compute(traded(quotes(), DAY), facts(stocks=many), AT, share=0.5)
    assert (b["limits"]["pool"], b["limits"]["promoted"]) == (14, 3)
    assert b["limits"]["promo_rate"] == pytest.approx(3 / 14)


def test_untraded_stocks_count_nowhere():
    b = board()
    assert (b["traded"], b["up"], b["flat"], b["down"]) == (11, 7, 1, 3)               # 000010, 000011, 000014 out
    assert b["up"] + b["down"] + b["flat"] == 11
    assert "CN:000010" not in b["sealed_ids"] + b["broken_ids"]


def test_float_noise_and_no_limit_placeholders():
    q = quotes().with_columns(pl.when(pl.col("listing_id") == "CN:000001").then(10.999999999999998).otherwise(pl.col("last")).alias("last"))
    b = compute(traded(q, DAY), facts(), AT, share=0.5)
    assert "CN:000001" in b["sealed_ids"]                                               # within 1e-6 of the limit
    assert "CN:000012" not in b["sealed_ids"] + b["broken_ids"]                          # +20% with no limit: never at it


def test_unknown_limits_are_null_not_zero():
    b = board(limits="none")
    assert b["limits"]["known"] is False
    assert all(b["limits"][k] is None for k in ("limit_up", "broken", "limit_down", "seal_rate", "pool", "promoted", "promo_rate", "height"))
    assert b["ladder"] == [] and b["sealed_ids"] == []
    assert all(r["limit_up"] is None for lv in ("l1", "l2") for r in b["industries"][lv])  # 日股's 業種 too


def test_industries_today_and_against_the_market():
    b = board()
    l2 = {r["name"]: r for r in b["industries"]["l2"]}
    chips, banks, media = l2["半导体"], l2["股份制银行"], l2["出版"]
    assert (chips["members"], chips["traded"], chips["up"], chips["limit_up"]) == (6, 4, 4, 2)
    assert chips["chg"] == pytest.approx(0.08) and chips["up_ratio"] == 1.0
    assert banks["chg"] == pytest.approx(-0.035) and banks["up_ratio"] == 0.0
    assert media["small"] is True and chips["small"] is False
    r20 = lambda chg, c19: (1 + chg) * (1 / c19) - 1   # noqa: E731
    all20 = sorted([r20(0.10, CHIP["c19"])] * 2 + [r20(0.06, CHIP["c19"]), r20(0.02, CHIP["c19"])]
                   + [r20(c, BANK["c19"]) for c in (-0.10, 0.0, -0.04, -0.03)] + [0.10, 0.05, 0.20])
    chips20 = sorted([r20(0.10, CHIP["c19"])] * 2 + [r20(0.06, CHIP["c19"]), r20(0.02, CHIP["c19"])])
    assert chips["rel20"] == pytest.approx((chips20[1] + chips20[2]) / 2 - all20[5])
    assert b["top"]["name"] == "半导体" and b["top"]["second"] == pytest.approx(-0.035)     # 出版 is small
    assert "distribution" not in b and "leader" not in b
    for r in b["industries"]["l1"] + b["industries"]["l2"]:
        assert not {"x", "y", "x0", "y0"} & r.keys() and {"rel5", "rel20", "prior15", "tag", "rank", "parent"} <= r.keys()


def test_the_15_bars_before_the_last_5_against_the_market():
    b = board()
    l2 = {r["name"]: r for r in b["industries"]["l2"]}
    p15 = lambda back: back["c4"] / back["c19"] - 1          # noqa: E731  closes 5 and 20 bars before today
    market = median([p15(CHIP)] * 4 + [p15(BANK)] * 4 + [p15(MEDIA)] * 3)   # the 11 traded
    assert l2["半导体"]["prior15"] == pytest.approx(p15(CHIP) - market)
    assert l2["股份制银行"]["prior15"] == pytest.approx(p15(BANK) - market)
    now5 = lambda chg, back: (1 + chg) / back["c4"] - 1        # noqa: E731
    m5 = median([now5(c, CHIP) for c in (0.10, 0.10, 0.06, 0.02)] + [now5(c, BANK) for c in (-0.10, 0.0, -0.04, -0.03)]
                + [now5(c, MEDIA) for c in (0.10, 0.05, 0.20)])
    assert l2["半导体"]["rel5"] == pytest.approx(median([now5(c, CHIP) for c in (0.10, 0.10, 0.06, 0.02)]) - m5)


@pytest.mark.parametrize("rel5,prior15,tag", [
    (0.015, 0.025, "一直强"), (0.015, 0.0249, "转强"), (0.015, -0.03, "转强"), (0.0149, 0.05, None),
    (-0.015, 0.025, "转弱"), (-0.015, 0.0, "转弱"), (-0.015, -0.025, "一直弱"), (-0.0149, -0.05, None), (None, 0.03, None)])
def test_the_tag_reads_the_last_5_against_the_15_before(rel5, prior15, tag):
    assert trend(rel5, prior15, 0.015, 0.025) == tag


def test_each_level_has_its_own_thresholds():
    assert (TREND[("CN", "l1")], TREND[("CN", "l2")], TREND[("JP", "l1")]) == ((0.015, 0.025), (0.02, 0.035), (0.01, 0.02))
    b = board()
    tag = lambda lv, name: next(r["tag"] for r in b["industries"][lv] if r["name"] == name)   # noqa: E731
    # the banks: 5 bars well under the market, the 15 before −3.1%: past 一级's −2.5%, not 二级's −3.5%
    assert (tag("l1", "银行"), tag("l2", "股份制银行")) == ("一直弱", "转弱")
    assert (tag("l1", "电子"), tag("l2", "半导体")) == ("一直强", "一直强")
    assert tag("l2", "出版") is None                            # three members: no rank, no tag


def test_ranks_skip_small_and_unknown():
    b = board()
    assert [(r["name"], r["rank"]) for r in b["industries"]["l2"]] == [("半导体", 1), ("股份制银行", 2), ("出版", None)]
    assert [(r["name"], r["rank"]) for r in b["industries"]["l1"]] == [("电子", 1), ("银行", 2), ("传媒", None)]
    unknown = compute(traded(quotes(), DAY).with_columns(
        pl.when(pl.col("listing_id").is_in(["CN:000006", "CN:000007", "CN:000009", "CN:000013"])).then(None)
        .otherwise(pl.col("prev_close")).alias("prev_close")), facts(), AT, share=0.5)
    banks = next(r for r in unknown["industries"]["l2"] if r["name"] == "股份制银行")
    assert banks["chg"] is None and banks["rank"] is None and unknown["industries"]["l2"][-1]["name"] == "股份制银行"


def test_a_l2_names_its_l1():
    b = board()
    assert {r["name"]: r["parent"] for r in b["industries"]["l2"]} == {"半导体": "电子", "股份制银行": "银行", "出版": "传媒"}
    assert all(r["parent"] is None for r in b["industries"]["l1"])


def test_amount_ratio_and_index_lines():
    b = board()
    chips = next(r for r in b["industries"]["l2"] if r["name"] == "半导体")
    assert chips["amount_ratio"] == pytest.approx(4e8 / (4 * 2e8 * 0.5))
    assert b["amount"] == 11e8 and b["projected"] == pytest.approx(22e8)                # 10:42: ÷ the share done
    early = compute(traded(quotes(), DAY), facts(), datetime(2026, 10, 12, 10, 0), share=0.3)
    assert early["projected"] is None                                                   # before 10:30 the clock is biased
    sh = b["indexes"][SH]
    ma60 = (59 * 3900.0 + 3812.4) / 60
    assert sh["last"] == 3812.4 and sh["chg"] == pytest.approx(3812.4 / 3800 - 1)
    assert sh["dist60"] == pytest.approx(3812.4 / ma60 - 1) and sh["state"] == 0            # under the 60-day line, over the 200
    assert sh["to_top"] == pytest.approx(3812.4 / 3967.6 - 1) and sh["to_bottom"] == pytest.approx(3812.4 / 3781.6 - 1)


def test_an_index_changes_against_its_own_close_before():
    """The quote's own close before, as for stocks — not the data's last close: with an update missed that was two days'
    change (stale-1012: 上证 −0.38% against 10-08's close where it was −0.43% against 10-09's). The lines that need
    the missing day are unknown; the box is a price, today's price against it stands."""
    stale = {SH: {**INDEX[SH], "prev": None, "sum59": None, "sum199": None, "ma60_prev": None, "ma60_5": None}}
    b = compute(traded(quotes(), DAY), facts(stale=True, indexes=stale), AT, share=0.5, indexes_now={SH: 3812.4}, indexes_prev={SH: 3830.0})
    sh = b["indexes"][SH]
    assert sh["chg"] == pytest.approx(3812.4 / 3830.0 - 1)
    assert sh["dist60"] is None and sh["state"] is None and sh["to_top"] == pytest.approx(3812.4 / 3967.6 - 1)
    fresh = compute(traded(quotes(), DAY), facts(), AT, share=0.5, indexes_now={SH: 3812.4}, indexes_prev={SH: 3801.0})
    assert fresh["indexes"][SH]["chg"] == pytest.approx(3812.4 / 3801.0 - 1)            # the quote's, whenever it has one


def test_the_close_board_from_bars_equals_the_quote_board():
    q = traded(quotes(), DAY).with_columns(pl.lit(datetime(2026, 10, 12, 15, 0)).alias("at"))
    bars = q.select("listing_id", pl.lit(DAY).alias("date"), "open", "high", "low", pl.col("last").alias("close"),
                    pl.col("prev_close").alias("pre_close"), "volume", "amount")
    at = datetime(2026, 10, 12, 15, 0)
    assert compute(from_bars(bars, DAY), facts(), at, share=1.0) == compute(q, facts(), at, share=1.0)


def test_a_tape_row_carries_what_the_strip_and_the_events_read():
    row = tape_row(board(), 72)
    assert (row["i"], row["limit_up"], row["broken"], row["seal_rate"], row["height"], row["top_sealed"]) == (72, 3, 2, 0.6, 3, True)
    assert (row["top_name"], row["sh_last"]) == ("半导体", 3812.4)
    assert (row["top1_name"], row["top1_chg"], row["second1_chg"]) == ("电子", pytest.approx(0.08), pytest.approx(-0.035))
    assert sorted(row["sealed_ids"]) == ["CN:000001", "CN:000002", "CN:000003"]
    assert isinstance(row["ladder"], str)


def test_each_industry_names_its_three_leaders_limit_ups_first():
    chips = next(r for r in board()["industries"]["l2"] if r["name"] == "半导体")
    assert [(x["listing_id"], x["sealed"]) for x in chips["leaders"]] == [("CN:000001", True), ("CN:000002", True), ("CN:000004", False)]
    assert chips["leaders"][0]["name"] == "股01" and chips["leaders"][2]["chg"] == pytest.approx(0.06)


def test_a_close_without_the_exchanges_close_before_takes_the_day_befores_in_todays_units():
    # 日股: J-Quants gives no close before — it is the last close in today's share units (a 2:1 split halves it), as
    # the chart's base price; a stock off the day before goes back to its last bar
    E = date(2026, 10, 7)
    rows = [("JP:1", E, 990.0, 1.0), ("JP:1", PREV, 1000.0, 1.0), ("JP:1", DAY, 520.0, 2.0),
            ("JP:2", PREV, 100.0, 1.0), ("JP:2", DAY, 99.0, 1.0),
            ("JP:3", E, 50.0, 1.0), ("JP:3", DAY, 51.0, 1.0)]
    bars = pl.DataFrame([{"listing_id": lid, "date": d, "open": c, "high": c, "low": c, "close": c, "pre_close": None,
                          "volume": 1e5, "amount": 1e8, "adj_factor": a} for lid, d, c, a in rows],
                        schema_overrides={"pre_close": pl.Float64})
    xs = from_bars(bars, DAY).sort("listing_id")
    assert xs["prev_close"].to_list() == [500.0, 100.0, 50.0]
    assert xs["last"].to_list() == [520.0, 99.0, 51.0] and set(xs["at"].dt.date().to_list()) == {DAY}


def test_a_change_that_cannot_be_known_is_neither_up_nor_down_nor_flat():
    q = traded(quotes(), DAY).with_columns(pl.when(pl.col("listing_id") == "CN:000008").then(None)
                                           .otherwise(pl.col("prev_close")).alias("prev_close"))
    b = compute(q, facts(), AT, share=0.5, indexes_now={SH: 3812.4})
    assert (b["up"], b["down"], b["flat"]) == (6, 3, 1) and b["traded"] == 11


def test_a_price_beyond_todays_limits_is_not_a_trade():
    # 10-13 09:25: TickFlow's whole-market quote gave 1,750 沪主板, 创业板 and 科创板 stocks twice their auction's price
    # for ten seconds; the board read +96% industries and 1,750 limit-ups
    twice = lambda lid, c: pl.when(pl.col("listing_id") == lid).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)   # noqa: E731
    q = quotes().with_columns(twice("CN:000008", "last"), twice("CN:000009", "high")).with_columns(
        pl.when(pl.col("listing_id") == "CN:000011").then(12.0).otherwise(pl.col("last")).alias("last"))
    kept, n = possible(q, facts(), DAY)
    ids = kept["listing_id"].to_list()
    assert n == 2 and "CN:000008" not in ids and "CN:000009" not in ids
    assert "CN:000012" in ids                                         # a new listing's placeholder limits: any price can be
    assert "CN:000011" in ids                                         # the day before's stamp is not judged by today's limits
    b = compute(traded(kept, DAY), facts(), AT, share=0.5)
    assert (b["traded"], b["limits"]["limit_up"]) == (9, 3)
    same, none = possible(q, facts(limits="none"), DAY)
    assert none == 0 and same.height == q.height                      # without today's limits nothing can be judged
