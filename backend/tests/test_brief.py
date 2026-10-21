"""开盘前简报 (spec §7.1, 1.34): one card per market and trading day, before the open."""
from datetime import date, datetime

import polars as pl
import pytest

from tests.test_l3_preview import TUNED
from wb.devdata import END, write
from wb.notify.brief import Inputs, RuleDay, announce, brief_on, compose, gather, make_brief, shown
from wb.screening.scan import enabled_rules, scan
from wb.screening.screen import holds
from wb.workspace.briefing import BriefItems
from wb.workspace.rules import Rules
from wb.workspace.store import Workspace
from wb.workspace.watch import Watch


def test_the_briefing_follows_the_profile_until_you_set_it(paths):
    ws = Workspace(paths)
    s = ws.settings()
    assert brief_on(s, "CN", "prod") and not brief_on(s, "JP", "test")       # a test profile's data is only a sample
    ws.set_setting("brief.JP", "on")
    assert brief_on(ws.settings(), "JP", "test")
    ws.set_setting("brief.CN", "off")
    assert not brief_on(ws.settings(), "CN", "prod")
    assert ws.settings()["brief.time.CN"] == "08:50" and ws.settings()["brief.time.JP"] == "08:40"
    ws.set_setting("brief.time.CN", "09:05")
    for bad in ("9:05", "24:00", "08:60", 905):
        with pytest.raises(ValueError):
            ws.set_setting("brief.time.CN", bad)
    with pytest.raises(ValueError):
        ws.set_setting("brief.CN", "maybe")
    with pytest.raises(ValueError):
        ws.set_setting("brief.per_rule", 0)


def test_a_briefing_without_items_says_how_to_choose_them():
    # 2026-10-10: the briefing lists only the 选股 views you put into it — none yet, it says where to
    b = compose(inputs(chosen=0))
    assert any("放进开盘前简报" in h for h in b["head"])
    assert not any("放进开盘前简报" in h for h in compose(inputs(chosen=1))["head"])


D, TODAY = date(2026, 9, 30), date(2026, 10, 8)


def sig(lid, vol=None, box=None):
    """A signal row: the structure (`box`: (scale, bars)) and a volume ratio its rule computed."""
    b = [{"stage": "box", "kind": "structure", "id": f"{lid}:{box[0]}:1", "bars": box[1]}] if box else []
    c = [{"id": "volume", "expr": "VOL_RATIO(5) >= $vol", "kind": "at", "value": vol}] if vol is not None else []
    return {"listing_id": lid, "status": "signal", "signal_date": D, "detail": {"bindings": b, "conditions": c, "distances": []}}


def latent(lid, top):
    return {"listing_id": lid, "status": "watch", "signal_date": None,
            "detail": {"bindings": [], "conditions": [], "distances": [{"id": "break_up", "value": 0.0, "threshold": top}]}}


def inputs(**kw):
    base = dict(market="CN", day=TODAY, data_day=D, expected=D, names={}, quotes={})
    return Inputs(**{**base, **kw})


def stocks(section):
    return [x["listing_id"] for x in section["lines"] if x["kind"] == "stock"]


def test_each_rules_new_signals_are_listed_once_its_best_first_and_capped():
    quotes = {f"CN:00000{i}": {"close": 10.0, "pre_close": 9.5, "amount": i * 1e8} for i in range(1, 6)}
    a = RuleDay("a", "天品", "list", [sig(f"CN:00000{i}", vol=1.5) for i in range(1, 6)], 5, 4, ranked=False)
    b = RuleDay("b", "长颈鹿", "list", [sig("CN:000005"), sig("CN:000001")], 2, 2, ranked=True)
    out = compose(inputs(rules=[a, b], quotes=quotes, names={"CN:000005": "五号"}), per_rule=3)
    s = next(x for x in out["sections"] if x["title"].startswith("新信号"))
    assert s["title"] == "新信号（09-30 收盘，每项最多 3 只）"
    assert [x["text"] for x in s["lines"] if x["kind"] == "group"] == ["天品 · 5 只", "长颈鹿 · 2 只"]
    assert stocks(s) == ["CN:000005", "CN:000004", "CN:000003", "CN:000001"]   # by turnover; a stock once; the rule's own order
    five = next(x for x in s["lines"] if x.get("listing_id") == "CN:000005")
    assert five["name"] == "五号" and five["text"] == "收 10.00 +5.3% · 额 5.0 亿 · 量比 1.50 · （也是长颈鹿）"
    assert s["lines"][-1] == {"kind": "note", "text": "另有 1 只，见应用「今日」"}           # 000002: not shown
    assert shown(out)[:2] == ["CN:000005", "CN:000004"]


