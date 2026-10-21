"""验证 on a market with a planted effect (spec §9.1, §9.5, §14 条件体检测试): after a volume spike the price drifts up
for 20 days, otherwise it is a random walk; the benchmark is the market's own average. Dropping the spike condition makes both 样本内 and 样本外 worse — 有效;
dropping `RAW_C > 0` changes nothing — 多余; the spike alone beats random entries; the threshold scan reports each point
with both samples and where the current value sits."""
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from wb.data.store import Store
from wb.datasets import conform
from wb.engine.l3.rule import load_rule
from wb.validate.study import checkup, event_study, scan_points, single

L, D, SPIKE, DRIFT = 60, 760, 0.03, 0.008
RULE = """
id: spike
name: 放量站上均线
markets: [CN]
params:
  - {id: k, value: 3.0, range: [1.0, 5.0], step: 0.5}
stages:
  - id: today
    kind: event
    when: "C > MA(C, 20)"
    at:
      - {id: vol, expr: "V >= MA(V, 20) * $k"}
      - {id: pos, expr: "RAW_C > 0"}
signal_at: today.at
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
    paths = get_paths("test", tmp_path_factory.mktemp("validate"))
    rng = np.random.default_rng(7)
    ds = days(D)
    spike = rng.random((L, D)) < SPIKE
    drift = np.zeros((L, D))
    for i, t in zip(*np.nonzero(spike), strict=True):
        drift[i, t + 1:t + 21] += DRIFT                       # the planted effect: up for 20 days after a spike
    ret = rng.normal(0.0, 0.015, (L, D)) + drift
    close = 10 * np.exp(np.cumsum(ret, axis=1))
    opn = np.concatenate([close[:, :1], close[:, :-1]], axis=1)
    vol = rng.uniform(0.8, 1.2, (L, D)) * 1e6 * np.where(spike, 4.0, 1.0)
    idx = 3000 * (close / close[:, :1]).mean(axis=0)              # the benchmark: the market's own average
    idx_open = np.concatenate([idx[:1], idx[:-1]])
    lids = [f"CN:{600000 + i}" for i in range(L)]
    bars = conform(pl.DataFrame({
        "listing_id": np.repeat(lids, D), "date": ds * L, "open": opn.ravel(), "high": (np.maximum(opn, close) * 1.01).ravel(),
        "low": (np.minimum(opn, close) * 0.99).ravel(), "close": close.ravel(), "volume": vol.ravel(), "adj_factor": 1.0,
        "up_limit": (opn * 1.1).ravel(), "suspended": False, "source": "t"}).with_columns(pl.col("date").cast(pl.Date)), "bars_1d")
    Store(paths, "CN").publish({
        "bars_1d": bars,
        "instruments": conform(pl.DataFrame({"listing_id": lids, "market": "CN", "code": [x[3:] for x in lids], "name": lids,
                                             "industry": "电子", "status": "L", "source": "t"}), "instruments"),
        "calendar": conform(pl.DataFrame({"date": ds, "is_open": True, "source": "t"}), "calendar"),
        "index_bars_1d": conform(pl.DataFrame({"index_id": "CN:IDX:000300", "date": ds, "open": idx_open, "high": idx,
                                               "low": idx_open, "close": idx, "source": "t"}), "index_bars_1d"),
    }, as_of=ds[-1], note="t")
    return paths, ds


def test_the_event_study_and_its_split(market):
    paths, ds = market
    oos = ds[-1] - timedelta(days=365)
    r = event_study(lambda m: paths, load_rule(RULE), ["CN"], ds[60], ds[-1], oos)
    s = r["summary"]
    assert s["effective"] >= 100 and s["win"] > 0.8 and s["median"] > 0.1            # the drift, not the noise
    assert s["split"]["in"]["effective"] >= 30 and s["split"]["out"]["effective"] >= 30
    assert r["rows"].height == s["signals"]
    assert s["worst"] and all(w["name"] == w["listing_id"] for w in s["worst"])        # named (this market's names are ids)


def test_the_event_study_trades_each_signal_by_its_plan(market):
    # 交易计划 (design 2026-10-09 §6): bought on the signal day's close, out at a close under -10% or a high reaching +10%,
    # else the 30th day — the drift after a spike reaches the target more often than the line, and beats stocks drawn at
    # random the same days traded the same way
    paths, ds = market
    oos = ds[-1] - timedelta(days=365)
    rule = load_rule(RULE + 'plan: {entry: close, stop: [{line: "C * 0.9", label: 跌一成}], take: [{line: "C * 1.1", label: 涨一成}], days: 30}\n')
    r = event_study(lambda m: paths, rule, ["CN"], ds[60], ds[-1], oos)
    s = r["summary"]
    p = s["plan"]
    assert s["metric"] == "plan" and p["expectancy"] > 0.02 and p["ci"][0] > 0
    assert p["vs_control"] > 0.02 and p["vs_control_ci"][0] > 0 and p["control_days"] >= 30
    assert set(p["reasons"]) <= {"stop0", "take0", "days", "gone"} and p["reasons"]["take0"] > p["reasons"].get("stop0", 0)
    assert p["labels"]["take0"] == "涨一成" and p["split"]["in"]["days"] >= 30 and p["years"] and p["groups"]["state"]
    assert p["worst"] and all(w["name"] == w["listing_id"] and w["ret"] is not None for w in p["worst"])
    # a judge is read as a plan: the next open, its lines, its days
    j = event_study(lambda m: paths, load_rule(RULE + 'judge: {fail: "C * 0.9", target: "C * 1.1", days: 30}\n'), ["CN"], ds[60],
                    ds[-1], oos)["summary"]
    assert j["metric"] == "plan" and j["plan"]["plan"]["entry"] == "open" and j["plan"]["expectancy"] > 0


def test_dropping_each_condition_says_which_ones_help(market):
    paths, ds = market
    r = checkup(lambda m: paths, load_rule(RULE), ["CN"], ds[60], ds[-1], ds[-1] - timedelta(days=365))
    verdict = {a["condition"]: a["verdict"] for a in r["ablation"]}
    assert r["metric"] == "plan" and verdict == {"today.vol": "有效", "today.pos": "多余"}   # judged by the plan's expectancy
    vol = next(a for a in r["ablation"] if a["condition"] == "today.vol")
    assert vol["signals"] > r["base"]["signals"] and vol["d_mean_in"] < 0 and vol["d_mean_out"] < 0  # changes against now
    assert vol["d_win_in"] < 0 and vol["d_win_out"] < 0                                             # the win rate beside it
    k = next(s for s in r["scans"] if s["param"] == "k")
    assert [p["value"] for p in k["points"]] == [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0] and k["current"] == 3.0
    assert all({"signals", "effective_in", "effective_out", "mean_in", "mean_out", "se_out", "win_in", "win_out"} <= set(p)
               for p in k["points"])
    assert r["compared"] == 2 + 8 + len(r["attribution"]["features"])                 # 2 conditions, 8 other values, the features


def test_a_check_up_judges_every_variant_by_the_rule_s_plan(market):
    paths, ds = market
    rule = load_rule(RULE + 'plan: {entry: close, stop: [{line: "C * 0.9"}], take: [{line: "C * 1.1"}], days: 30}\n')
    r = checkup(lambda m: paths, rule, ["CN"], ds[60], ds[-1], ds[-1] - timedelta(days=365))
    assert r["metric"] == "plan" and r["plan"]["take"][0]["line"] == "C * 1.1"
    assert {a["condition"]: a["verdict"] for a in r["ablation"]}["today.vol"] == "有效"   # the spike still matters, judged so


def test_a_condition_on_its_own_against_random_entries(market):
    paths, ds = market
    r = single(lambda m: paths, "V >= MA(V, 20) * 3", ["CN"], ds[60], ds[-1], ds[-1] - timedelta(days=365))
    assert r["events"]["effective"] >= 100 and r["events"]["win"] > r["random"]["win"] + 0.2
    assert 0.3 < r["random"]["win"] < 0.7                                              # random entries: a coin
    # the next day's opening gap, events against random entries (the 次日容易低开 claim can be put to the data this way)
    assert r["events"]["gap"]["n"] >= r["events"]["effective"] and r["random"]["gap"]["down"] is not None   # bought or not, any window


def test_a_scan_has_at_most_fifteen_points_and_keeps_the_current_value():
    pts = scan_points(1.0, 30.0, 0.1, 7.3)
    assert len(pts) == 15 and 7.3 in pts and pts[0] == 1.0 and pts[-1] == 30.0
    assert scan_points(1, 5, 1, 3) == [1, 2, 3, 4, 5]


def test_a_checkup_reads_each_candidate_feature_on_the_signal_day(market):
    # 候选条件归因 (spec §9.5): the features as of the signal day, cut at their in-sample quintiles — here MA_DIST(20)
    # read directly from the bars on each signal day gives the same cuts
    from wb.engine.l1.compiler import Compiler
    from wb.engine.l1.frame import available_columns, build_frame
    from wb.engine.l1.run import evaluate
    from wb.validate.study import _outcomes, _signals
    from wb.validate.engine import Variant
    paths, ds = market
    rule, oos = load_rule(RULE), ds[-1] - timedelta(days=365)
    r = checkup(lambda m: paths, rule, ["CN"], ds[60], ds[-1], oos)
    a = r["attribution"]
    feats = {f["id"]: f for f in a["features"]}
    skipped = {f["id"]: f["why"] for f in a["skipped"]}
    assert {"mv", "turnover20"} <= set(skipped) and "算不出" in skipped["amount20"]       # no valuation; no amount in its bars
    assert feats["dist20"]["verdict"] in ("单调", "不单调", "样本太少") and len(feats["dist20"]["groups"]) == 5
    sig = _signals(lambda m: paths, [Variant(rule)], ["CN"], ds[60], ds[-1], None)[0]
    st = Store(paths, "CN")
    comp = Compiler("CN", available=available_columns(st))
    comp.add("d", "MA_DIST(20)", want="value")
    vals = evaluate(build_frame(st, "CN", "test", comp.program().columns), comp.program())
    ins = (sig.join(vals.select("listing_id", pl.col("date").alias("signal_date"), "d"), on=["listing_id", "signal_date"])
           .filter(pl.col("signal_date") < oos)["d"].drop_nulls())
    assert feats["dist20"]["cuts"] == pytest.approx([ins.quantile(q, "linear") for q in (0.2, 0.4, 0.6, 0.8)])
    assert isinstance(a["suggestions"], list) and a["stage"] == "today"
