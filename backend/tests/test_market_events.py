"""盘面变化 (design 2026-10-12 §4): what changed during the day, from the per-minute rows alone — a line crossed and
held, 封板率 moving to another band, the top board opening and sealing again, a new top industry, limit-downs piling
up. Debounced: a crossing must hold, coming back must go past a margin; the same rows always give the same events."""
from datetime import datetime, timedelta

from wb.market.events import events

INDEX = {"prev": 3800.0, "ma60_prev": 3900.0, "box": {"top": 4000.0, "bottom": 3700.0}}


def at(i: int) -> datetime:
    base = datetime(2026, 10, 12, 9, 30) if i < 120 else datetime(2026, 10, 12, 13, 0)
    return base + timedelta(minutes=i if i < 120 else i - 120, seconds=5)


def row(i: int, **over) -> dict:
    return {"i": i, "at": at(i), "sh_last": 3805.0, "seal_rate": 0.75, "limit_up": 40, "broken": 13, "limit_down": 2,
            "top_name": "电力", "top_chg": 0.02, "second_chg": 0.01, "sealed_ids": [], "broken_ids": [], **over}


def run(rows, top_ids=(), names=None, top_level=None):
    return events(rows, INDEX, top_ids, names or {}, top_level)


def titles(evs):
    return [(e["i"], e["title"]) for e in evs]


def test_the_open_gap_is_noted_with_where_the_index_stands():
    evs = run([row(0, sh_last=3784.8)] + [row(i, sh_last=3784.8) for i in range(1, 5)])
    assert titles(evs) == [(0, "上证低开 0.4%")]
    assert evs[0]["detail"] == "在 60 日线下 3.0%" and evs[0]["tone"] == "neutral"
    assert run([row(i, sh_last=3805.0) for i in range(5)]) == []           # +0.13%: no gap worth a line


def test_a_late_start_has_no_open_event():
    assert run([row(i, sh_last=3784.8) for i in range(30, 35)]) == []


def test_a_cross_held_two_rows_is_no_event_and_three_is():
    two = [row(i, sh_last=3790.0) for i in range(0, 5)] + [row(i, sh_last=3810.0) for i in (5, 6)] + [row(i, sh_last=3790.0) for i in range(7, 12)]
    assert run(two) == []
    three = [row(i, sh_last=3790.0) for i in range(0, 5)] + [row(i, sh_last=3810.0) for i in range(5, 12)]
    evs = run(three)
    assert titles(evs) == [(5, "上证翻红")] and evs[0]["tone"] == "up" and evs[0]["detail"] == "昨收 3,800.00"


def test_wobbling_inside_the_margin_is_no_cross():
    # 0.1% of 3,800 is 3.8 points: 3,802 and 3,798 are inside it
    assert run([row(i, sh_last=3790.0) for i in range(0, 5)] + [row(i, sh_last=3802.0 if i % 2 else 3798.0) for i in range(5, 30)]) == []


def test_the_index_flips_at_most_four_times_a_day():
    rows, price = [], 3790.0
    for k in range(10):                                                # ten flips, each held 3 rows
        price = 3810.0 if price < 3800 else 3790.0
        rows += [row(i, sh_last=price) for i in range(5 + 3 * k, 8 + 3 * k)]
    flips = [e for e in run([row(i, sh_last=3790.0) for i in range(5)] + rows) if e["kind"] == "close"]
    assert len(flips) == 4


def test_the_60_day_line_and_the_box_edges():
    rows = ([row(i, sh_last=3880.0) for i in range(0, 5)] + [row(i, sh_last=3920.0) for i in range(5, 10)]
            + [row(i, sh_last=4010.0) for i in range(10, 15)] + [row(i, sh_last=3990.0) for i in range(15, 20)])
    got = [(e["i"], e["title"], e["tone"]) for e in run(rows) if e["kind"] in ("ma60", "box")]
    assert got == [(5, "上证站上 60 日线", "up"), (10, "上证突破箱顶", "up"), (15, "上证回到箱体里", "warn")]


def test_seal_rate_wobbling_around_70_is_one_event():
    rows = ([row(i, seal_rate=0.75) for i in range(15, 21)]
            + [row(i, seal_rate=[0.69, 0.71, 0.68, 0.72][i % 4]) for i in range(21, 41)]
            + [row(i, seal_rate=0.66, broken=17) for i in range(41, 46)]
            + [row(i, seal_rate=[0.66, 0.71][i % 2]) for i in range(46, 60)])
    seal = [e for e in run(rows) if e["kind"] == "seal"]
    assert titles(seal) == [(41, "封板率跌破 70%")]
    assert seal[0]["tone"] == "warn" and seal[0]["detail"] == "09:45 时 75%，炸板 17 只"


