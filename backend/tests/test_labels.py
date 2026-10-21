"""Box labels and how far the algorithm agrees with them (spec §5.10)."""
from datetime import date, timedelta

import polars as pl
import pytest

from wb.engine.l2.agreement import THRESHOLDS, agreement, compare
from wb.workspace.labels import CATEGORIES, Labels, LabelError

D = lambda i: date(2024, 1, 1) + timedelta(days=i)  # noqa: E731
LID = "CN:600000"


def label(**kw):
    base = {"listing_id": LID, "cutoff": D(40), "is_box": True, "category": "down_base", "start": D(5), "end": D(30),
            "top": 12.0, "bottom": 11.0, "scale": "mid", "touches": [{"date": D(8), "side": "top", "price": 12.0}],
            "exclusions": [{"date": D(3), "kind": "箱前冲高", "price": 13.0, "reason": "箱前的冲高针"}]}
    return {**base, **kw}


def test_labels_are_stored_with_a_fixed_group_split_seven_to_three_per_category(paths):
    store = Labels(paths)
    ids = [store.add(label()) for _ in range(10)]
    groups = [store.get(i)["grp"] for i in ids]
    assert groups.count("test") == 3 and groups.count("tune") == 7
    store.update(ids[0], {"note": "看错了", "top": 12.2})
    got = store.get(ids[0])
    assert got["note"] == "看错了" and got["top"] == 12.2 and got["grp"] == groups[0]   # the group never changes
    assert got["touches"] == [{"date": "2024-01-09", "side": "top", "price": 12.0}]
    store.delete(ids[1])
    assert len(store.all()) == 9 and len(store.all(LID)) == 9


def test_a_label_is_checked_before_it_is_stored(paths):
    store = Labels(paths)
    with pytest.raises(LabelError, match="category"):
        store.add(label(category="whatever"))
    with pytest.raises(LabelError, match="上沿"):
        store.add(label(top=10.0))                                       # top under bottom
    with pytest.raises(LabelError, match="截止日"):
        store.add(label(end=D(50)))                                      # drawn past the bars it could see
    assert "down_base" in CATEGORIES and store.add(label(is_box=False, category="trend_pause"))


def bars(n=60):
    """A box 11–12 on bars 5..30 then a break up on bar 31."""
    close = [11.5] * 31 + [12.6] * (n - 31)
    return pl.DataFrame({"listing_id": LID, "date": [D(i) for i in range(n)], "close": close})


def boxes(*rows):
    cols = ["box_id", "scale", "status", "start", "known_at", "end", "ended_at", "top", "bottom"]
    return pl.DataFrame([dict(zip(cols, r, strict=True), listing_id=LID) for r in rows],
                        schema={"box_id": pl.Utf8, "scale": pl.Utf8, "status": pl.Utf8, "start": pl.Date, "known_at": pl.Date,
                                "end": pl.Date, "ended_at": pl.Date, "top": pl.Float64, "bottom": pl.Float64, "listing_id": pl.Utf8})


def test_the_measures_of_section_5_10():
    m = compare({"start": D(5), "end": D(30), "top": 12.0, "bottom": 11.0}, {"start": D(7), "end": D(30), "top": 12.1, "bottom": 11.0},
                [D(i) for i in range(60)], first_break=(D(31), D(31)))
    assert m["top_diff"] == pytest.approx(0.1 / 12.0) and m["bottom_diff"] == 0.0
    assert m["overlap"] == pytest.approx(1.0 / 1.1) and m["start_diff"] == 2 and m["end_diff"] == 0
    assert m["same_first_break"] is True and m["consistent"] is True
    far = compare({"start": D(5), "end": D(30), "top": 12.0, "bottom": 11.0}, {"start": D(5), "end": D(30), "top": 12.5, "bottom": 11.0},
                  [D(i) for i in range(60)])
    assert far["consistent"] is False                                    # the top is 4% off


def test_agreement_matches_each_label_with_the_box_known_by_its_cutoff_and_sums_up():
    b = boxes(("a1", "mid", "established", D(6), D(15), D(30), D(36), 12.05, 11.0),     # known before the cutoff: matched
              ("a2", "mid", "established", D(5), D(45), None, None, 12.0, 11.0))        # known after the cutoff: invisible
    labels = [label(id="L1", grp="test"), label(id="L2", grp="test", is_box=False, category="trend_pause", start=D(10), end=D(20))]
    out = agreement(labels, b, bars())
    one = {r["id"]: r for r in out["labels"]}
    assert one["L1"]["box_id"] == "a1" and one["L1"]["consistent"] is True
    assert one["L2"]["false_draw"] is True                               # the algorithm drew a box where you said none
    s = out["summary"]["test"]
    assert (s["boxes"], s["consistent"], s["not_boxes"], s["false_draws"]) == (1, 1, 1, 1)
    assert out["thresholds"] == THRESHOLDS


