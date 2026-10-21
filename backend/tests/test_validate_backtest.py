"""组合回测 over a market (spec §9.2): each signal comes with its box line (its BOX stage's bottom × (1 − tol), in the
adjusted prices the boxes are in) and the rule's ranking; the book is bought only after the signal day at that day's
open, every cash move is in the ledger, and the report gives the NAV against the benchmark, the drawdown, the trades
and what could not be done, with 样本内 / 样本外."""
from datetime import date

import polars as pl
import pytest

from tests.test_validate_engine import ALL, boxes_market
from wb.data.store import Store
from wb.engine.l2.run import structures
from wb.validate.backtest import _own_lines, _own_settings, _owned, backtest, book_settings
from wb.validate.engine import Variant, market_signals
from wb.workspace.rules import builtin_rules


def test_a_signal_carries_its_box_line_and_the_rules_ranking(paths):
    boxes_market(paths)
    rule = builtin_rules()["tpl_shang_pin"][0]
    sig = market_signals(paths, "CN", [Variant(rule)], *ALL, lines=True)[0]
    stage = next(s.id for s in rule.stages if s.use == "BOX")
    boxes = structures(paths, "CN", ["BOX"], rule.primitive_params())["BOX"]["boxes"]
    tol = rule.primitive_params().get("BOX", {}).get("tol", 0.015)
    got = sig.join(boxes.select(pl.col("box_id").alias(stage), "bottom"), on=stage)
    assert got.height == sig.height > 0 and (got["stop"] - got["bottom"] * (1 - tol)).abs().max() < 1e-9
    plain = market_signals(paths, "CN", [Variant(rule)], *ALL)[0]
    assert plain.columns == ["listing_id", "signal_date"] and plain.height == sig.height          # the event study's as before
    ranked = market_signals(paths, "CN", [Variant(builtin_rules()["tpl_shang_heng_shang"][0])], *ALL, lines=True)[0]
    assert ranked.height and ranked["rank0"].null_count() == 0                                     # box.width on the signal day


def test_a_book_bought_after_its_signals_whose_ledger_balances(paths):
    boxes_market(paths)
    rule = builtin_rules()["tpl_shang_pin"][0]
    s = book_settings("CN", {"max_positions": 5, "hold": 20})
    out = backtest(lambda m: paths, rule, ["CN"], date(2023, 6, 1), date(2026, 9, 30), date(2025, 9, 30), {"CN": s})
    book, trades, ledger, nav = out["books"]["CN"], out["trades"], out["ledger"], out["nav"]
    assert trades.height > 0 and s.t_plus_1 and s.lot == 100
    # never at the signal day's close: bought at the open of the market's next trading day
    st = Store(paths, "CN")
    days = st.read("calendar").filter(pl.col("is_open"))["date"].sort().to_list()
    nxt = {d: days[k + 1] for k, d in enumerate(days[:-1])}
    assert all(e == nxt[sd] for sd, e in zip(trades["signal_date"], trades["entry_date"], strict=True))
    opens = st.read("bars_1d").select("listing_id", pl.col("date").alias("entry_date"), "open")
    assert trades.join(opens, on=["listing_id", "entry_date"]).select((pl.col("entry_price") - pl.col("open")).abs().max()).item() < 1e-9
    # every cash move is in the ledger; the NAV is cash and holdings on every trading day of the range
    assert s.cash + ledger["amount"].sum() == pytest.approx(nav["cash"][-1])
    assert nav["date"].to_list() == [d for d in days if date(2023, 6, 1) <= d <= date(2026, 9, 30)]
    assert book["stats"]["total"] == pytest.approx(nav["nav"][-1] / s.cash - 1)
    assert book["stats"]["max_drawdown"] <= 0 and book["benchmark"]["total"] is not None
    assert book["split"]["oos_start"] == "2025-09-30" and book["split"]["in"]["total"] is not None
    assert len(book["curve"]) == nav.height and book["curve"][0][1] == pytest.approx(nav["nav"][0] / s.cash)
    assert {"blocked_buy", "blocked_sell", "skipped", "same_bar", "gap"} <= set(book["counts"])
    assert book["settings"]["hold"] == 20 and book["settings"]["costs"]["stamp"] == 0.0005


