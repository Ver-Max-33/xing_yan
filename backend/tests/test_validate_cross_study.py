"""越线买 in the event study, the check-up and the control (design 2026-10-11 §6, §7, §10), on a small synthetic A-share
store whose stocks swing in bands and move out of them (boxes and breaks found by the BOX primitive itself): a stock in
the 潜伏 list at a close is bought the next day where its high first reaches the line, at the line or the open; its
control at its stock's average price of the day; the check-up judges each variant by its crosses."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.data.store import Store
from wb.datasets import conform
from wb.engine.l2.run import structures
from wb.engine.l3.rule import Plan, load_rule
from wb.validate.engine import Variant
from wb.validate.study import _signals, checkup, event_study, plan_study

L, N = 12, 420
RULE = """
id: xb
name: 箱顶越线
markets: [CN]
stages:
  - {id: box, kind: structure, use: BOX, with: {scale: mid}}
  - {id: breakout, kind: event, on: box.break_up}
signal_at: breakout.at
watch_stage: box
plan:
  entry: cross
  stop: [{line: "box.bottom * 0.985", label: 箱底}]
  days: 10
"""


def days(n, end=date(2026, 9, 30)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    from wb.config import get_paths
    paths = get_paths("test", tmp_path_factory.mktemp("cross"))
    rng = np.random.default_rng(11)
    ds = days(N)
    rows = []
    for i in range(L):
        level, c = 10.0, []
        while len(c) < N:   # a stretch swinging in a band, then a move out of it
            x = rng.uniform(-1, 1)
            for _ in range(int(rng.integers(35, 70))):
                x = 0.85 * x + rng.normal(0, 0.45)
                c.append(level * (1 + 0.04 * np.tanh(x)))
            level *= rng.choice([1.12, 0.92, 1.08])
        c = np.array(c[:N])
        prev = np.concatenate([c[:1], c[:-1]])
        o = prev * (1 + rng.normal(0, 0.004, N))
        h, lo = np.maximum(o, c) * (1 + rng.uniform(0.002, 0.02, N)), np.minimum(o, c) * (1 - rng.uniform(0.002, 0.02, N))
        rows.append(pl.DataFrame({"listing_id": f"CN:{600000 + i}", "date": ds, "open": o, "high": h, "low": lo, "close": c,
                                  "volume": rng.uniform(0.8, 1.2, N) * 1e6, "adj_factor": 1.0, "up_limit": (prev * 1.1).round(2),
                                  "down_limit": (prev * 0.9).round(2), "suspended": False, "source": "t"}))
    bars = pl.concat(rows).with_columns(pl.col("date").cast(pl.Date))
    idx = bars.group_by("date").agg(pl.col("close").mean() * 300).sort("date")["close"].to_numpy()
    lids = [f"CN:{600000 + i}" for i in range(L)]
    Store(paths, "CN").publish({
        "bars_1d": conform(bars, "bars_1d"),
        "instruments": conform(pl.DataFrame({"listing_id": lids, "market": "CN", "code": [x[3:] for x in lids], "name": lids,
                                             "industry": "电子", "status": "L", "source": "t"}), "instruments"),
        "calendar": conform(pl.DataFrame({"date": ds, "is_open": True, "source": "t"}), "calendar"),
        "index_bars_1d": conform(pl.DataFrame({"index_id": "CN:IDX:000300", "date": ds, "open": np.concatenate([idx[:1], idx[:-1]]),
                                               "high": idx * 1.005, "low": idx * 0.995, "close": idx, "source": "t"}), "index_bars_1d"),
    }, as_of=ds[-1], note="t")
    return paths, ds, bars


def test_a_cross_is_bought_where_the_high_first_reaches_the_line_the_list_waited_on(market):
    paths, ds, bars = market
    rule = load_rule(RULE)
    sig = _signals(lambda m: paths, [Variant(rule)], ["CN"], ds[60], ds[-1], None, plan=True)[0]
    assert sig.height > 30
    tops = structures(paths, "CN", ["BOX"], rule.primitive_params())["BOX"]["boxes"].select(pl.col("box_id").alias("box"), "top")
    x = sig.join(tops, on="box").join(bars.select("listing_id", pl.col("date").alias("signal_date"), "open", "high"),
                                      on=["listing_id", "signal_date"])
    assert x.height == sig.height
    for r in x.iter_rows(named=True):            # the top 盯盘 alerts at, reached that day, at the top or the open
        assert r["edge"] == pytest.approx(r["top"]) and r["line"] == pytest.approx(r["top"] * 1.015)
        assert r["high"] >= r["edge"] * (1 - 1e-9) and r["entry_price"] == pytest.approx(max(r["open"], r["edge"]))
    assert sig.select("listing_id", "box").is_duplicated().sum() == 0   # a line is crossed once
    trades, _ = plan_study(lambda m: paths, rule, sig, ["CN"], control=False)
    bought = trades.filter(pl.col("entry_date").is_not_null()).join(sig.select("listing_id", "signal_date", pl.col("entry_price").alias("p")),
                                                                    on=["listing_id", "signal_date"])
    assert bought.height > 20 and (bought["entry_date"] == bought["signal_date"]).all()
    assert (bought["entry_price"] - bought["p"]).abs().max() < 1e-9


def test_the_control_of_a_cross_buys_at_its_stock_s_average_price_of_the_day(market):
    paths, ds, bars = market
    rule = load_rule(RULE)
    sig = _signals(lambda m: paths, [Variant(rule)], ["CN"], ds[60], ds[-1], None, plan=True)[0]
    _, ctrl = plan_study(lambda m: paths, rule, sig, ["CN"], seed=1)
    c = (ctrl.filter(pl.col("entry_date").is_not_null())
         .join(bars.select("listing_id", pl.col("date").alias("signal_date"),
                           ((pl.col("open") + pl.col("high") + pl.col("low") + pl.col("close")) / 4).alias("avg")),
               on=["listing_id", "signal_date"]))
    assert c.height > 20 and (c["entry_date"] == c["signal_date"]).all() and (c["entry_price"] - c["avg"]).abs().max() < 1e-9


def test_the_event_study_of_a_cross_plan_says_how_the_crosses_went(market):
    # design 2026-10-11 §10: crosses a day, at the open, closed over the line, and those the rule's signal that close
    # against the 假突破
    paths, ds, _ = market
    p = event_study(lambda m: paths, load_rule(RULE), ["CN"], ds[60], ds[-1], ds[-1] - timedelta(days=200))["summary"]["plan"]
    c = p["cross"]
    assert p["plan"]["entry"] == "cross" and c["crosses"] == p["signals"] and c["per_day"] >= 1
    assert 0 < c["at_open"] < 1 and 0 < c["closed_above"] < 1 and 0 < c["signal"] < 1
    assert {g["key"] for g in c["kinds"]} <= {"收盘成了规则信号", "收盘站上突破线、没成信号", "收盘没站上（假突破）"}
    assert "收盘没站上（假突破）" in {g["key"] for g in c["kinds"]}
    assert sum(g["days"] for g in c["kinds"]) >= p["days"]


def test_a_check_up_of_a_cross_plan_judges_each_variant_by_its_crosses(market):
    paths, ds, _ = market
    rule = load_rule(RULE.replace("  - {id: box, kind: structure, use: BOX, with: {scale: mid}}",
                                  "  - id: box\n    kind: structure\n    use: BOX\n    with: {scale: mid}\n"
                                  "    at: [{id: narrow, expr: \"box.width <= $w\"}]")
                     .replace("stages:", "params:\n  - {id: w, value: 0.08, range: [0.04, 0.2], step: 0.04}\nstages:"))
    base = _signals(lambda m: paths, [Variant(rule)], ["CN"], ds[60], ds[-1], None, plan=True)[0]
    assert base.height > 20 and base["entry_price"].is_not_null().all()   # its signals are crosses
    r = checkup(lambda m: paths, rule, ["CN"], ds[60], ds[-1], ds[-1] - timedelta(days=200))
    assert r["plan"]["entry"] == "cross" and r["base"]["signals"] == base.height
    narrow = next(a for a in r["ablation"] if a["condition"] == "box.narrow")
    assert narrow["signals"] > r["base"]["signals"]                     # without it more boxes wait, more lines are crossed



def test_the_event_study_compares_buying_at_the_line_at_the_close_and_at_the_next_open(market):
    # design 2026-10-11 §10 买法对比: the same rule and the same sales, bought three ways; how many were compared is said
    paths, ds, _ = market
    oos = ds[-1] - timedelta(days=200)
    p = event_study(lambda m: paths, load_rule(RULE), ["CN"], ds[60], ds[-1], oos)["summary"]["plan"]
    e = p["entries"]
    assert e["compared"] == 3 and [r["entry"] for r in e["rows"]] == ["cross", "close", "open"]
    own = e["rows"][0]
    assert own["expectancy"] == p["expectancy"] and own["days"] == p["days"]          # the rule's own way is the report's
    close, opn = e["rows"][1], e["rows"][2]
    assert close["signals"] == opn["signals"] > 0 and close["days"] > 0 and opn["days"] > 0   # the same signals, bought two ways
    assert close["expectancy"] != opn["expectancy"] and all(Plan(**r["plan"]).fills == "close" for r in e["rows"])   # same sales
    assert {"ci", "vs_control", "vs_control_ci", "win", "held"} <= set(close)
    # a rule with no line to cross compares the close and the next open
    plain = load_rule(RULE.replace("watch_stage: box\n", "").replace("entry: cross", "entry: close"))
    q = event_study(lambda m: paths, plain, ["CN"], ds[60], ds[-1], oos)["summary"]["plan"]["entries"]
    assert q["compared"] == 2 and [r["entry"] for r in q["rows"]] == ["close", "open"]


def test_variants_that_wait_on_the_same_list_replay_it_once(market, monkeypatch):
    # a check-up's variants that change only the breakout bar (its volume: a cross does not read it) wait on the same
    # 潜伏 list: it is replayed once a chunk, each variant removing the days its own rule signals
    from wb.engine.l3.match import Matcher
    from wb.validate.engine import market_signals
    from wb.validate.plantrades import plan_rule

    paths, ds, _ = market
    rule = load_rule(RULE.replace("  - {id: breakout, kind: event, on: box.break_up}",
                                  "  - {id: breakout, kind: event, on: box.break_up, at: [{id: vol, expr: \"VOL_RATIO(5) >= $v\"}]}")
                     .replace("stages:", "params:\n  - {id: v, value: 1.5, range: [0.5, 3.0], step: 0.5}\nstages:"))
    calls = []
    real = Matcher.watches

    def counted(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(Matcher, "watches", counted)
    vs = [Variant(plan_rule(rule)), Variant(plan_rule(rule.with_params({"v": 2.5}))), Variant(plan_rule(rule), frozenset({("breakout", "vol")}))]
    got = market_signals(paths, "CN", vs, ds[60], ds[-1], lines=True, cross=rule.trade_plan)
    assert len(calls) == 1 and got[0].height > 20
    alone = [market_signals(paths, "CN", [v], ds[60], ds[-1], lines=True, cross=rule.trade_plan)[0] for v in vs]
    for a, b in zip(got, alone, strict=True):
        assert a.sort("listing_id", "signal_date").equals(b.sort("listing_id", "signal_date"))


def test_a_cross_plan_without_lines_is_still_bought_where_the_line_was_crossed(market):
    # no 失效线, no 目标位: the crosses keep their own price — never bought at the day's average as a control is (final
    # review I2: 27 of 133 were bought under the day's open)
    paths, ds, bars = market
    rule = load_rule(RULE.replace('  stop: [{line: "box.bottom * 0.985", label: 箱底}]\n', ""))
    sig = _signals(lambda m: paths, [Variant(rule)], ["CN"], ds[60], ds[-1], None, plan=True)[0]
    assert sig.height > 20 and sig["entry_price"].is_not_null().all()
    trades, _ = plan_study(lambda m: paths, rule, sig, ["CN"], control=False)
    t = (trades.filter(pl.col("entry_date").is_not_null())
         .join(sig.select("listing_id", "signal_date", pl.col("entry_price").alias("p")), on=["listing_id", "signal_date"])
         .join(bars.select("listing_id", pl.col("date").alias("signal_date"), "open"), on=["listing_id", "signal_date"]))
    assert t.height > 20 and (t["entry_price"] - t["p"]).abs().max() < 1e-9 and (t["entry_price"] >= t["open"] - 1e-9).all()