def test_a_redraw_is_compared_with_its_original_for_your_own_baseline():
    labels = [label(id="L1", grp="tune"), label(id="R1", grp="tune", redraw_of="L1", top=12.1, start=D(6))]
    out = agreement(labels, boxes(), bars())
    assert out["self"] == {"pairs": 1, "consistent": 1, "rate": 1.0}
    assert "R1" not in {r["id"] for r in out["labels"]}                 # a redraw is a baseline, not a sample


def test_a_slant_label_is_compared_with_slant_boxes_only():
    b = boxes(("h1", "mid", "established", D(5), D(15), D(30), D(36), 12.0, 11.0),
              ("s1", "slant", "established", D(6), D(16), D(30), D(36), 12.1, 11.05))
    out = agreement([label(id="L1", grp="test", category="slant")], b, bars())
    assert out["labels"][0]["box_id"] == "s1"
    out = agreement([label(id="L2", grp="test")], b, bars())
    assert out["labels"][0]["box_id"] == "h1"


def test_gallery_verdicts_belong_to_the_rule_and_can_be_cleared(paths):
    from wb.workspace.labels import HitLabels
    h = HitLabels(paths)
    h.set("r1", "CN:600000", D(5), "right", version=2, rule_hash="abc")
    h.set("r1", "CN:600001", D(6), "wrong")
    h.set("r1", "CN:600000", D(5), "wrong")                               # changed your mind
    assert {(x["listing_id"], x["verdict"]) for x in h.all("r1")} == {("CN:600000", "wrong"), ("CN:600001", "wrong")}
    h.set("r1", "CN:600001", D(6), None)                                  # cleared
    assert len(h.all("r1")) == 1 and h.all("r2") == []
    with pytest.raises(LabelError):
        h.set("r1", "CN:600000", D(5), "maybe")


def test_a_box_that_agrees_wins_over_one_that_merely_overlaps_more():
    b = boxes(("long1", "long", "established", D(20), D(25), D(30), D(36), 12.0, 11.0),   # same edges, starts 15 bars late
              ("mid1", "mid", "established", D(6), D(15), D(30), D(36), 12.1, 11.0))      # 0.8% off the top, starts 1 bar off
    out = agreement([label(id="L1", grp="test")], b, bars())
    assert out["labels"][0]["box_id"] == "mid1" and out["labels"][0]["consistent"] is True


def test_the_test_group_takes_both_markets_whatever_order_you_label_in(paths):
    store = Labels(paths)
    for cat in CATEGORIES:
        for lid in ("CN:600000", "CN:600001", "JP:7203", "JP:6758"):     # CN first in every category
            store.add(label(listing_id=lid, category=cat))
    test = [x for x in store.all() if x["grp"] == "test"]
    assert len(test) == len(CATEGORIES)                                  # one in four per category
    assert sum(x["listing_id"].startswith("CN:") for x in test) == len(CATEGORIES) // 2


def test_a_label_cannot_see_past_its_cutoff_or_redraw_itself(paths):
    store = Labels(paths)
    with pytest.raises(LabelError, match="截止日"):
        store.add(label(touches=[{"date": D(45), "side": "top", "price": 12.0}]))
    with pytest.raises(LabelError, match="截止日"):
        store.add(label(exclusions=[{"date": D(41), "kind": "刺穿", "price": 12.6, "reason": ""}]))
    with pytest.raises(LabelError, match="价格"):
        store.add(label(top=float("inf")))
    lid = store.add(label())
    with pytest.raises(LabelError, match="自己"):
        store.update(lid, {"redraw_of": lid})


def test_gallery_verdicts_take_a_whole_version_number_only(paths):
    from wb.workspace.labels import HitLabels
    with pytest.raises(LabelError):
        HitLabels(paths).set("r1", "CN:600000", D(5), "right", version="abc")


def test_a_label_is_redrawn_once_and_only_a_week_after_it_was_drawn(paths):
    store = Labels(paths)
    lid = store.add(label())
    with pytest.raises(LabelError, match="一周"):
        store.add(label(redraw_of=lid))
    store.conn.execute("UPDATE box_label SET created_at='2000-01-01T00:00:00' WHERE id=?", (lid,))
    store.conn.commit()
    rid = store.add(label(redraw_of=lid, top=12.1))
    with pytest.raises(LabelError, match="只重画一次"):
        store.add(label(redraw_of=lid))
    store.update(rid, {"top": 12.2})                    # the redraw itself can still be corrected
    with pytest.raises(LabelError, match="重画"):
        store.add(label(redraw_of=rid))                 # a redraw is a baseline, not a sample to redraw
