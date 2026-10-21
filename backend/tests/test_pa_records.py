"""价格行为分析 (design 2026-10-20 §5.1–§5.3, §8): analyses kept in their own tables — running to ok or failed, 盘中 when
day 0 is older than the session already under way, the first of the day for the evaluation, the previous plan counted
in trading days, the first open after the analysis, and a restart closing what was running."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from tests.pa_fixtures import pa_market, plan
from wb.ai.client import LLM
from wb.data.store import Store
from wb.datasets import conform
from wb.pa.frame import daily_input, with_previous
from wb.pa.records import (calendar, create, finish, get, history, intraday, previous_plan, recent, recover, same,
                           start_date)
from wb.pa.run import Result, prompt_version
from wb.pa.settings import save_task
from wb.workspace.store import Workspace

TZ = ZoneInfo("Asia/Shanghai")
LID = "CN:600000"
LIMIT, NO = plan(), plan(order_type="不下单", order_direction=None, entry_price=None, stop_loss_price=None,
                         take_profit_price=None, take_profit_price_2=None)
CAL = [date(2026, 9, 1) + timedelta(days=i) for i in range(60) if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5]


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def ok_result(**kw) -> Result:
    return Result(**{"status": "ok", "stage1": {"direction": "bullish"}, "stage2": {"decision": LIMIT}, "ai_plan": LIMIT,
                     "final_plan": LIMIT, "call_ids": ["c1", "c2"], "cost": 0.5, "latency_ms": 900, "prompt_hash": "h", **kw})


def more_days(paths, n: int) -> None:
    """The exchange's calendar already knows the next `n` weekdays (the bars do not)."""
    st = Store(paths, "CN")
    last = st.latest().as_of
    days = [last + timedelta(days=i) for i in range(1, 2 * n + 3) if (last + timedelta(days=i)).weekday() < 5][:n]
    st.publish({"calendar": conform(pl.DataFrame({"date": days, "is_open": True, "source": "t"}), "calendar")}, as_of=last)


def test_a_record_from_running_to_ok_with_both_plans(paths):
    pa_market(paths)
    more_days(paths, 3)
    inp = daily_input(paths, LID)
    aid = create(paths, inp, previous=None, now=at("2026-09-30 20:00"), model="deepseek-v4-pro")
    row = get(paths, aid)
    assert row["status"] == "running" and row["first_of_day"] == 1 and row["intraday"] == 0 and row["trigger"] == "manual"
    assert row["input"]["k1_close"] == inp.k1_close and len(row["input"]["bars"]) == 100 and row["model"] == "deepseek-v4-pro"
    finish(paths, aid, ok_result(ai_plan=plan(stop_loss_price=9.7)))
    row = get(paths, aid)
    assert row["status"] == "ok" and row["final_plan"] == LIMIT and row["ai_plan"]["stop_loss_price"] == 9.7
    assert row["start_date"] == "2026-10-01" and row["call_ids"] == ["c1", "c2"] and row["cost"] == 0.5


def test_previous_plan_is_the_newest_with_an_order_counted_in_trading_days(paths):
    pa_market(paths)
    inp, cal = daily_input(paths, LID), calendar(paths)
    d = inp.day
    d3, d1 = cal[cal.index(d) - 3], cal[cal.index(d) - 1]
    a = create(paths, replace(inp, day=d3), previous=None, now=at(f"{d3} 20:00"), model="m")
    finish(paths, a, ok_result())
    b = create(paths, replace(inp, day=d1), previous=None, now=at(f"{d1} 20:00"), model="m")
    finish(paths, b, ok_result(final_plan=NO))
    p = previous_plan(paths, LID, d, calendar=cal)
    assert p.id == a and p.bars == 3 and p.decision == LIMIT and p.time == get(paths, a)["created_at"]
    assert previous_plan(paths, "CN:600001", d, calendar=cal) is None


def test_same_day_rerun_is_not_first_of_day_and_its_last_plan_is_from_an_earlier_day(paths):
    # the same bars' other analysis is not a plan 「1 bar ago」, and taking it so made every click after a buy a new input
    # (10-21)
    pa_market(paths)
    inp, cal = daily_input(paths, LID), calendar(paths)
    d2 = cal[cal.index(inp.day) - 2]
    a = create(paths, replace(inp, day=d2), previous=None, now=at(f"{d2} 20:00"), model="m")
    finish(paths, a, ok_result())
    b = create(paths, inp, previous=previous_plan(paths, LID, inp.day, calendar=cal), now=at("2026-09-30 20:00"), model="m")
    finish(paths, b, ok_result())
    prev = previous_plan(paths, LID, inp.day, calendar=cal)
    c = create(paths, inp, previous=prev, now=at("2026-09-30 20:05"), model="m")
    assert get(paths, c)["first_of_day"] == 0 and prev.id == a and prev.bars == 2 and get(paths, c)["previous_id"] == a


