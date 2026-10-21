"""Industries, boards, tags and watch groups in formulas (spec §7.1 股票池, §7.3 板块强度): an industry's median of
a value on a day, its rank among the industries (an industry of three listings or fewer is not ranked against the
big ones), and whether a listing is on a board, in an industry, tagged, or in one of your watch groups."""
from datetime import timedelta

import polars as pl

from tests.test_l1_compiler import D0, frame, run
from tests.test_l1_library import values


def two_days(day2: dict[str, float], industry: dict[str, str | None], **cols):
    """Each listing at 10 on D0 and day2[lid] on the next day."""
    lids = list(day2)
    rows = [lid for lid in lids for _ in range(2)]
    df = frame(close=[v for lid in lids for v in (10.0, day2[lid])], listing_id=rows,
               industry=[industry[lid] for lid in rows], **{k: [v[lid] for lid in rows] for k, v in cols.items()})
    return df.with_columns(pl.Series("date", [D0, D0 + timedelta(days=1)] * len(lids)))


A, B, C = [f"CN:{i}" for i in range(1, 5)], [f"CN:{i}" for i in range(5, 9)], ["CN:9", "CN:10"]
DAY2 = {**dict.fromkeys(A, 11.0), **dict.fromkeys(B, 10.5), **dict.fromkeys(C, 12.0)}   # +10%, +5%, +20%
IND = {**dict.fromkeys(A, "电子"), **dict.fromkeys(B, "银行"), **dict.fromkeys(C, "钢铁")}


def per_listing(xs: list, day2: dict = DAY2) -> dict:
    return {lid: xs[2 * k + 1] for k, lid in enumerate(day2)}           # the second day's value of each listing


def test_an_industry_ranks_by_its_members_median_return_and_a_small_one_is_not_ranked():
    out = per_listing(values("INDUSTRY_RANK(1)", two_days(DAY2, IND)))
    assert {out[x] for x in A} == {1} and {out[x] for x in B} == {2}
    assert {out[x] for x in C} == {None}                                  # 2 listings: listed apart (§7.3)
    med = per_listing(values("IND_MEDIAN(C / REF(C, 1) - 1)", two_days(DAY2, IND)))
    assert abs(med["CN:1"] - 0.10) < 1e-12 and med["CN:9"] is None
    first_day = values("INDUSTRY_RANK(1)", two_days(DAY2, IND))[0]
    assert first_day is None                                              # no return yet: unknown


def test_a_listing_without_an_industry_is_not_ranked_and_leaves_its_industry_smaller():
    out = per_listing(values("INDUSTRY_RANK(1)", two_days(DAY2, {**IND, "CN:1": None})))
    assert out["CN:1"] is None and out["CN:2"] is None                 # 电子 is down to three: listed apart
    assert out["CN:5"] == 1                                            # 银行 is the only big industry left


def test_board_industry_tags_and_watch_groups_read_as_conditions():
    df = two_days(DAY2, IND, board={x: "创业板" if x in A else "主板" for x in DAY2},
                  tags={x: ["锂电", "AI"] if x == "CN:1" else [] for x in DAY2},
                  watch_groups={x: ["自选"] if x in B else [] for x in DAY2})
    board = per_listing(run('IN_BOARD("创业板")', df))
    assert board["CN:1"] is True and board["CN:5"] is False
    assert per_listing(run('IN_INDUSTRY("银行")', df))["CN:5"] is True
    tagged = per_listing(run('TAGGED("锂电") AND TAGGED("ai")', df))   # tags compare without case
    assert tagged["CN:1"] is True and tagged["CN:2"] is False
    group = per_listing(run('IN_GROUP("自选")', df))
    assert group["CN:6"] is True and group["CN:1"] is False


def test_breadth_is_the_days_share_of_listings_up():
    out = values("BREADTH", two_days({**DAY2, "CN:9": 9.0}, IND))           # one of ten falls on the second day
    assert out[0] is None and abs(out[1] - 0.9) < 1e-12