def test_the_latent_stocks_nearest_their_box_top_are_listed_nearest_first():
    quotes = {"CN:600001": {"close": 10.0}, "CN:600002": {"close": 10.0}, "CN:600003": {"close": 10.0}}
    r = RuleDay("a", "上横上", "list", [latent("CN:600001", 10.25), latent("CN:600002", 10.05), latent("CN:600003", 10.5)],
                0, 0, ranked=False)
    s = next(x for x in compose(inputs(rules=[r], quotes=quotes))["sections"] if x["title"].startswith("快到突破"))
    assert s["title"] == "快到突破（离上沿 3% 以内）" and stocks(s) == ["CN:600002", "CN:600001"]   # 5% is too far
    assert s["lines"][0]["text"] == "收 10.00 · 上沿 10.05（差 0.5%） · 上横上"


def test_near_the_box_top_is_measured_to_the_top_not_the_break_line():
    """A 潜伏 row as a scan writes it: threshold = the break line (上沿 × 1.015), edge = the 上沿 itself."""
    def row(lid, top):
        return {"listing_id": lid, "status": "watch", "signal_date": None, "detail": {"bindings": [], "conditions": [],
                "distances": [{"id": "break_up", "value": 10.0, "threshold": top * 1.015, "edge": top}]}}
    quotes = {"CN:600001": {"close": 10.0}, "CN:600002": {"close": 10.0}, "CN:600003": {"close": 10.0}}
    r = RuleDay("a", "上横上", "list", [row("CN:600001", 10.28), row("CN:600002", 9.95), row("CN:600003", 10.05)], 0, 0, ranked=False)
    s = next(x for x in compose(inputs(rules=[r], quotes=quotes))["sections"] if x["title"].startswith("快到突破"))
    # 600001: 2.8% under its top (4.3% under the line) is near; 600002 is over its top already; 600003 0.5% under
    assert stocks(s) == ["CN:600003", "CN:600001"]
    assert s["lines"][0]["text"] == "收 10.00 · 上沿 10.05（差 0.5%） · 上横上"


def test_your_watched_stocks_are_listed_only_when_something_happened():
    r = RuleDay("a", "天品", "list", [sig("CN:000001")], 1, 0, ranked=False)
    watched = [{"listing_id": "CN:000001", "fired": []}, {"listing_id": "CN:000002", "fired": []},
               {"listing_id": "CN:000003", "fired": [{"kind": "fail", "text": "收盘跌回箱顶", "close": 9.0}]},
               {"listing_id": "CN:000004", "fired": [{"kind": "review", "text": "看半年报", "close": None}]}]
    quotes = {"CN:000001": {"close": 10.0, "pre_close": 10.0}}
    s = next(x for x in compose(inputs(rules=[r], watched=watched, quotes=quotes))["sections"] if x["title"].startswith("你的观察"))
    assert s["title"] == "你的观察（4 只里 3 只有事）"
    assert [x["text"] for x in s["lines"]] == ["出信号：天品 · 收 10.00 +0.0%", "失效条件：收盘跌回箱顶（收 9.00）", "该复核了：看半年报"]


def test_the_screeners_say_how_many_and_how_that_changed():
    rules = [RuleDay("x", "养箱", "count", [], 273, 261, False), RuleDay("y", "爆大量", "count", [], 260, None, False),
             RuleDay("z", "复盘", "count", [], 0, 0, False, error="缺数据"), RuleDay("w", "N 天连板", "off", [], 9, 9, False)]
    b = compose(inputs(rules=rules))
    s = next(x for x in b["sections"] if x["title"] == "选股器")
    assert s["lines"] == [{"kind": "text", "text": "养箱 273（+12） · 爆大量 260"}] and "「复盘」缺数据" in b["head"]


