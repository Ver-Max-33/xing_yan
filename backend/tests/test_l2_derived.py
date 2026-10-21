"""金品, 箱中箱, 箱体关系, 假底 from hand-made box tables (spec §5.4 由箱体推出的结构)."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.engine.l2.derived import false_bottoms, false_bottoms_if, gold, nest, relations

D = lambda i: date(2024, 1, 1) + timedelta(days=i)


def boxes(*rows):
    cols = ("box_id", "scale", "start", "known_at", "end", "ended_at", "end_reason", "top", "bottom")
    return pl.DataFrame([dict(zip(cols, r, strict=True), listing_id="CN:600000", status="established") for r in rows],
                        schema={"box_id": pl.Utf8, "scale": pl.Utf8, "start": pl.Date, "known_at": pl.Date, "end": pl.Date,
                                "ended_at": pl.Date, "end_reason": pl.Utf8, "top": pl.Float64, "bottom": pl.Float64,
                                "listing_id": pl.Utf8, "status": pl.Utf8})


P = ("P", "mid", D(0), D(25), D(70), D(75), "上破离箱", 10.0, 9.0)
BARS = pl.DataFrame({"listing_id": "CN:600000", "date": [D(i) for i in range(400)]})   # a bar every day


def test_small_boxes_inside_a_big_box_are_upper_or_lower_gold():
    b = boxes(P, ("C1", "short", D(40), D(50), D(55), D(58), "下破离箱", 9.62, 9.30),   # well below the top: 下品
              ("C2", "short", D(56), D(66), None, None, None, 9.95, 9.70))             # top at the big top: 上品
    g = gold(b, BARS).sort("small_box_id")
    assert g["kind"].to_list() == ["下品", "上品"]
    assert g["inner_distance"][0] == pytest.approx((10.0 - 9.62) / 9.62)  # from the small box's top
    assert g["known_at"].to_list() == [D(50), D(66)]
    assert set(nest(b)["small_box_id"]) == {"C1", "C2"}



def test_upper_gold_is_pressed_under_the_top_unless_it_rises_from_below():
    # 上品 is a small range inside the big box pressed under its top; only 变异上品 (a rising narrow channel)
    # comes up to the top from the lower middle. A flat small box from near the big bottom up to the top is the big
    # box's own last swing: no 金品 (2026-10-11: 65% of what was called 上品 reached into the lower half)
    flat = ("C2", "short", D(56), D(66), None, None, None, 9.98, 9.30)                 # bottom at 30% of P's height
    assert gold(boxes(P, flat), BARS).is_empty()
    rising = boxes(P, flat).with_columns(pl.Series("slope", [None, 0.002]))            # 变异上品
    g = gold(rising, BARS)
    assert g["kind"].to_list() == ["上品"] and g["variant"].to_list() == [True]
    high = ("C2", "short", D(56), D(66), None, None, None, 9.98, 9.55)                 # bottom at 55%: 上品
    assert gold(boxes(P, high), BARS)["kind"].to_list() == ["上品"]
    assert gold(boxes(P, flat), BARS, floor=0.2)["kind"].to_list() == ["上品"]        # a setting: no number was given
    # a narrow big box (云南旅游: 2.3% tall) has no upper half a small box fits in: within two of its bands under the top
    narrow = ("N", "mid", D(0), D(25), D(70), D(75), "上破离箱", 10.0, 9.77)
    hugging = ("C3", "short", D(56), D(66), None, None, None, 10.0, 9.85)              # 1.5% under the top, 35% up
    assert gold(boxes(narrow, hugging), BARS, tol=0.02)["kind"].to_list() == ["上品"]

def test_a_box_above_the_old_top_after_the_break_is_sky_gold():
    b = boxes(P, ("C", "short", D(72), D(80), None, None, None, 10.6, 10.05))
    g = gold(b, BARS)
    assert g["kind"].to_list() == ["天品"] and g["known_at"][0] == D(80)
    assert g["above_top"][0] == pytest.approx(10.05 / 10.0 - 1)
    # a small box that dips back into the old box is not 天品
    assert gold(boxes(P, ("C", "short", D(72), D(80), None, None, None, 10.6, 9.6)), BARS).is_empty()


def test_sky_gold_is_a_small_box_at_the_old_top():
    # 天品: a low-volume pullback after the break, held at the old top, then a new small box there and a new
    # advance: the reference cases' 28 plain 天品 sit at the old top (bottom +1.2% over it at the median, +4.4% at 90%). One that
    # formed after the price had run far above the top is a pause after a rally, not 天品
    far = boxes(P, ("C", "short", D(72), D(80), None, None, None, 12.6, 12.0))   # bottom 20% above the old top
    assert gold(far, BARS).is_empty()
    near = boxes(P, ("C", "short", D(72), D(80), None, None, None, 10.9, 10.45))  # 4.5% above it
    assert gold(near, BARS)["kind"].to_list() == ["天品"]


def test_sky_gold_forms_right_after_the_break():
    # his 天品 small boxes start on the break or a few bars after it (median 0 bars, at most 6): the pullback to the top
    late = boxes(P, ("C", "short", D(82), D(90), None, None, None, 10.6, 10.05))   # 12 bars after the break on day 70
    assert gold(late, BARS).is_empty()
    soon = boxes(P, ("C", "short", D(75), D(83), None, None, None, 10.6, 10.05))   # 5 bars after it
    assert gold(soon, BARS)["kind"].to_list() == ["天品"]
    # bars, not days: with every other day a holiday, day 82 is the 6th bar after the break
    sparse = BARS.filter((pl.col("date") <= D(70)) | ((pl.col("date") - D(70)).dt.total_days() % 2 == 0))
    assert gold(boxes(P, ("C", "short", D(82), D(90), None, None, None, 10.6, 10.05)), sparse, soon=6)["kind"].to_list() == ["天品"]


def events(*rows):
    return pl.DataFrame([{"box_id": b, "date": d, "event": e, "value": None, "known_at": d} for b, d, e in rows],
                        schema={"box_id": pl.Utf8, "date": pl.Date, "event": pl.Utf8, "value": pl.Float64, "known_at": pl.Date})


def test_sky_gold_counts_from_the_break_even_if_the_box_later_came_back():
    # 天品 is about the breakout (a low-volume pullback held at the old top): such pullbacks dip a little
    # under the top (002464 in 2016-11: broke on 11-16, a close back at the edge on 11-29, broke again on 11-30) — the
    # small box formed right after the first break is 天品, though the box only ended at the second
    q = ("Q", "mid", D(0), D(25), D(90), D(95), "上破离箱", 10.0, 9.0)
    c = ("C", "short", D(70), D(80), None, None, None, 10.6, 10.0)
    ev = events(("Q", D(70), "break_up"), ("Q", D(76), "return"), ("Q", D(90), "break_up"))
    g = gold(boxes(q, c), BARS, events=ev)
    assert g["kind"].to_list() == ["天品"] and g["known_at"][0] == D(80)
    assert gold(boxes(q, c), BARS).is_empty()   # by the box's end alone it started 20 bars too early


def test_sky_gold_may_start_on_the_bars_just_before_the_break():
    # the small box's window can take in the last bar or two at the top before the breakout (002765 in 2017-08:
    # 08-24, the day before the break): still the box formed at the breakout; one that began well before is 上品's
    near = boxes(P, ("C", "short", D(68), D(80), None, None, None, 10.6, 10.0))
    assert gold(near, BARS)["kind"].to_list() == ["天品"]
    early = boxes(P, ("C", "short", D(66), D(80), None, None, None, 10.6, 10.0))
    assert gold(early, BARS).is_empty()


def test_a_small_box_known_only_after_the_big_one_ended_is_not_inside_it():
    # P broke on day 70 and left on day 75: that it ended is known only on day 75
    late = boxes(P, ("C", "short", D(60), D(76), None, None, None, 9.6, 9.3))
    assert nest(late).is_empty() and gold(late, BARS).is_empty()


def test_a_small_box_known_while_the_big_one_is_pending_is_inside_it():
    # day 72 lies between P's break (70) and its leave (75): on day 72 P may still come back, so a
    # small box confirmed then is inside it — deciding by the break bar would read the future (§5.8)
    pending = boxes(P, ("C", "short", D(60), D(72), None, None, None, 9.6, 9.3))
    assert nest(pending)["small_box_id"].to_list() == ["C"] and gold(pending, BARS)["kind"].to_list() == ["下品"]


def test_relations_between_successive_boxes_and_the_seeking_line():
    b = boxes(("A", "mid", D(0), D(25), D(40), D(45), "上破离箱", 10.0, 9.0),
              ("B", "mid", D(50), D(75), D(90), D(95), "下破离箱", 11.0, 10.05),   # B's bottom at A's top: 邻
              ("C", "mid", D(100), D(130), None, None, None, 8.5, 7.5))          # a gap below B: 隔
    bars = pl.DataFrame({"listing_id": "CN:600000", "date": [D(i) for i in range(140)],
                         "close": [9.5 + 0.01 * i for i in range(140)]})
    r = relations(b, bars)
    assert r["kind"].to_list() == ["邻", "隔"]
    assert r["seek_direction"][0] == "up" and r["seek_bars"][0] == 10


def test_a_nested_box_follows_the_nested_box_before_it_not_the_big_ones():
    # boxes of one level follow one another: a 中箱 drawn inside a 大箱 (the inner machine, id …:inner:…) is paired with
    # the 中箱 inside before it, not with the 大箱 holding it or the big machine's box before that
    b = boxes(("x:big:1.0", "mid", D(0), D(25), D(40), D(45), "上破离箱", 10.0, 9.0),
              ("x:big:2.1", "long", D(50), D(120), None, None, None, 12.0, 10.0),
              ("x:inner:1.0", "mid", D(70), D(90), D(100), D(105), "上破离箱", 11.0, 10.4),
              ("x:inner:2.1", "mid", D(108), D(125), None, None, None, 11.9, 11.2))
    bars = pl.DataFrame({"listing_id": "CN:600000", "date": [D(i) for i in range(140)], "close": [10.0] * 140})
    r = relations(b, bars)
    assert sorted(r.select("from_box_id", "to_box_id").rows()) == [("x:big:1.0", "x:big:2.1"), ("x:inner:1.0", "x:inner:2.1")]


def test_a_nested_box_does_not_end_a_big_box_s_false_bottom():
    # 新箱 ends a 假底 when a box of P's level starts after the break: a 中箱 inside a later 大箱 is another level
    inner = ("x:inner:1.0", "mid", D(66), D(80), None, None, None, 11.0, 10.2)
    assert fb(P_RUN, inner).select("end", "end_reason").row(0) != (D(80), "新箱")


def test_derived_structures_never_depend_on_later_knowledge():
    # a mid box that starts earlier but is confirmed later than a short box must not change what was
    # derived from the short box before the mid box existed (spec §5.8)
    big = ("L", "long", D(0), D(70), D(150), D(155), "上破离箱", 10.0, 9.0)
    short = ("S", "short", D(153), D(165), None, None, None, 10.6, 10.1)
    mid = ("M", "mid", D(152), D(180), None, None, None, 10.8, 10.05)
    early, late = gold(boxes(big, short), BARS), gold(boxes(big, short, mid), BARS)
    assert late.filter(pl.col("known_at") <= D(170)).equals(early.filter(pl.col("known_at") <= D(170)))
    inside_short = ("S2", "short", D(80), D(90), D(100), D(101), "下破离箱", 9.6, 9.3)
    inside_mid = ("M2", "mid", D(75), D(110), D(140), D(141), "下破离箱", 9.7, 9.2)
    n1, n2 = nest(boxes(big, inside_short)), nest(boxes(big, inside_short, inside_mid))
    assert n2.filter(pl.col("known_at") <= D(100)).sort("big_box_id").equals(n1.filter(pl.col("known_at") <= D(100)).sort("big_box_id"))


def test_a_gold_is_the_big_box_s_last_small_box():
    # 金品是大箱末端的小箱（大箱末端或刚突破后的小结构；曲箱到末期变平，也成了金品）—
    # once a later small box in the same big box is known, the earlier one is no longer its 金品
    b = boxes(P, ("C1", "short", D(40), D(50), D(55), D(58), "下破离箱", 9.62, 9.30),
              ("C2", "short", D(56), D(66), None, None, None, 9.95, 9.70))
    assert gold(b, BARS).sort("small_box_id")["superseded_at"].to_list() == [D(66), None]
    assert gold(boxes(P, ("C", "short", D(72), D(80), None, None, None, 10.6, 10.05)), BARS)["superseded_at"].to_list() == [None]   # 天品


def test_the_last_small_box_is_the_same_whatever_order_the_boxes_come_in():
    # a flat and a slant small box with one start, confirmed the same day: the later id is the last, every time
    a, b = ("C_a", "short", D(40), D(50), None, None, None, 9.62, 9.30), ("C_b", "short", D(40), D(50), None, None, None, 9.95, 9.70)
    for rows in ((P, a, b), (P, b, a)):
        g = gold(boxes(*rows), BARS).sort("small_box_id")
        assert g["superseded_at"].to_list() == [D(50), None]


# ---- 假底 (design 2026-10-06 §5) ----------------------------------------------------
# P: a mid box 10.0 / 9.0 from day 0, touched at the bottom on day 10 (9.02) and 30 (9.05), at the top on day 20
# and 45; the pullback after day 45 bottoms at 9.40 on day 55; the upward break is on day 60.

def fb_bars(lows=None, closes=None, n=140):
    low, close = [9.5] * n, [9.7] * n
    low[10], low[30], low[55] = 9.02, 9.05, 9.40
    for i in range(60, n):
        low[i], close[i] = 10.1, 10.3
    for i, v in (lows or {}).items():
        low[i] = v
    for i, v in (closes or {}).items():
        close[i] = v
    return pl.DataFrame({"listing_id": "CN:600000", "date": [D(i) for i in range(n)], "low": low, "close": close})


def fb_touches(*extra):
    rows = [("P", 10, "bottom", 9.02, 25), ("P", 20, "top", 9.98, 25), ("P", 30, "bottom", 9.05, 32), ("P", 45, "top", 9.99, 47), *extra]
    return pl.DataFrame([{"box_id": b, "date": D(d), "side": s, "price": p, "known_at": D(k)} for b, d, s, p, k in rows])


def fb_events(*days, box="P"):
    return pl.DataFrame([{"box_id": box, "date": D(d), "event": "break_up", "value": None, "known_at": D(d)} for d in days],
                        schema={"box_id": pl.Utf8, "date": pl.Date, "event": pl.Utf8, "value": pl.Float64, "known_at": pl.Date})


NO_GOLD = pl.DataFrame(schema={"big_box_id": pl.Utf8, "small_box_id": pl.Utf8, "kind": pl.Utf8, "small_bottom": pl.Float64,
                               "known_at": pl.Date, "superseded_at": pl.Date})
P_RUN = ("P", "mid", D(0), D(25), D(60), D(65), "上破离箱", 10.0, 9.0)


def fb(*rows, touches=None, events=None, gold=NO_GOLD, bars=None, **kw):
    return false_bottoms(boxes(*(rows or (P_RUN,))), fb_touches() if touches is None else touches,
                         fb_events(60) if events is None else events, gold, fb_bars() if bars is None else bars, **kw)


def test_the_false_bottom_is_c_the_low_after_the_last_top_touch():
    # before the break the price first found support at C — C (9.40) is above the bottom touch before the last top
    # touch (9.05): known on the break, drawn from C
    r = fb().row(0, named=True)
    assert (r["source"], r["date"], r["price"], r["previous_low"], r["known_at"]) == ("C 点", D(55), 9.40, 9.05, D(60))


def test_a_pullback_to_the_bottom_leaves_no_false_bottom():
    # a pullback that reaches the bottom leaves no 假底: back to the bottom band, or not above the bottom touch before
    assert fb(bars=fb_bars(lows={55: 9.10})).is_empty()                # inside the bottom band (9.0 × 1.015)
    assert fb(touches=fb_touches(("P", 40, "bottom", 9.50, 42))).is_empty()   # 9.40 is not above 9.50


def test_a_gold_s_bottom_is_the_false_bottom():
    # the 金品 is the 假底: P's current 上品 (its small box from day 46, bottom 9.70) on the break bar
    small = ("C2", "short", D(46), D(52), None, None, None, 9.95, 9.70)
    gold = pl.DataFrame([{"big_box_id": "P", "small_box_id": "C2", "kind": "上品", "small_bottom": 9.70, "known_at": D(52),
                          "superseded_at": None}], schema=NO_GOLD.schema)
    r = fb(P_RUN, small, gold=gold).row(0, named=True)
    assert (r["source"], r["gold_box_id"], r["date"], r["price"], r["previous_low"]) == ("金品", "C2", D(46), 9.70, 9.05)
    # a 金品 down at P's bottom is no support above it: C instead
    low_gold = gold.with_columns(pl.lit(9.05).alias("small_bottom"))
    assert fb(P_RUN, small, gold=low_gold)["source"].to_list() == ["C 点"]


def test_the_line_ends_when_lost_when_a_new_box_forms_or_after_its_horizon():
    assert fb(bars=fb_bars(lows={70: 9.18}, closes={70: 9.20})).select("end", "ended_at", "end_reason").row(0) == (D(70), D(70), "失守")
    later = ("Q", "mid", D(66), D(80), None, None, None, 11.0, 10.2)        # P's scale, starting after the break
    assert fb(P_RUN, later).select("end", "end_reason").row(0) == (D(80), "新箱")
    assert fb(horizon=30).select("end", "end_reason").row(0) == (D(90), "到期")
    assert fb(bars=fb_bars(n=80)).select("end", "end_reason").row(0) == (None, None)   # still running on the last bar


def test_one_false_bottom_per_box_the_first_break_that_has_one():
    # a later upward break adds none, also after the first is lost (a rule's stage knows a 假底 by its box)
    assert fb(events=fb_events(60, 75)).height == 1
    assert fb(events=fb_events(60, 75), bars=fb_bars(lows={70: 9.18}, closes={70: 9.20})).height == 1
    # the first break had none (the pullback reached the bottom); back in the box, a new top touch (day 65), a
    # pullback to 9.30 and the next break: that one has its C
    later = fb(touches=fb_touches(("P", 65, "top", 9.98, 67)), events=fb_events(60, 75),
               bars=fb_bars(lows={55: 9.05, 70: 9.30}, closes={70: 9.60}))
    assert later.select("known_at", "price", "date").rows() == [(D(75), 9.30, D(70))]


def test_small_boxes_have_no_false_bottom_and_later_knowledge_is_not_used():
    assert fb(("P", "short", D(0), D(25), D(60), D(65), "上破离箱", 10.0, 9.0)).is_empty()
    # the top touch on day 45 confirmed only after the break: the last top touch known then is day 20, and the
    # lowest low from there to the break is the bottom touch on day 30 — no 假底
    late = fb_touches().with_columns(pl.when(pl.col("date") == D(45)).then(pl.lit(D(62))).otherwise(pl.col("known_at")).alias("known_at"))
    assert fb(touches=late).is_empty()



# ---- 假底 as if the box broke the next day (design 2026-10-11 §6: a cross buys before the break is known) -----------

def fb_if(*evenings, rows=None, touches=None, gold=NO_GOLD, bars=None):
    ev = pl.DataFrame([{"listing_id": "CN:600000", "box_id": b, "date": D(d)} for b, d in evenings])
    return false_bottoms_if(ev, boxes(*(rows or (P_RUN,))), fb_touches() if touches is None else touches, gold,
                            fb_bars() if bars is None else bars)


def test_the_false_bottom_as_if_broken_the_next_day_is_the_one_the_break_records():
    # the evening before the break (day 59): the same method, the data up to that close — C on day 55
    assert fb_if(("P", 59))["false_bottom"].to_list() == [fb()["price"][0]] == [9.40]
    small = ("C2", "short", D(46), D(52), None, None, None, 9.95, 9.70)
    gold = pl.DataFrame([{"big_box_id": "P", "small_box_id": "C2", "kind": "上品", "small_bottom": 9.70, "known_at": D(52),
                          "superseded_at": None}], schema=NO_GOLD.schema)
    r = fb_if(("P", 59), rows=(P_RUN, small), gold=gold).row(0, named=True)
    assert (r["false_bottom"], r["source"]) == (fb(P_RUN, small, gold=gold)["price"][0], "金品") == (9.70, "金品")


def test_the_false_bottom_as_if_reads_nothing_after_its_evening():
    # on day 50 the pullback had not reached 9.40 yet (day 55): its lowest low since the top touch on day 45 is 9.5
    assert fb_if(("P", 50), ("P", 59))["false_bottom"].to_list() == [9.5, 9.40]
    # a top touch confirmed only after the evening: the last one known is day 20, the lows since reach the bottom band
    late = fb_touches().with_columns(pl.when(pl.col("date") == D(45)).then(pl.lit(D(62))).otherwise(pl.col("known_at")).alias("known_at"))
    assert fb_if(("P", 59), touches=late)["false_bottom"].to_list() == [None]
    # a gold known only on the break bar is not the evening's
    small = ("C2", "short", D(46), D(60), None, None, None, 9.95, 9.70)
    gold = pl.DataFrame([{"big_box_id": "P", "small_box_id": "C2", "kind": "上品", "small_bottom": 9.70, "known_at": D(60),
                          "superseded_at": None}], schema=NO_GOLD.schema)
    assert fb_if(("P", 59), rows=(P_RUN, small), gold=gold)["source"].to_list() == ["C 点"]


def test_a_small_box_or_a_box_unknown_has_no_false_bottom_as_if():
    short = ("S", "short", D(0), D(25), D(60), D(65), "上破离箱", 10.0, 9.0)
    assert fb_if(("S", 59), ("Z", 59), rows=(P_RUN, short))["false_bottom"].to_list() == [None, None]