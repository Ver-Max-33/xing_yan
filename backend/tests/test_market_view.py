"""市场环境与板块强度 (spec §7.3): breadth per day, limit-up streaks, industry strength and the indexes' state, from
the stored daily bars. What the data does not carry (limit flags in a test profile) is unknown, never zero."""
from datetime import date

import polars as pl

from wb.data.store import Store
from wb.devdata import write
from wb.market.view import DAYS, breadth, market_view, sectors, streaks

D1, D2 = date(2026, 9, 29), date(2026, 9, 30)


def test_breadth_counts_each_day_and_an_unknown_flag_is_not_a_zero():
    v = pl.DataFrame({
        "listing_id": ["a", "b", "c"] * 2, "date": [D1] * 3 + [D2] * 3,
        "up": [True, False, None, True, True, False], "down": [False, True, None, False, False, True],
        "limit_up": [None] * 3 + [True, False, None], "touched": [None] * 3 + [True, True, None],
        "limit_down": [None] * 6, "high60": [False] * 3 + [True, False, False], "low60": [False] * 5 + [True]},
        schema_overrides={c: pl.Boolean for c in ("limit_up", "touched", "limit_down")})
    first, second = breadth(v)
    assert first == {"date": "2026-09-29", "total": 3, "up": 1, "down": 1, "limit_up": None, "broken": None,
                     "limit_down": None, "high60": 0, "low60": 0}
    assert second == {"date": "2026-09-30", "total": 3, "up": 2, "down": 1, "limit_up": 1, "broken": 1,
                      "limit_down": None, "high60": 1, "low60": 1}


def test_limit_up_streaks_are_counted_by_length_and_unknown_when_no_listing_says():
    assert streaks(pl.Series([0, 1, 1, 2, 5, 7, None])) == {"1": 2, "2": 1, "3": 0, "4": 0, "5+": 2}
    assert streaks(pl.Series([None, None], dtype=pl.Int64)) is None


def test_industries_rank_by_their_members_returns_and_a_small_one_stands_apart():
    rows = ([("电子", 0.02, 0.10, True)] * 4 + [("银行", -0.01, 0.03, False)] * 4 + [("钢铁", 0.05, 0.30, True)] * 2
            + [(None, 0.0, 0.0, False)])
    v = pl.DataFrame({"listing_id": [f"x{i}" for i in range(len(rows))], "industry": [r[0] for r in rows],
                      "chg": [r[1] for r in rows], "ret5": [r[2] / 2 for r in rows], "ret20": [r[2] for r in rows],
                      "up": [r[3] for r in rows], "limit_up": [False] * len(rows)})
    by = {s["industry"]: s for s in sectors(v)}
    assert set(by) == {"电子", "银行", "钢铁"}                                       # no industry: not a group
    assert by["电子"]["members"] == 4 and by["电子"]["up_ratio"] == 1.0 and abs(by["电子"]["chg"] - 0.02) < 1e-12
    assert (by["电子"]["rank20"], by["银行"]["rank20"]) == (1, 2) and by["电子"]["rank5"] == 1
    assert by["钢铁"]["small"] is True and by["钢铁"]["rank20"] is None              # two members: listed apart
    assert [s["industry"] for s in sectors(v)][:2] == ["电子", "银行"]               # ranked ones first, by 20 days


def test_the_market_view_on_stored_bars(paths):
    write(paths, 320)
    v = market_view(paths, "CN")
    assert v["day"] == str(Store(paths, "CN").latest().as_of) and len(v["breadth"]) == DAYS
    last = v["breadth"][-1]
    assert last["total"] == 2 and last["up"] + last["down"] <= 2 and last["limit_up"] is not None   # CN carries limits
    idx = {i["index_id"]: i for i in v["indexes"]}
    assert set(idx) == {"CN:IDX:000001", "CN:IDX:000300"}
    one = idx["CN:IDX:000300"]
    assert one["state"] in (-1, 0, 1) and one["ma200"] and one["close"] and "box" in one
    assert {s["industry"] for s in v["sectors"]} == {"银行", "食品饮料"} and v["known"] == {"limits": True, "industry": True}


def test_the_pages_industry_rank_is_the_one_rules_read():
    # INDUSTRY_RANK: tied industries share a rank and the next one is the next number; an industry ranks on its
    # members with a known return (four members, one of them unknown, is too few)
    from tests.test_l1_groups import per_listing, two_days
    from tests.test_l1_library import values
    ind = {**{f"CN:{i}": "电子" for i in range(1, 5)}, **{f"CN:{i}": "银行" for i in range(5, 9)},
           **{f"CN:{i}": "钢铁" for i in range(9, 13)}, **{f"CN:{i}": "化工" for i in range(13, 17)}}
    day2 = {lid: {"电子": 11.0, "银行": 11.0, "钢铁": 10.5, "化工": 12.0}[x] for lid, x in ind.items()} | {"CN:16": None}
    rule = per_listing(values("INDUSTRY_RANK(1)", two_days(day2, ind)), day2)
    ret = {lid: None if c is None else c / 10 - 1 for lid, c in day2.items()}
    v = pl.DataFrame({"listing_id": list(ind), "industry": list(ind.values()), "chg": list(ret.values()),
                      "ret5": list(ret.values()), "ret20": list(ret.values()), "up": [True] * len(ind),
                      "limit_up": [False] * len(ind)})
    page = {s["industry"]: s for s in sectors(v)}
    assert {x: page[x]["rank20"] for x in ("电子", "银行", "钢铁", "化工")} == {"电子": 1, "银行": 1, "钢铁": 2, "化工": None}
    assert all(page[ind[lid]]["rank20"] == rule[lid] for lid in ind if lid != "CN:16")
    assert page["化工"]["members"] == 4 and page["化工"]["small"] is False