def test_the_first_lines_say_which_close_it_reads_and_warn_when_that_is_not_the_expected_one():
    ok = compose(inputs(index={"name": "上证指数", "close": 3842.19, "dist60": -0.016, "dist200": -0.042, "state": -1},
                        breadth={"total": 5572, "up": 2567, "limit_up": 56, "limit_down": 13}))
    assert ok["title"] == "A 股 · 10-08 周四 开盘前" and ok["warning"] is None              # 9-30 then a holiday: not missing
    assert ok["head"] == ["数据截至 09-30 收盘", "大盘：上证指数 3,842.19 · 在 60、200 日线之下 · 下降 · 上涨 46% · 涨停 56 · 跌停 13"]
    late = compose(inputs(data_day=date(2026, 9, 29), problem="数据到 20:00 还没齐", late=True, pending=True))
    assert late["title"].endswith("（补发）")
    assert late["warning"] == "09-30 的数据没取到：数据到 20:00 还没齐；下面是 09-29 收盘的结果"
    assert late["head"][0] == "数据截至 09-29 收盘（最新的数据还没扫完，下面是上一次扫描的结果）"   # a correction, or a scan you cancelled
    assert compose(inputs())["head"][1] == "大盘：没有指数数据，算不出来"


def test_a_market_kept_out_of_notifications_has_no_numbers():
    r = RuleDay("a", "天品", "list", [sig("CN:000001", vol=2.0, box=("mid", 39)), latent("CN:000002", 10.1)], 1, 0, False)
    quotes = {"CN:000001": {"close": 10.0, "pre_close": 9.0, "amount": 1e9}, "CN:000002": {"close": 10.0}}
    out = compose(inputs(rules=[r], quotes=quotes, quiet=True,
                         index={"name": "上证指数", "close": 3842.0, "dist60": 0.1, "dist200": 0.1, "state": 1},
                         breadth={"total": 10, "up": 5, "limit_up": 1, "limit_down": 0}))
    text = repr(out)
    assert "10.00" not in text and "额" not in text and "量比" not in text and "上沿 10" not in text and "3,842" not in text
    assert out["head"][1] == "大盘：上证指数 · 在 60、200 日线之上 · 上升"


def test_a_day_with_nothing_to_look_at_is_one_line_or_nothing():
    out = compose(inputs())
    assert [s["title"] for s in out["sections"]] == ["今天没有要看的"]
    assert compose(inputs(), empty=False) is None
    assert compose(inputs(expected=date(2026, 10, 1)), empty=False) is not None          # a warning is always said


def test_announcements_are_said_and_their_absence_too():
    r = RuleDay("a", "天品", "list", [sig("CN:000001")], 1, 0, False)
    ann = {"CN:000001": [{"day": D, "title": "关于竞得土地使用权的公告"}, {"day": D, "title": "董事会决议公告"},
                         {"day": D, "title": "监事会决议公告"}]}
    out = compose(inputs(rules=[r], announcements=ann, quotes={"CN:000001": {"close": 10.0}}))
    line = next(x for s in out["sections"] for x in s["lines"] if x.get("listing_id") == "CN:000001")
    assert line["text"].endswith("公告：关于竞得土地使用权的公告；董事会决议公告 等 3 篇")
    assert compose(inputs(ann_problem="巨潮连不上"))["footer"].endswith(" · 公告没取到：巨潮连不上")


def test_the_structure_and_the_volume_read_by_their_names():
    r = RuleDay("a", "下横上", "list", [sig("CN:000001", vol=1.03, box=("long", 66))], 1, 0, False)
    out = compose(inputs(rules=[r], quotes={"CN:000001": {"close": 19.11, "pre_close": 17.76, "amount": 2.1e8}}))
    line = next(x for s in out["sections"] for x in s["lines"] if x.get("listing_id") == "CN:000001")
    assert line["text"] == "收 19.11 +7.6% · 额 2.1 亿 · 大箱 66 根 · 量比 1.03"


def scanned(paths):
    """Demo data with one enabled rule scanned on its last day (END)."""
    write(paths, 320)
    Rules(paths).save(TUNED)
    Rules(paths).set_state("above_ma", enabled=True)
    scan(paths, "CN", enabled_rules(paths), day=END)


