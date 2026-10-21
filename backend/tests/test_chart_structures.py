"""Structures for the chart (spec §8.2, §6.3): one listing's L2 tables in the display basis, every row
with its known_at for the replay."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.charts.structures import overlay

D = lambda i: date(2024, 1, 1) + timedelta(days=i)  # noqa: E731
LID = "CN:600000"


def daily(n=12, adj=None):
    """Raw daily rows; an ex-date on bar 6 halves the price (adj factor 1 → 2)."""
    adj = adj if adj is not None else [1.0] * 6 + [2.0] * (n - 6)
    return pl.DataFrame({"listing_id": LID, "date": [D(i) for i in range(n)], "adj_factor": adj})


def tables():
    boxes = pl.DataFrame({
        "box_id": ["b1", "p1"], "listing_id": LID, "scale": ["mid", "short"], "status": ["established", "proto"],
        "start": [D(2), D(1)], "known_at": [D(4), D(2)], "end": [D(8), None], "ended_at": [D(9), None],
        "end_reason": ["上破离箱", None], "top": [20.0, 19.0], "bottom": [18.0, 18.5], "width": [0.11, 0.03],
        "length": [7, None], "shape": ["flat", None], "context": ["下横", None], "position": ["低位", None],
        "excluded_before": [D(1), None], "superseded_at": [None, D(4)]})
    touches = pl.DataFrame({"box_id": ["b1", "b1"], "date": [D(3), D(7)], "side": ["top", "bottom"], "price": [20.0, 18.0],
                            "known_at": [D(4), D(9)]})
    events = pl.DataFrame({"box_id": ["b1", "b1"], "date": [D(5), D(8)], "event": ["spike_up", "break_up"],
                           "value": [20.6, 2.5], "known_at": [D(5), D(8)]})
    pivots = pl.DataFrame({"listing_id": LID, "idx": [3], "date": [D(3)], "kind": ["high"], "price": [20.0], "known_at": [D(5)]})
    return {"BOX": {"boxes": boxes, "touches": touches, "events": events}, "PIVOTS": {"pivots": pivots}}


def test_prices_stay_as_computed_in_the_backward_adjusted_basis():
    o = overlay(tables(), daily(), "hfq")
    b = next(x for x in o["boxes"] if x["id"] == "b1")
    assert b["segments"] == [{"from": D(2), "to": D(8), "top": 20.0, "bottom": 18.0}]
    assert b["touches"][1] == {"date": D(7), "side": "bottom", "price": 18.0, "known_at": D(9)}


def test_forward_adjusted_prices_differ_by_one_constant():
    o = overlay(tables(), daily(), "qfq")                                # latest factor 2: everything halves
    b = next(x for x in o["boxes"] if x["id"] == "b1")
    assert b["segments"] == [{"from": D(2), "to": D(8), "top": 10.0, "bottom": 9.0}]
    assert o["pivots"][0]["price"] == 10.0


def test_raw_prices_split_a_box_at_the_ex_date():
    o = overlay(tables(), daily(), "raw")
    b = next(x for x in o["boxes"] if x["id"] == "b1")
    assert b["segments"] == [{"from": D(2), "to": D(5), "top": 20.0, "bottom": 18.0},
                             {"from": D(6), "to": D(8), "top": 10.0, "bottom": 9.0}]
    ev = {e["event"]: e for e in b["events"]}
    assert ev["spike_up"]["value"] == 20.6                               # a price, converted on its day (factor 1)
    assert ev["break_up"]["value"] == 2.5                                # a volume ratio: never converted


def test_rows_keep_their_known_at_and_protos_their_lifetime():
    o = overlay(tables(), daily(), "hfq")
    p = next(x for x in o["boxes"] if x["id"] == "p1")
    assert p["status"] == "proto" and p["known_at"] == D(2) and p["superseded_at"] == D(4)
    assert p["segments"] == [{"from": D(1), "to": None, "top": 19.0, "bottom": 18.5}]   # still open
    b = next(x for x in o["boxes"] if x["id"] == "b1")
    assert (b["end"], b["ended_at"], b["end_reason"]) == (D(8), D(9), "上破离箱")


def test_a_redrawn_box_says_when_it_was_first_drawn():
    # each version carries its own known_at (the day it was redrawn) and the day the box was first drawn: the panel says
    # 成立 on the first, not on the last redraw
    t = tables()
    first = t["BOX"]["boxes"].filter(pl.col("box_id") == "b1").with_columns(pl.lit(D(7)).alias("superseded_at"))
    t["BOX"]["boxes"] = pl.concat([first, t["BOX"]["boxes"].with_columns(
        pl.when(pl.col("box_id") == "b1").then(pl.lit("b2")).otherwise(pl.col("box_id")).alias("box_id"),
        pl.when(pl.col("box_id") == "b1").then(pl.lit(D(7))).otherwise(pl.col("known_at")).alias("known_at"))])
    t["BOX"]["boxes"] = t["BOX"]["boxes"].with_columns(
        pl.when(pl.col("box_id").is_in(["b1", "b2"])).then(pl.lit("L1")).otherwise(pl.col("box_id")).alias("lineage"))
    o = {x["id"]: x for x in overlay(t, daily(), "hfq")["boxes"]}
    assert (o["b2"]["known_at"], o["b2"]["first_known"]) == (D(7), D(4)) and o["b1"]["first_known"] == D(4)
    assert o["p1"]["first_known"] == D(2)


def test_a_listing_without_adjustment_factors_draws_raw_prices_in_every_basis():
    o = overlay(tables(), daily(adj=[None] * 12), "qfq")
    assert next(x for x in o["boxes"] if x["id"] == "b1")["segments"][0]["top"] == 20.0


def test_missing_primitives_give_empty_lists():
    o = overlay({}, daily(), "qfq")
    assert o["boxes"] == [] and o["slant"] == [] and o["pivots"] == [] and o["swings"] == []
    assert o["gold"] == [] and o["false_bottoms"] == []


@pytest.mark.parametrize("basis", ["qfq", "hfq", "raw"])
def test_the_overlay_is_plain_json(basis):
    import json
    json.dumps(overlay(tables(), daily(), basis), default=str)


def test_a_slanted_edge_is_a_line_over_traded_bars_bent_at_the_ex_date_in_raw_prices():
    slant = pl.DataFrame({"box_id": ["s1"], "listing_id": LID, "status": ["established"], "start": [D(2)], "known_at": [D(5)],
                          "end": [D(9)], "ended_at": [D(10)], "end_reason": ["上破离箱"], "width": [0.1],
                          "top_a": [D(2)], "top_a_price": [20.0], "top_b": [D(4)], "top_b_price": [21.0],
                          "bottom_a": [D(3)], "bottom_a_price": [18.0], "bottom_b": [D(5)], "bottom_b_price": [18.5]})
    t = {"SLANT_BOX": {"slant": slant}}
    top = overlay(t, daily(), "hfq")["slant"][0]["top"]
    assert [(p["date"], p["price"]) for p in top] == [(D(2), 20.0), (D(9), 23.5)]           # +0.5 a bar
    top = overlay(t, daily(), "raw")["slant"][0]["top"]
    assert [(p["date"], p["price"]) for p in top] == [(D(2), 20.0), (D(5), 21.5), (D(6), 11.0), (D(9), 11.75)]


def test_an_ex_date_on_a_suspended_day_cuts_at_the_bars_around_it():
    traded = [D(i) for i in range(12) if i != 6]                         # the ex-date, bar 6, had no trading
    b = next(x for x in overlay(tables(), daily(), "raw", traded)["boxes"] if x["id"] == "b1")
    assert [(g["from"], g["to"]) for g in b["segments"]] == [(D(2), D(5)), (D(7), D(8))]
    slant = pl.DataFrame({"box_id": ["s1"], "listing_id": LID, "status": ["established"], "start": [D(2)], "known_at": [D(5)],
                          "end": [D(9)], "ended_at": [D(10)], "end_reason": ["上破离箱"], "width": [0.1],
                          "top_a": [D(2)], "top_a_price": [20.0], "top_b": [D(4)], "top_b_price": [21.0],
                          "bottom_a": [D(3)], "bottom_a_price": [18.0], "bottom_b": [D(5)], "bottom_b_price": [18.5]})
    top = overlay({"SLANT_BOX": {"slant": slant}}, daily(), "raw", traded)["slant"][0]["top"]
    assert [(p["date"], p["price"]) for p in top] == [(D(2), 20.0), (D(5), 21.5), (D(7), 11.0), (D(9), 11.5)]   # +0.5 a traded bar


def test_a_gold_says_when_a_later_small_box_replaced_it():
    # 金品 is the big box's last small box: the chart shows an earlier one only until the next one is known
    gold = pl.DataFrame({"listing_id": LID, "small_box_id": ["s1", "s2"], "big_box_id": "b1", "kind": ["下品", "上品"],
                         "variant": False, "known_at": [D(4), D(7)], "superseded_at": [D(7), None]})
    o = overlay({**tables(), "BOX_DERIVED": {"gold": gold}}, daily(), "hfq")
    assert [g["superseded_at"] for g in o["gold"]] == [D(7), None]


def test_a_false_bottom_carries_its_box_edges_and_its_end():
    d = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    daily = pl.DataFrame({"date": d, "adj_factor": [2.0, 2.0, 2.0]})
    boxes = pl.DataFrame([{"box_id": "P", "scale": "mid", "status": "established", "start": d[0], "known_at": d[0], "end": d[1],
                           "ended_at": d[2], "end_reason": "上破离箱", "top": 20.0, "bottom": 18.0}])
    fbs = pl.DataFrame([{"listing_id": "CN:1", "box_id": "P", "source": "C 点", "gold_box_id": None, "date": d[0], "price": 19.0,
                         "previous_low": 18.1, "known_at": d[1], "end": d[2], "ended_at": d[2], "end_reason": "失守"}])
    o = overlay({"BOX": {"boxes": boxes}, "BOX_DERIVED": {"false_bottoms": fbs}}, daily, "qfq")
    assert o["false_bottoms"] == [{"box_id": "P", "source": "C 点", "date": d[0], "price": 19.0 / 2, "known_at": d[1],
                                    "end": d[2], "end_reason": "失守", "box_top": 10.0, "box_bottom": 9.0}]