def test_the_frame_brings_boards_tags_groups_and_the_index_state(paths):
    from wb.data.store import Store
    from wb.devdata import write
    from wb.engine.l1.frame import available_columns, build_frame
    from wb.workspace.store import Workspace
    write(paths, 320)
    ws = Workspace(paths)
    ws.add_item(ws.add_group("重点"), "CN:600519")
    ws.set_tags("CN:600519", ["白酒", "核心资产"])
    st = Store(paths, "CN")
    assert {"board", "s_share", "tags", "watch_groups", "idx_state"} <= available_columns(st)
    f = build_frame(st, "CN", "test", {"board", "s_share", "tags", "watch_groups", "idx_state"})
    mt = f.filter(pl.col("listing_id") == "CN:600519").tail(1).row(0, named=True)
    assert mt["board"] == "主板" and sorted(mt["tags"]) == ["核心资产", "白酒"] and mt["watch_groups"] == ["重点"]
    assert mt["s_share"] is False
    assert mt["idx_state"] in (-1, 0, 1)
    other = f.filter(pl.col("listing_id") == "CN:000001").tail(1).row(0, named=True)
    assert other["tags"] == [] and other["watch_groups"] == []
    assert f.filter(pl.col("listing_id") == "CN:600519")["idx_state"].head(150).null_count() == 150   # no 200-day line yet


def test_tags_are_kept_per_listing_and_listed_with_their_counts(paths):
    from wb.workspace.store import Workspace
    ws = Workspace(paths)
    ws.set_tags("CN:600519", ["白酒", " 白酒 ", "核心资产", ""])
    ws.set_tags("JP:7203", ["自动车"])
    assert ws.tags_of("CN:600519") == ["核心资产", "白酒"] and ws.tags_of("CN:000001") == []
    assert ws.all_tags() == [{"tag": "核心资产", "n": 1}, {"tag": "白酒", "n": 1}, {"tag": "自动车", "n": 1}]
    ws.set_tags("CN:600519", [])
    assert ws.tags_of("CN:600519") == [] and [t["tag"] for t in ws.all_tags()] == ["自动车"]


def test_s_share_is_an_a_share_word():
    """未股改 is the A-share reform: a Tokyo name that starts with S (SBIホールディングス, SCSK) is no such thing."""
    import pytest

    from wb.engine.l1.compiler import CompileError, Compiler
    Compiler("CN").add("x", "S_SHARE()")
    with pytest.raises(CompileError, match="不适用于 JP"):
        Compiler("JP").add("x", "S_SHARE()")


def test_the_whole_market_is_its_stocks_and_a_fund_is_read_only_when_named(paths):
    """Spec §7.1 股票池: ETFs, REITs, 出資証券 and PRO Market stocks (only professional investors may buy those) are
    left out of every market-wide read — rules, 选股, the market page; a listing asked for by name is read as it is."""
    from wb.data.store import Store
    from wb.devdata import write
    from wb.engine.l1.frame import build_frame
    from wb.markets import load_market
    assert {"ETF・ETN", "REIT・ベンチャーファンド・カントリーファンド・インフラファンド", "出資証券", "TOKYO PRO MARKET",
            "PRO Market"} <= set(load_market("JP").non_stock_boards)   # J-Quants' and JPX's names
    write(paths, 320)
    st = Store(paths, "JP")
    inst = st.read("instruments")
    st.publish({"instruments": inst.filter(pl.col("listing_id") == "JP:130A").with_columns(pl.lit("ETF・ETN").alias("board"))},
               as_of=st.latest().as_of)
    assert set(build_frame(st, "JP", "test", set())["listing_id"]) == {"JP:7203", "JP:6758"}
    assert set(build_frame(st, "JP", "test", set(), ["JP:130A"])["listing_id"]) == {"JP:130A"}
    assert set(build_frame(Store(paths, "CN"), "CN", "test", set())["listing_id"]) == {"CN:600519", "CN:000001"}
