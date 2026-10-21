"""持有中's suggested fail lines (design 2026-10-10 §2): from the stock's structures as its last close knows them — a 金品's
small-box bottom, a 假底 still standing, a mid or long box's bottom × 0.985 (a box still live, or one left upward within
the last 60 bars) — the ones under the last close that no close since has crossed, highest first, in today's prices.
You pick one; nothing is saved by itself."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.data.store import Store
from wb.devdata import write
from wb.engine.l2.box import BOX_SCHEMA
from wb.engine.l2.derived import FB_SCHEMA, GOLD_SCHEMA
from wb.workspace.watch import Watch, fail_lines

D0 = date(2026, 1, 1)


def day(i: int) -> date:
    return D0 + timedelta(days=i)


def frame(rows: list[dict], schema: dict) -> pl.DataFrame:
    return pl.DataFrame([{k: r.get(k) for k in schema} for r in rows], schema=schema)


def box(bid: str, scale: str, bottom: float, **kw) -> dict:
    return {"box_id": bid, "scale": scale, "status": "established", "known_at": day(10), "bottom": bottom, **kw}


def test_the_lines_under_the_close_that_still_hold_highest_first():
    closes = [20.0] * 100
    closes[60] = 19.0
    bars = pl.DataFrame({"date": [day(i) for i in range(100)], "close": closes})
    tables = {
        "BOX": {"boxes": frame([
            box("A", "mid", 15.0, ended_at=day(70), end_reason="上破离箱"),   # left upward 29 bars ago: still defends
            box("B", "long", 12.0),                                          # live
            box("C", "mid", 17.0, ended_at=day(80), end_reason="下破离箱"),   # left downward: over
            box("D", "mid", 16.5, superseded_at=day(50)),                     # an older version of a box
            box("E", "short", 19.0),                                         # a small box has no box-bottom line
            box("F", "mid", 14.0, ended_at=day(30), end_reason="上破离箱")],   # left upward 69 bars ago
            BOX_SCHEMA)},
        "BOX_DERIVED": {
            "gold": frame([
                {"kind": "天品", "variant": False, "small_bottom": 18.0, "known_at": day(80)},
                {"kind": "上品", "variant": False, "small_bottom": 18.5, "known_at": day(70), "superseded_at": day(80)},
                {"kind": "下品", "variant": True, "small_bottom": 17.5, "known_at": day(20)},    # 79 bars ago
                {"kind": "天品", "variant": False, "small_bottom": 25.0, "known_at": day(90)}],   # above the close
                GOLD_SCHEMA),
            "false_bottoms": frame([
                {"source": "C 点", "price": 16.0, "known_at": day(70)},
                {"source": "金品", "price": 18.0, "known_at": day(80)},                   # the 天品's own line: said once
                {"source": "C 点", "price": 17.2, "known_at": day(40), "end": day(75)},  # its line ended
                {"source": "C 点", "price": 19.5, "known_at": day(50)}],                 # a close under it since (day 60)
                FB_SCHEMA)}}
    got = fail_lines(tables, bars)
    assert [(x["source"], x["label"], x["price"], x["since"]) for x in got] == [
        ("金品", "天品小箱底", 18.0, day(80)), ("假底", "假底（C 点）", 16.0, day(70)),
        ("箱底", "中箱底 × 0.985", pytest.approx(14.775), day(10)), ("箱底", "大箱底 × 0.985", pytest.approx(11.82), day(10))]


def test_a_variant_says_so_and_a_stock_without_structures_has_none():
    bars = pl.DataFrame({"date": [day(i) for i in range(5)], "close": [10.0] * 5})
    gold = frame([{"kind": "下品", "variant": True, "small_bottom": 9.0, "known_at": day(3)}], GOLD_SCHEMA)
    assert [x["label"] for x in fail_lines({"BOX_DERIVED": {"gold": gold}}, bars)] == ["变异下品小箱底"]
    assert fail_lines({}, bars) == [] and fail_lines({"BOX_DERIVED": {"gold": gold}}, bars.clear()) == []


def test_the_suggestions_are_in_todays_prices_across_a_split(paths):
    write(paths, 320)
    watch, lid = Watch(lambda m: paths), "CN:600519"
    st = Store(paths, "CN")
    last = st.read("bars_1d").filter(pl.col("listing_id") == lid).sort("date").tail(1)
    before = watch.fail_lines(lid)
    assert before and all(x["price"] < last["close"][0] for x in before)
    assert before[0]["distance"] == pytest.approx(before[0]["price"] / last["close"][0] - 1)
    halves = ("open", "high", "low", "close", "pre_close", "up_limit", "down_limit")
    st.publish({"bars_1d": last.with_columns(pl.col(*halves) / 2, pl.col("adj_factor") * 2)}, as_of=last["date"][0], note="split")
    after = watch.fail_lines(lid)   # the same structures (adjusted prices), half the exchange's price
    assert [x["label"] for x in after] == [x["label"] for x in before]
    assert [x["price"] for x in after] == pytest.approx([x["price"] / 2 for x in before], abs=0.01)
