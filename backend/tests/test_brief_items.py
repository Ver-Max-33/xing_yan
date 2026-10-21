"""开盘前简报's items (spec §7.1, 2026-10-10): the 选股 views you put into the briefing — a rule, its 附加条件 (any number,
all held on the scan's day) and maybe 同时满足另一条规则 — each listed or counted, in your order. Nothing until you put
one in; a view it could not use (a rule not scanned every day, a condition no market can read, the same view twice)
is refused with why."""
import pytest

from wb.devdata import write
from wb.screening.screen import Unreadable
from wb.workspace.briefing import BriefItems
from wb.workspace.rules import Rules

SHORT = "MA(BAR_LEN, 20) <= 0.03"
EACH = "CN: RAW_C > 10; JP: RAW_C > 1000"   # each market its own part


@pytest.fixture
def items(paths):
    write(paths, 320)
    for r in ("tpl_shang_pin", "tpl_tian_pin"):
        Rules(paths).set_state(r, enabled=True)
    return BriefItems(lambda m: paths)



def test_nothing_until_you_put_a_view_in_then_your_order(items):
    assert items.list() == []
    a = items.add("tpl_shang_pin", wheres=[SHORT, " ", f" {SHORT}"])
    b = items.add("tpl_shang_pin", wheres=[EACH])                       # the same rule, another condition: its own item
    c = items.add("tpl_tian_pin", also="tpl_shang_pin", also_status="watch", mode="count")
    assert [(x["id"], x["rule_id"], x["wheres"], x["also"], x["also_status"], x["mode"]) for x in items.list()] == [
        (a, "tpl_shang_pin", [SHORT], None, None, "list"), (b, "tpl_shang_pin", [EACH], None, None, "list"),
        (c, "tpl_tian_pin", [], "tpl_shang_pin", "watch", "count")]
    items.move(c, -1)
    items.set_mode(a, "count")
    items.remove(b)
    assert [(x["id"], x["mode"]) for x in items.list()] == [(a, "count"), (c, "count")]
    items.move(a, -1)                                                    # already first: stays
    assert [x["id"] for x in items.list()] == [a, c]


def test_a_view_the_briefing_could_not_use_is_refused_with_why(items):
    with pytest.raises(ValueError, match="没有启用"):
        items.add("long13")                                              # not scanned every day
    with pytest.raises(ValueError, match="没有规则"):
        items.add("no_such_rule")
    with pytest.raises(ValueError, match="附加条件"):
        items.add("tpl_shang_pin", wheres=["NO_SUCH(1) > 0"])
    items.add("tpl_shang_pin", wheres=[SHORT])
    with pytest.raises(ValueError, match="已经在简报里"):
        items.add("tpl_shang_pin", wheres=[f"  {SHORT}"])
    for bad in ({"mode": "all"}, {"also": "tpl_tian_pin", "also_status": "maybe"}, {"also": "no_such_rule"}):
        with pytest.raises(ValueError):
            items.add("tpl_tian_pin", **bad)
    with pytest.raises(KeyError):
        items.set_mode(999, "list")
    assert len(items.list()) == 1


def test_a_condition_one_market_cannot_read_now_is_kept_and_only_one_no_market_can_read_is_refused(paths, tmp_path):
    # 2026-10-11, the user's PC: A 股 on 正式档, 日股 on 测试档 with no data at all. 小盘股 (CN: CIRC_MV …; JP: TOTAL_MV …)
    # was refused because 日股 could not read TOTAL_MV — the briefing tells a market that cannot read it in its own entry
    from wb.config import get_paths
    write(paths, 320)
    Rules(paths).set_state("tpl_shang_pin", enabled=True)
    nothing = get_paths("test", tmp_path / "empty")   # a market whose data profile holds nothing yet
    items = BriefItems(lambda m: paths if m == "CN" else nothing)
    items.add("tpl_shang_pin", wheres=["CN: RAW_C > 10; JP: TOTAL_MV / 1e8 <= 1000"])
    # both markets with data, the demo data without market values: the one that can read its part keeps the view
    both = BriefItems(lambda m: paths)
    both.add("tpl_shang_pin", wheres=["CN: RAW_C > 20; JP: TOTAL_MV / 1e8 <= 1000"])
    with pytest.raises(ValueError, match=r"附加条件读不了（TOTAL_MV / 1e8 <= 1000）：CN：.*TOTAL_MV.*；JP：.*TOTAL_MV"):
        both.add("tpl_shang_pin", wheres=["TOTAL_MV / 1e8 <= 1000"])   # no market can read it now
    with pytest.raises(ValueError, match="不认识"):
        items.add("tpl_shang_pin", wheres=["NO_SUCH(1) > 0"])          # a market without data still reads the formula
    # a market without data neither keeps a view nor refuses it: here the only market with data cannot read it
    with pytest.raises(ValueError, match=r"附加条件读不了（CN: TOTAL_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000）：CN：.*TOTAL_MV"):
        items.add("tpl_shang_pin", wheres=["CN: TOTAL_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000"])
    Rules(nothing).set_state("tpl_shang_pin", enabled=True)
    nowhere = BriefItems(lambda m: nothing)   # no market has data yet: the formula alone is checked
    nowhere.add("tpl_shang_pin", wheres=["TOTAL_MV / 1e8 <= 1000"])


def test_the_ready_conditions_are_named_as_the_screen_page_names_them():
    # the briefing names a ready 附加条件 by its words (screen.WHERE_NAMES); the 选股 page's list is lib/jobs.ts WHERE_PRESETS
    import re
    from pathlib import Path

    from wb.screening.screen import WHERE_NAMES
    ts = (Path(__file__).resolve().parents[2] / "frontend/src/lib/jobs.ts").read_text(encoding="utf-8")
    page = dict((e, n) for n, e in re.findall(r'\{ label: "([^"]+)", expr: "([^"]+)"', ts))
    assert page == WHERE_NAMES and page["CN: FOREIGN_HOLDERS >= 2"] == "多个外资持股"


def test_a_condition_whose_data_has_not_come_yet_says_so_not_that_it_is_wrong(paths):
    # 10-17: 「附加条件有误（CN: FOREIGN_HOLDERS >= 2）：第 1 个字符：…在当前数据档里没有」 — the formula was right; the
    # holder lists had not been fetched yet
    from wb.screening.screen import NotYet, check
    write(paths, 320)
    with pytest.raises(NotYet, match="A 股还没有取到前十大流通股东的数据.*更新"):
        check(paths, "CN", "CN: FOREIGN_HOLDERS >= 2")
    with pytest.raises(Unreadable) as e:                                   # a formula written wrong is still that
        check(paths, "CN", "FOREIGN_HOLDERS >=")
    assert not isinstance(e.value, NotYet)