def test_a_rule_without_a_box_has_no_box_stop_and_says_so(paths):
    boxes_market(paths)
    rule = builtin_rules()["tpl_ma_bunch"][0]
    out = backtest(lambda m: paths, rule, ["CN"], date(2023, 6, 1), date(2026, 9, 30), date(2025, 9, 30),
                   {"CN": book_settings("CN", {"box_stop": True, "hold": 10, "own": False})})
    assert out["books"]["CN"]["notes"] == ["规则里没有箱体阶段：跌破箱体下沿止损不适用"]
    assert "box" not in out["trades"]["reason"].drop_nulls().to_list()


# ---- 稳健性检验 (spec §9.3): the §6.5 grid as a heatmap of books, and the costs raised ----------------------------------
from tests.test_validate_study import market  # noqa: E402,F401 - the planted-effect market (a module fixture)
from wb.engine.l3.rule import load_rule  # noqa: E402
from wb.validate.backtest import HEAT, robustness  # noqa: E402

TUNED = """
id: spike2
name: 放量站上均线（两个参数）
markets: [CN]
params:
  - {id: k, value: 3.0, range: [1.0, 5.0], step: 0.5, tune: true}
  - {id: n, value: 20, range: [10, 30], step: 5, tune: true}
stages:
  - id: today
    kind: event
    when: "C > MA(C, $n)"
    at:
      - {id: vol, expr: "V >= MA(V, 20) * $k"}
signal_at: today.at
"""


def test_the_grid_as_a_heatmap_whose_current_cell_is_the_rules_own_book(market):
    paths, ds = market
    rule = load_rule(TUNED)
    s = book_settings("CN", {"max_positions": 5, "hold": 20})
    start, end, oos = ds[60], ds[-1], ds[500]
    r = robustness(lambda m: paths, rule, ["CN"], start, end, oos, {"CN": s})
    assert [a["param"] for a in r["axes"]] == ["k", "n"] and r["axes"][0]["current"] == 3.0
    assert all(len(a["values"]) <= HEAT and a["current"] in a["values"] for a in r["axes"])
    assert len(r["cells"]) == len(r["axes"][0]["values"]) * len(r["axes"][1]["values"]) == r["compared"]
    here = next(c for c in r["cells"] if c["at"] == [3.0, 20])
    own = backtest(lambda m: paths, rule, ["CN"], start, end, oos, {"CN": s})["books"]["CN"]
    assert here["CN"]["total"] == pytest.approx(own["stats"]["total"]) and here["CN"]["out_total"] == pytest.approx(own["split"]["out"]["total"])
    # the costs raised on the rule's own signals: never a better book
    costs = r["costs"]
    assert [c["k"] for c in costs] == [1, 2, 3] and costs[0]["CN"]["total"] == pytest.approx(own["stats"]["total"])
    assert costs[0]["CN"]["total"] > costs[1]["CN"]["total"] > costs[2]["CN"]["total"]


def test_a_rule_without_tuned_parameters_only_has_its_costs_raised(market):
    paths, ds = market
    rule = load_rule(TUNED.replace(", tune: true", ""))
    r = robustness(lambda m: paths, rule, ["CN"], ds[60], ds[-1], ds[500], {"CN": book_settings("CN", {"hold": 20})})
    assert r["axes"] == [] and r["cells"] == [] and len(r["costs"]) == 3
    assert r["notes"] == ["规则没有标 tune: true 的参数：只做成本上调压力测试", "规则里没有箱体阶段：跌破箱体下沿止损不适用"]


def test_the_report_says_what_a_market_cannot_book():
    # JP: only whole-day UL / LL flags (an approximation the spec says the report states), and factors that carry
    # splits only — a JP book has no dividends, which "分红 0" alone would hide
    from wb.validate.backtest import _notes
    sig = pl.DataFrame({"listing_id": ["JP:7203"], "signal_date": [date(2026, 3, 2)], "stop": [1.0]})
    jp = _notes("JP", sig, book_settings("JP", {}))
    assert any("UL / LL" in n for n in jp) and any("分红没有记" in n for n in jp)
    assert _notes("CN", sig, book_settings("CN", {})) == []


