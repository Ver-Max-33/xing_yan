"""价格行为分析 (design 2026-10-20 §5.2–§5.5): settling the analyses every day — both plans × both targets traded by
A-share rules, a random control of 20 stocks per plan, the 5- and 20-day excess from the first open, the settled flag,
and the evaluation over each stock's first analysis of a day. Demo, 测试档 and failed analyses are never settled."""
import json
import statistics
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

from tests.pa_fixtures import pa_market, plan
from wb.config import get_paths
from wb.data.store import Store
from wb.datasets import conform
from wb.db import connect
from wb.pa import records, settle
from wb.pa.frame import daily_input
from wb.pa.run import Result
from wb.pa.settle import control, evaluation, pool, settle_all

LID = "CN:600000"


@pytest.fixture
def paths(tmp_path):              # 正式档: 测试档 rows are never settled
    p = get_paths("prod", tmp_path)
    pa_market(p, extra_listings=30)
    return p


def analysis(paths, back: int = 45, *, status: str = "ok", **inp_fields) -> str:
    """An analysis made `back` trading days before the data's last day, after that day's close; its plans a 限价单 1%
    under that day's real close, stop 5% under, targets 5% and 10% over (the AI's own stop 2% closer)."""
    inp = daily_input(paths, LID)
    cal = records.calendar(paths)
    d0 = cal[-1 - back]
    close = Store(paths, "CN").read("bars_1d").filter((pl.col("listing_id") == LID) & (pl.col("date") == d0))["close"][0]
    e = round(close * 0.99, 2)
    final = plan(entry_price=e, stop_loss_price=round(e * 0.95, 2), take_profit_price=round(e * 1.05, 2),
                 take_profit_price_2=round(e * 1.10, 2))
    aid = records.create(paths, replace(inp, day=d0, k1_close=close, **inp_fields), previous=None, model="m",
                         now=datetime.combine(d0, time(20), records.TZ))
    s1 = {"direction": "bullish", "cycle_position": "normal_channel", "gate_trace": [   # the AI overrode the program's vote
        {"node_id": "2.3", "answer": "是", "branch": "bullish", "overridden_by_ai": True, "program_branch": "neutral"}]}
    records.finish(paths, aid, Result(status=status, stage1=s1,
                                      stage2={"decision": final, "next_cycle_prediction": {"direction": "bullish"}},
                                      ai_plan={**final, "stop_loss_price": round(e * 0.97, 2)}, final_plan=final,
                                      call_ids=["c"], cost=0.5, prompt_hash="h"))
    return aid


def trade_rows(paths, aid):
    return [dict(r) for r in connect(paths.shared_db, "shared").execute("SELECT * FROM pa_trade WHERE analysis_id=?", (aid,))]


def test_settle_all_fills_both_tracks_and_targets_and_controls(paths):
    aid = analysis(paths)
    assert settle_all(lambda m: paths) == 1
    rows = trade_rows(paths, aid)
    assert {(r["track"], r["target"]) for r in rows} == {("ai", "tp1"), ("ai", "tp2"), ("final", "tp1"), ("final", "tp2")}
    assert all(r["ctrl_n"] == 20 and r["final"] == 1 for r in rows)
    assert all(r["status"] in ("done", "not_bought", "void") for r in rows)
    assert any(r["ret"] is not None for r in rows if r["status"] == "done")


def test_controls_are_reproducible_and_scaled_to_each_stock(paths):
    a = records.get(paths, analysis(paths))
    c = control(paths, a, "final", a["final_plan"], "tp1")
    assert c == control(paths, a, "final", a["final_plan"], "tp1") and c["n"] == 20 and 0 <= c["bought"] <= 20
    assert c["final"] is True and control(paths, a, "final", plan(order_type="不下单"), "tp1") is None
    assert control(paths, a, "final", plan(order_direction="做空"), "tp1") is None


def test_the_pool_leaves_out_st_young_beijing_and_the_stock_itself(paths):
    d0 = records.calendar(paths)[-1]
    got = pool(paths, d0, exclude=LID)
    assert LID not in got and not {"CN:600100", "CN:830001", "CN:600102"} & set(got) and len(got) == 27
    assert got == sorted(got)


def test_forward_excess_after_20_days_and_settled_flag(paths):
    aid = analysis(paths)
    young = analysis(paths, back=10)                                              # 20 days have not passed yet
    settle_all(lambda m: paths)
    row, other = records.get(paths, aid), records.get(paths, young)
    assert row["x20"] is not None and row["x5"] is not None and row["fwd_outcome"] == "settled" and row["settled"] == 1
    assert other["x20"] is None and other["settled"] == 0 and other["start_date"] is not None


def test_evaluation_counts_first_of_day_only_and_says_when_too_few(paths):
    analysis(paths)
    analysis(paths)                                                               # a rerun on the same day 0
    settle_all(lambda m: paths)
    ev = evaluation(paths)
    p = ev["plans"]["final"]["tp1"]
    assert p["n"] == 1 and p["orders"] == 1 and ev["enough"] is False and ev["plans"]["final_control"]["tp1"]["n"] == 1
    assert {g["key"] for g in ev["groups"]["direction"]} == {"bullish"} and ev["groups"]["buy"][0]["key"] == "买"
    assert ev["intraday"]["no"]["final"]["tp1"]["n"] == 1 and ev["intraday"]["yes"]["final"]["tp1"]["n"] == 0