def test_a_briefing_reads_the_rules_last_scan_the_closes_and_the_market(paths):
    scanned(paths)
    BriefItems(lambda m: paths).add("above_ma")
    inp = gather(paths, "CN", date(2026, 10, 1), END, {"scanned_snapshot": None, "message": None})
    assert inp.data_day == END and inp.expected == END
    [r] = inp.rules
    assert r.name == "站上 20 日线" and r.mode == "list" and r.prev_signals is None and r.signals > 0
    lid = next(x["listing_id"] for x in r.rows if x["status"] == "signal")
    assert inp.quotes[lid]["close"] > 0 and inp.names[lid]
    assert inp.index["name"] == "上证指数" and inp.breadth["total"] > 0
    assert inp.pending is True                                                   # the state does not say it was scanned


def test_an_item_the_briefing_cannot_read_today_says_why_and_one_taken_out_is_gone(paths):
    scanned(paths)
    items = BriefItems(lambda m: paths)
    Rules(paths).save(TUNED.replace("id: above_ma", "id: later").replace("站上 20 日线", "后来启用"))
    Rules(paths).set_state("later", enabled=True)                                # enabled after the scan: no run that day
    a, b = items.add("above_ma"), items.add("later")
    got = gather(paths, "CN", date(2026, 10, 1), END, {}).rules
    assert [(r.rule_id, r.error) for r in got] == [("above_ma", None), ("later", "没有 09-30 的扫描")]
    Rules(paths).set_state("above_ma", enabled=False)                            # turned off since
    assert gather(paths, "CN", date(2026, 10, 1), END, {}).rules[0].error == "没有启用，不在每日扫描里"
    items.remove(a)
    items.remove(b)
    assert gather(paths, "CN", date(2026, 10, 1), END, {}).rules == []


def test_an_items_conditions_and_the_other_rule_narrow_its_stocks_and_name_it(paths):
    # the 选股 view as it is: the stocks every 附加条件 holds for on the scan's day, and the other rule found too
    scanned(paths)
    Rules(paths).save(TUNED.replace("id: above_ma", "id: twin").replace("站上 20 日线", "同样的规则"))
    Rules(paths).set_state("twin", enabled=True)
    scan(paths, "CN", [r for r in enabled_rules(paths) if r[0].id == "twin"], day=END)
    items = BriefItems(lambda m: paths)
    items.add("above_ma")
    every = {x["listing_id"] for x in gather(paths, "CN", date(2026, 10, 1), END, {}).rules[0].rows}
    items.add("above_ma", wheres=["MA(BAR_LEN, 20) <= 0.03", "RAW_C <= 20"], also="twin", also_status="watch")
    _, narrow = gather(paths, "CN", date(2026, 10, 1), END, {}).rules
    assert narrow.name == "站上 20 日线 · 近期 K 线短小、RAW_C <= 20 · 同时满足「同样的规则」（命中或潜伏）"
    short, cheap = holds(paths, "CN", "MA(BAR_LEN, 20) <= 0.03", END)[0], holds(paths, "CN", "RAW_C <= 20", END)[0]
    assert {x["listing_id"] for x in narrow.rows} == every & short & cheap        # the twin found all of them too
    assert narrow.signals == sum(x["status"] == "signal" for x in narrow.rows) and narrow.prev_signals is None


def test_an_item_whose_other_rule_is_for_another_market_is_left_out_not_said_unscanned(paths):
    # 10-13: 上品 同时满足「平稳横盘后向上突破」 (A 股 only) put 「…」没有 10-09 的扫描 at the top of every 日股 briefing
    scanned(paths)
    Rules(paths).save(TUNED.replace("id: above_ma", "id: jp_only").replace("站上 20 日线", "只看日股").replace("[CN, JP]", "[JP]"))
    Rules(paths).set_state("jp_only", enabled=True)
    items = BriefItems(lambda m: paths)
    items.add("above_ma")
    items.add("above_ma", also="jp_only")
    assert [(r.rule_id, r.error) for r in gather(paths, "CN", date(2026, 10, 1), END, {}).rules] == [("above_ma", None)]