def test_seal_rate_waits_for_the_15th_minute_and_ten_touches():
    early = [row(i, seal_rate=0.9) for i in range(0, 10)] + [row(i, seal_rate=0.5) for i in range(10, 14)]
    assert [e for e in run(early) if e["kind"] == "seal"] == []
    few = [row(i, seal_rate=0.9, limit_up=5, broken=1) for i in range(15, 20)] + [row(i, seal_rate=0.5, limit_up=3, broken=3) for i in range(20, 30)]
    assert [e for e in run(few) if e["kind"] == "seal"] == []


def test_the_top_board_breaks_once_and_reseals_after_five_rows():
    top = ("CN:600825",)
    rows = ([row(i, sealed_ids=list(top)) for i in range(0, 10)] + [row(i, broken_ids=list(top)) for i in range(10, 13)]
            + [row(i, sealed_ids=list(top)) for i in range(13, 16)] + [row(i, broken_ids=list(top)) for i in range(16, 18)]
            + [row(i, sealed_ids=list(top)) for i in range(18, 30)])
    got = [(e["i"], e["title"], e["tone"]) for e in run(rows, top, {"CN:600825": "新华传媒"}, 10) if e["kind"] == "top"]
    assert got == [(10, "最高板 新华传媒（10 板）打开涨停", "warn"), (18, "最高板 新华传媒 回封", "up")]   # when it resealed


def test_a_new_top_industry_needs_three_rows_a_lead_and_a_gap():
    rows = ([row(i) for i in range(0, 10)]
            + [row(i, top_name="半导体", top_chg=0.031, second_chg=0.02) for i in (10, 11)] + [row(12)]       # two rows: no
            + [row(i, top_name="半导体", top_chg=0.031, second_chg=0.029) for i in range(13, 17)]             # no lead
            + [row(i, top_name="半导体", top_chg=0.031, second_chg=0.02) for i in range(17, 21)]              # yes
            + [row(i, top_name="软件开发", top_chg=0.04, second_chg=0.03) for i in range(21, 26)]            # within 15 rows
            + [row(i, top_name="软件开发", top_chg=0.04, second_chg=0.03) for i in range(32, 40)])           # after the gap
    got = [(e["i"], e["title"], e["detail"]) for e in run(rows) if e["kind"] == "leader"]
    assert got == [(17, "半导体升到领涨第一", "+3.1%，领先第二名 1.1 个点"),        # whose number it is, and by how much
                   (32, "软件开发升到领涨第一", "+4.0%，领先第二名 1.0 个点")]


def test_the_leader_event_reads_the_l1_columns_and_old_rows_their_own():
    new = [row(i, top_name="半导体", top_chg=0.03, second_chg=0.0, top1_name=n, top1_chg=0.02, second1_chg=0.0)
           for i, n in enumerate(["电子"] * 5 + ["传媒"] * 20, start=1)]       # the 二级 leader never changes
    assert [(e["i"], e["title"]) for e in run(new) if e["kind"] == "leader"] == [(6, "传媒升到领涨第一")]
    old = [row(i, top_name=n, top_chg=0.02, second_chg=0.0) for i, n in enumerate(["半导体"] * 5 + ["出版"] * 20, start=1)]
    assert [(e["i"], e["title"]) for e in run(old) if e["kind"] == "leader"] == [(6, "出版升到领涨第一")]


def test_limit_downs_10_30_50_once_each():
    rows = [row(i, limit_down=n) for i, n in enumerate([2, 9, 10, 12, 8, 31, 29, 55, 60])]
    assert [e["title"] for e in run(rows) if e["kind"] == "limit_down"] == ["跌停增加到 10 只", "跌停增加到 30 只", "跌停增加到 50 只"]


def test_the_same_rows_give_the_same_events():
    rows = ([row(0, sh_last=3784.8)] + [row(i, sh_last=3810.0, seal_rate=0.62, broken=20) for i in range(1, 40)]
            + [row(i, limit_down=33) for i in range(40, 45)])
    assert run(rows) == run(list(rows)) and len(run(rows)) >= 3
    assert all(set(e) == {"i", "at", "kind", "tone", "title", "detail"} for e in run(rows))