def test_cn_bars_carry_the_dividend_records_the_cash_comes_from(paths):
    # spec §9.2: an A-share holder is paid what the dividend record says (cash a share, before tax), not what the factor
    # implies (a rights issue moves the factor too)
    from wb.datasets import conform
    from wb.validate.backtest import _bars
    days = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]
    st = Store(paths, "CN")
    st.publish({"bars_1d": conform(pl.DataFrame({"listing_id": "CN:600000", "date": days, "open": 10.0, "high": 10.5, "low": 9.5,
                                                 "close": 10.0, "volume": 1e6, "adj_factor": 1.0, "suspended": False}), "bars_1d"),
                "corp_actions": conform(pl.DataFrame({"listing_id": ["CN:600000"], "ex_date": [days[1]], "kind": ["dividend"],
                                                      "ratio": [1.0], "cash_div": [0.5], "source": ["t"]}), "corp_actions")}, as_of=days[-1])
    assert _bars(st, st.latest(), ["CN:600000"], days[0]).sort("date")["cash_div"].to_list() == [None, 0.5, None]


def test_a_ranking_that_is_true_or_false_joins_up_across_batches(paths, monkeypatch):
    # a batch with no binding types its ranking columns as numbers, one with bindings had the expression's own type (a
    # comparison: true / false) — the batches must still join up, as numbers
    from wb.engine.l3.rule import Rank
    from wb.validate import engine
    boxes_market(paths)
    rule = builtin_rules()["tpl_shang_heng_shang"][0]
    rule = rule.model_copy(update={"ranking": [Rank(expr="C > MA(C, 5)", dir="desc")]})
    monkeypatch.setattr(engine, "CHUNK", 1)
    sig = market_signals(paths, "CN", [Variant(rule)], *ALL, lines=True)[0]
    assert sig.height and sig.schema["rank0"] == pl.Float64


def test_a_weak_market_is_the_day_after_the_benchmark_closed_below_its_200_day_line():
    from datetime import timedelta

    from wb.validate.backtest import _weak
    ds = [date(2025, 1, 1) + timedelta(days=k) for k in range(260)]
    weak = _weak(pl.DataFrame({"date": ds, "close": [100.0] * 250 + [90.0] * 10}), ds[240:])
    assert min(weak) == ds[251] and ds[250] not in weak and len(weak) == 9               # known at day 250's close
    assert _weak(pl.DataFrame({"date": ds[:100], "close": [90.0] * 100}), ds[:100]) == set()   # no 200 days yet: not weak


def test_a_rate_set_for_a_run_applies_to_every_day_and_the_others_keep_their_history():
    s = book_settings("CN", {"costs": {"stamp": 0.0008}})
    assert s.costs.on(date(2020, 1, 2)).stamp == 0.0008 and s.costs.on(date(2024, 1, 2)).stamp == 0.0008
    assert (s.costs.on(date(2020, 1, 2)).transfer, s.costs.on(date(2024, 1, 2)).transfer) == (0.00002, 0.00001)


def test_a_book_can_end_by_the_rule_s_own_lines(paths):
    # 规则自己的判断 in the book (the default): 上品's 失效线 (its 金品's small box bottom, no target) in place of the box
    # line, its days the holding period; the report keeps the settings as asked and says what the judgement used
    boxes_market(paths)
    rule = builtin_rules()["tpl_shang_pin"][0]

    def run(own):
        return backtest(lambda m: paths, rule, ["CN"], date(2023, 6, 1), date(2026, 9, 30), date(2025, 9, 30),
                        {"CN": book_settings("CN", {"max_positions": 5, "own": own})})
    mine, book = run(True), run(False)
    held = [out["trades"].filter(pl.col("reason") == "hold")["days"].max() for out in (mine, book)]
    assert held == [rule.trade_plan.days, 20]
    assert set(mine["trades"]["reason"].drop_nulls()) <= {"fail", "hold", "target"}
    cn = mine["books"]["CN"]
    assert cn["settings"]["own"] is True and cn["settings"]["hold"] == 20 and book_settings("CN").own is True
    assert cn["notes"][0] == ("按规则自己的交易计划离场：收盘跌破失效线（g.small_bottom）次日开盘卖，最多持有 60 个交易日；"
                              "组合回测还没有越线买（按信号第二天开盘买）、卖出条件（C < MA(C, 5)）：账户回测改版时加上")


