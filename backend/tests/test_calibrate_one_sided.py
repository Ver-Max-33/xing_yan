"""Fails on HEAD (12 marks, all right: no warning), passes with patches/calibrate_one_sided_marks.diff."""
from tests.test_l3_preview import TUNED
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l3.calibrate import calibrate, group
from wb.engine.l3.preview import preview
from wb.engine.l3.rule import load_rule


def test_marks_of_one_kind_only_are_said_to_push_one_way(paths):
    # your 12 marks on 天品 (10-13) are all 对: the calibration then can only recommend looser settings (vol 1.0 → 0.9, hits
    # 879 → 952), and the 7:3 groups by hash put 11 in 调参 and 1 in 检验 — said, not silently scored
    write(paths, 320)
    rule = load_rule(TUNED)
    marks = []
    for lid in Store(paths, "CN").read("instruments")["listing_id"].to_list():
        for d in preview(paths, rule, "CN", lid).signals["signal_date"].to_list():
            marks.append({"listing_id": lid, "signal_date": d, "verdict": "right"})
        if len(marks) >= 12:
            break
    marks = marks[:12]
    r = calibrate(lambda m: paths, rule, marks, years=2)
    assert r["warning"] and "没有标错的" in r["warning"]
    if sum(group(m["listing_id"], m["signal_date"]) == "test" for m in marks) < 3:
        assert "检验组只有" in r["warning"]