def test_a_condition_no_longer_readable_says_so_for_its_item_only(paths):
    # saved when it could be read; the data has since lost what it names (here written straight into the table)
    scanned(paths)
    BriefItems(lambda m: paths).add("above_ma")
    from wb.db import connect
    connect(paths.shared_db, "shared").execute(
        "INSERT INTO brief_item (rule_id, wheres, mode, sort, created_at) VALUES ('above_ma', '[\"NO_SUCH(1) > 0\"]', 'list', 9, 'x')")
    ok, bad = gather(paths, "CN", date(2026, 10, 1), END, {}).rules
    assert ok.error is None and bad.error.startswith("附加条件读不了（NO_SUCH(1) > 0）") and bad.rows == []


def test_your_watched_stocks_come_with_the_conditions_the_close_fired(paths):
    scanned(paths)
    w = Watch(lambda m: paths)
    w.add("CN:600519", now=datetime(2026, 9, 29, 17, 0))
    w.add_condition("CN:600519", "fail", "收盘跌回箱顶", op="<", price=10.0**9)
    w.check("CN", now=datetime(2026, 9, 30, 16, 30))
    [x] = gather(paths, "CN", date(2026, 10, 1), END, {}).watched
    assert x["listing_id"] == "CN:600519" and x["fired"][0]["kind"] == "fail" and x["fired"][0]["close"] > 0


class Cninfo:
    def __init__(self, fail=False):
        self.asked, self.fail = [], fail

    def announcements(self, lid, start, end):
        self.asked.append((lid, start, end))
        if self.fail:
            raise ConnectionError("连不上")
        return pl.DataFrame({"listing_id": [lid] * 3, "doc_id": ["1", "2", "3"], "day": [END] * 3,
                             "title": ["H股公告-月报表", "关于竞得土地使用权的公告", "2026年中期报告（英文版）"],
                             "url": [""] * 3, "size_kb": [1.0] * 3, "source": ["cninfo"] * 3})


def test_announcements_are_fetched_only_for_the_stocks_named_routine_ones_left_out(paths):
    inp = Inputs(market="CN", day=date(2026, 10, 1), data_day=END, expected=END)
    c = Cninfo()
    announce(inp, c, ["CN:000002"], date(2026, 10, 1))
    assert c.asked == [("CN:000002", END, date(2026, 10, 1))]
    assert inp.announcements == {"CN:000002": [{"day": END, "title": "关于竞得土地使用权的公告"}]}
    bad = Inputs(market="CN", day=date(2026, 10, 1), data_day=END, expected=END)
    announce(bad, Cninfo(fail=True), ["CN:000002", "CN:000004"], date(2026, 10, 1))
    assert bad.announcements is None and bad.ann_problem.startswith("巨潮连不上")


def test_a_briefing_is_made_from_the_store_and_says_what_it_names_announcements_for(paths):
    scanned(paths)
    BriefItems(lambda m: paths).add("above_ma")
    c = Cninfo()
    b = make_brief(paths, "CN", date(2026, 10, 1), END, {}, cninfo=c)
    assert b["title"] == "A 股 · 10-01 周四 开盘前" and any(s["title"].startswith("新信号") for s in b["sections"])
    assert [a[0] for a in c.asked] == shown(b)                                   # only the stocks it names


def test_watched_stocks_say_their_new_danger_and_listed_ones_carry_its_tags():
    ws = {"CN:600519": [{"kind": "limit_up", "label": "连板后易回落", "new": True,
                         "brief": "连板后易回落：09-30 收盘涨停（连续 2 天）；过去十年连续 2 天涨停的股票之后约三个月平均少涨 5–7%"}],
          "CN:000001": [{"kind": "unlock", "label": "解禁前易走弱", "new": False, "brief": "x"}]}
    rule = RuleDay("r", "上品", "list", [sig("CN:000001")], 1, None, ranked=False)
    out = compose(inputs(rules=[rule], names={"CN:600519": "贵州茅台", "CN:000001": "平安银行"},
                         watched=[{"listing_id": "CN:600519", "fired": []}], danger=ws))
    lines = {x["listing_id"]: x["text"] for s in out["sections"] for x in s["lines"] if x.get("kind") == "stock"}
    assert "危险提醒：连板后易回落：09-30 收盘涨停" in lines["CN:600519"]      # new on the data day: said under 你的观察
    assert lines["CN:000001"].startswith("危险：解禁前易走弱")                 # a listed stock: its tags first