def test_demo_test_profile_and_failed_rows_are_never_settled(paths, tmp_path):
    demo = analysis(paths, demo=True)
    failed = analysis(paths, status="failed")
    test = analysis(paths, profile="test", adjusted=False)
    settle_all(lambda m: paths)
    for aid in (demo, failed, test):
        assert records.get(paths, aid)["settled"] == 0 and trade_rows(paths, aid) == []


def flag_day0(paths, lids: list[str], d0) -> None:
    """Those stocks' day-0 bars made impossible (low above high): the quality check makes their prices unknown."""
    st = Store(paths, "CN")
    b = st.read("bars_1d").filter(pl.col("listing_id").is_in(lids) & (pl.col("date") == d0))
    st.publish({"bars_1d": b.with_columns((pl.col("high") + 1).alias("low"))}, as_of=st.latest().as_of)


def test_a_drawn_stock_without_a_clean_day_0_bar_is_passed_over(paths):        # final review, Important 2
    a = records.get(paths, analysis(paths))
    flag_day0(paths, [f"CN:{600103 + i}" for i in range(10)], date.fromisoformat(a["day"]))   # 10 of the pool's 27
    c = control(paths, a, "final", a["final_plan"], "tp1")
    assert c["n"] == 17 and c["final"] is True                                    # every clean stock, none flagged
    assert settle_all(lambda m: paths) == 1


def test_one_analysis_that_cannot_be_settled_does_not_stop_the_rest(paths, monkeypatch):
    bad, good = analysis(paths), analysis(paths, back=40)
    real = settle.control

    def control_(p, a, *args, **kw):
        if a["id"] == bad:
            raise RuntimeError("a data slip")
        return real(p, a, *args, **kw)

    monkeypatch.setattr(settle, "control", control_)
    assert settle_all(lambda m: paths) == 1
    assert records.get(paths, good)["settled"] == 1 and records.get(paths, bad)["settled"] == 0


def test_the_evaluation_reads_the_controls_per_analysis_like_the_plans(paths):  # final review, Important 3
    analysis(paths)
    analysis(paths, back=40)
    settle_all(lambda m: paths)
    rows = [r for r in (trade_rows(paths, a["id"]) for a in records.recent(paths)) for r in r
            if (r["track"], r["target"]) == ("final", "tp1")]
    ev = evaluation(paths)
    c, p = ev["plans"]["final_control"]["tp1"], ev["plans"]["final"]["tp1"]
    assert c["bought"] is None and c["bought_share"] == pytest.approx(sum(r["ctrl_bought"] for r in rows) / sum(r["ctrl_n"] for r in rows))
    assert c["ret_median"] == pytest.approx(statistics.median([r["ctrl_ret_mean"] for r in rows]))   # one number an analysis
    assert p["bought_share"] == pytest.approx(p["bought"] / p["orders"])


def test_the_programs_own_direction_is_a_group_of_its_own(paths):               # final review, Important 5
    analysis(paths)
    settle_all(lambda m: paths)
    g = evaluation(paths)["groups"]
    assert [x["key"] for x in g["program"]] == ["neutral"] and [x["key"] for x in g["direction"]] == ["bullish"]


def test_settling_runs_once_per_snapshot(paths, monkeypatch):                    # final review (Minor 3, promoted)
    analysis(paths, back=10)                                                      # stays unsettled: its 20 days are ahead
    calls, real = [], settle.bars_for
    monkeypatch.setattr(settle, "bars_for", lambda *a, **k: calls.append(1) or real(*a, **k))
    settle_all(lambda m: paths)
    first = len(calls)
    settle_all(lambda m: paths)
    assert first > 0 and len(calls) == first                                     # the same snapshot: nothing new to learn
    st = Store(paths, "CN")
    nxt = st.latest().as_of + timedelta(days=1)
    st.publish({"calendar": conform(pl.DataFrame({"date": [nxt], "is_open": True, "source": "t"}), "calendar")}, as_of=nxt)
    settle_all(lambda m: paths)
    assert len(calls) > first                                                     # a new snapshot: settled again


def test_the_ais_short_plans_are_not_orders_it_could_buy(paths):   # the comparison with upstream, 10-21
    # a 做空 plan is never bought on A shares ("short"): counted as an order, it pulled the AI's 买到的比例 down
    analysis(paths)
    short = analysis(paths, back=40)
    db = connect(paths.shared_db, "shared")
    ai = json.loads(db.execute("SELECT ai_plan FROM pa_analysis WHERE id=?", (short,)).fetchone()[0])
    db.execute("UPDATE pa_analysis SET ai_plan=? WHERE id=?", (json.dumps({**ai, "order_direction": "做空"}), short))
    settle_all(lambda m: paths)
    p = evaluation(paths)["plans"]["ai"]["tp1"]
    assert p["orders"] == 1 and p["bought_share"] == pytest.approx(p["bought"])