def test_the_rule_s_own_lines_are_ranked_after_its_ranking():
    # `own`: the judgement's lines are read as rankings after the rule's own; the 失效线 is the stop (none without one),
    # the 目标位 target_px; a rule without a judgement (a candidate list) keeps the book's exits
    s = book_settings("CN", {"own": True})
    rules = builtin_rules()
    calm, back, plain = rules["tpl_calm_breakout"][0], rules["tpl_xia_po_hui_xiang"][0], rules["vol_burst"][0]
    assert [r.expr for r in _owned(calm, s).ranking] == [calm.ranking[0].expr, "box.false_bottom", "box.bottom * 0.985"]
    sig = pl.DataFrame({"listing_id": ["a"], "rank0": [0.02], "rank1": [9.5], "rank2": [9.2], "stop": [9.7]})
    assert _own_lines(sig, calm, s).select("stop", "target_px").row(0) == (9.5, None)   # the highest of its lines stops
    sig = pl.DataFrame({"listing_id": ["a"], "rank0": [0.02], "rank1": [None], "rank2": [9.2], "stop": [9.7]})
    assert _own_lines(sig, calm, s).select("stop", "target_px").row(0) == (9.2, None)   # no 假底: the box's bottom
    sig = pl.DataFrame({"listing_id": ["a"], "rank0": [9.0], "rank1": [12.0], "stop": [9.7]})
    assert _own_lines(sig, back, s).select("stop", "target_px").row(0) == (9.0, 12.0)
    assert _own_settings(back, s).hold == back.trade_plan.days and _own_settings(plain, s).own is False
    assert _own_lines(sig, plain, _own_settings(plain, s)).equals(sig) and _owned(plain, _own_settings(plain, s)) is plain


def test_the_book_is_given_each_signal_s_own_target():
    # the 目标位 a signal carries reaches the simulation with its 失效线 (once dropped on the way: no target ever ended a
    # position in a run, while the event study saw a third of 下破回箱 end at the box top)
    from tests.test_portfolio import DAYS, NO_COST, bars
    from wb.validate.backtest import _run
    from wb.validate.portfolio import Settings, prepare
    rise = bars("CN:600000", [(k, 10.0 + 0.2 * k, 10.1 + 0.2 * k, 9.9 + 0.2 * k, 10.0 + 0.2 * k) for k in range(30)], limits=False)
    sig = pl.DataFrame([("CN:600000", DAYS[1], 9.0, 12.0, 0)], orient="row",
                       schema={"listing_id": pl.Utf8, "signal_date": pl.Date, "stop": pl.Float64, "target_px": pl.Float64, "rank": pl.Int64})
    s = Settings(cash=100_000.0, costs=NO_COST, max_positions=1, hold=None, box_stop=False, own=True)
    t = _run(sig, prepare(rise), DAYS, s)["trades"]
    assert t["reason"].to_list() == ["target"] and t["exit_price"].to_list() == [pytest.approx(12.0)]


def test_a_close_entry_bought_at_the_next_open_is_said():
    """组合回测 buys every plan at the next open (§9.2); a 尾盘买 plan's note said nothing of it (review 10-13)."""
    from wb.engine.l3.rule import load_rule
    from wb.validate.backtest import _own_note, book_settings
    rule = load_rule('id: r\nname: r\nmarkets: [CN]\nstages: [{id: x, kind: event, when: "C > 0"}]\nsignal_at: x.at\n'
                     "plan: {entry: close, stop: [{line: 'L'}], days: 20}\n")
    assert "尾盘买（按信号第二天开盘买）" in _own_note(rule, book_settings("CN"))[0]