def test_the_same_input_is_the_same_analysis_until_our_own_code_around_pa_changes(paths, monkeypatch):
    # final review 5: the key had the prompts and PA's patch, not wb/pa — the guard, the limits the table shows, the
    # calls — so after a change there the next click gave the old result, saying the rules had not changed
    pa_market(paths)
    inp = daily_input(paths, LID)
    a = create(paths, inp, previous=None, now=at("2026-09-30 20:00"), model="m", task={"model": "m"})
    finish(paths, a, ok_result(prompt_hash=prompt_version()))
    assert same(paths, inp, previous=None, task={"model": "m"}) == a
    monkeypatch.setattr("wb.pa.records.code_version", lambda: "wb/pa changed")
    assert same(paths, inp, previous=None, task={"model": "m"}) is None


def test_a_failed_first_try_does_not_use_up_the_day(paths):
    pa_market(paths)
    inp = daily_input(paths, LID)
    a = create(paths, inp, previous=None, now=at("2026-09-30 20:00"), model="m")
    finish(paths, a, Result(status="failed", error="x", call_ids=["c1"], cost=0.1))
    b = create(paths, inp, previous=None, now=at("2026-09-30 20:05"), model="m")
    assert get(paths, b)["first_of_day"] == 1 and get(paths, a)["error"] == "x"


def test_intraday_and_start_date():
    assert intraday(date(2026, 9, 29), at("2026-09-30 08:00"), CAL) is False     # before the open: yesterday is the latest
    assert intraday(date(2026, 9, 29), at("2026-09-30 14:30"), CAL) is True      # 盘中: today's bar is not in yet
    assert intraday(date(2026, 9, 29), at("2026-09-30 15:30"), CAL) is True      # closed, the data not stored yet
    assert intraday(date(2026, 9, 30), at("2026-09-30 20:00"), CAL) is False
    assert intraday(date(2026, 10, 2), at("2026-10-03 11:00"), CAL) is False     # a Saturday
    assert start_date(at("2026-09-30 10:00"), CAL) == date(2026, 10, 1)       # made during a session: the next open
    assert start_date(at("2026-10-03 11:00"), CAL) == date(2026, 10, 5)       # on a Saturday: Monday's
    assert start_date(at("2026-10-23 20:00"), CAL[:CAL.index(date(2026, 10, 23)) + 1]) is None   # no day after it yet


def test_history_newest_first_and_recent_without_the_heavy_parts(paths):
    pa_market(paths)
    inp = daily_input(paths, LID)
    a = create(paths, inp, previous=None, now=at("2026-09-30 20:00"), model="m")
    b = create(paths, inp, previous=None, now=at("2026-09-30 20:05"), model="m")
    assert [r["id"] for r in history(paths, LID)] == [b, a] and history(paths, "CN:600001") == []
    assert [r["id"] for r in recent(paths)] == [b, a] and not {"input", "stage1", "stage2"} & set(recent(paths)[0])


def test_recover_marks_a_running_row_failed(paths):                            # Review Focus 4
    pa_market(paths)
    aid = create(paths, daily_input(paths, LID), previous=None, now=at("2026-09-30 20:00"), model="m")
    assert recover(paths) == 1 and get(paths, aid)["error"] == "应用重启，分析中断" and get(paths, aid)["status"] == "failed"
    assert recover(paths) == 0


def test_main_ai_and_settings_still_read_after_dev_wrote(paths):
    pa_market(paths)
    create(paths, daily_input(paths, LID), previous=None, now=at("2026-09-30 20:00"), model="m")
    save_task(paths, {"provider": "deepseek", "model": "deepseek-flash", "effort": "low"})
    assert Workspace(paths).settings()["pa.model"] and LLM(paths).status()["tasks"].keys() == {"rule_assistant", "research"}


def test_a_previous_plan_from_before_an_ex_date_moves_to_todays_prices(paths):   # final review, Important 1
    pa_market(paths)                                       # ex-rights ÷1.25 at bar 120 of 160
    inp, cal = daily_input(paths, LID), calendar(paths)
    d0 = cal[115]                                          # before it: that day 0's 前复权 prices are 1.25× today's
    a = create(paths, replace(inp, day=d0, adj0=1.0), previous=None, now=at(f"{d0} 20:00"), model="m")
    finish(paths, a, ok_result())                          # LIMIT: entry 10.00, stop 9.60, TP1 10.40, TP2 10.80
    prev = previous_plan(paths, LID, inp.day, calendar=cal)
    plan = dict(with_previous(inp, prev).frame.previous_plan)
    assert inp.adj0 == pytest.approx(1.25) and prev.decision["entry_price"] == 10.0
    assert [plan[k] for k in ("entry_price", "stop_loss_price", "take_profit_price", "take_profit_price_2")] == [8.0, 7.68, 8.32, 8.64]
