"""The signals of a rule and its variants over a whole market, a chunk of listings at a time (spec §9.5 计算量): the
same signals as one whole-market run, a variant without a condition is the rule written without it, another
parameter value is the rule with that value, and a cross-sectional rule still reads the whole market."""
from datetime import date

import polars as pl
import pytest

from tests.test_l3_run import ABOVE_MA, RANKED
from wb.devdata import write
from wb.engine.l3.rule import load_rule
from wb.engine.l3.run import run_rule
from wb.validate import engine
from wb.validate.engine import Variant, market_signals

TWO = ABOVE_MA.replace('when: "C > MA(C, 20)"}', 'when: "C > MA(C, 20)", at: [{id: vol, expr: "V >= MA(V, 5) * $k"}]}').replace(
    "signal_at:", "params:\n  - {id: k, value: 1.2, range: [0.5, 2.0], step: 0.1}\nsignal_at:")


def keys(df):
    return sorted(zip(df["listing_id"], df["signal_date"], strict=True))


@pytest.fixture
def data(paths, monkeypatch):
    write(paths, 320)
    monkeypatch.setattr(engine, "CHUNK", 1)  # one listing at a time
    return paths


ALL = (date(1990, 1, 1), date(2100, 1, 1))


def whole(paths, rule, market="CN"):
    rule = load_rule(rule) if isinstance(rule, str) else rule
    return run_rule(paths, rule, market, start=ALL[0], end=ALL[1], record=False).rows


def test_chunks_give_the_whole_markets_signals_and_variants_are_their_own_rules(data):
    rule = load_rule(TWO)
    ref = whole(data, rule)
    base, no_vol, k2 = market_signals(data, "CN", [Variant(rule), Variant(rule, frozenset({("today", "vol")})),
                                                    Variant(rule.with_params({"k": 2.0}))], *ALL)
    assert keys(base) == keys(ref) and base.height > 0
    assert keys(no_vol) == keys(whole(data, ABOVE_MA))
    assert keys(k2) == keys(whole(data, rule.with_params({"k": 2.0})))
    assert len(keys(k2)) < len(keys(base)) < len(keys(no_vol))


def test_a_cross_sectional_rule_reads_the_whole_market_however_it_is_chunked(data):
    rule = load_rule(RANKED)
    ref = whole(data, rule)
    got = market_signals(data, "CN", [Variant(rule)], *ALL)[0]
    assert keys(got) == keys(ref) and got.height > 0


def test_a_threshold_compared_at_the_condition_shares_the_frame_and_is_still_its_own_rule(data):
    rule = load_rule(ABOVE_MA.replace('when: "C > MA(C, 20)"}', 'when: "C > MA(C, 20)", at: [{id: lo, expr: "RAW_C >= $lo"}]}')
                     .replace("signal_at:", "params:\n  - {id: lo, value: 10, range: [0, 2000], step: 10}\nsignal_at:"))
    hi = rule.with_params({"lo": 1000})
    assert engine._key(rule, "CN", None) == engine._key(hi, "CN", None)       # one evaluated frame for both
    a, b = market_signals(data, "CN", [Variant(rule), Variant(hi)], *ALL)
    assert keys(a) == keys(whole(data, rule)) and keys(b) == keys(whole(data, hi)) and 0 < len(keys(b)) < len(keys(a))


def boxes_market(paths, n=40, days=900, seed=3):
    """Sideways ranges between trends, so boxes form and break (the demo data's random walks seldom make one)."""
    import numpy as np

    from wb.data.store import Store
    from wb.datasets import conform
    rng = np.random.default_rng(seed)
    ds, d = [], date(2026, 9, 30)
    while len(ds) < days:
        if d.weekday() < 5:
            ds.append(d)
        d = date.fromordinal(d.toordinal() - 1)
    ds = ds[::-1]
    rows = []
    for i in range(n):
        level, t, closes = 10.0 + i, 0, []
        while t < days:
            span = int(rng.integers(30, 60))
            closes += [level * (1 + 0.035 * np.sin(k / 2.5) + rng.normal(0, 0.004)) for k in range(span)]   # a range
            up = int(rng.integers(8, 20))
            closes += [level * (1.012 ** k) * (1 + rng.normal(0, 0.004)) for k in range(1, up + 1)]           # a trend
            level, t = closes[-1], len(closes)
        closes = closes[:days]
        for k, c in enumerate(closes):
            o = closes[k - 1] if k else c
            rows.append((f"CN:{600000 + i}", ds[k], o, max(o, c) * 1.008, min(o, c) * 0.992, c,
                         float(rng.uniform(0.8, 1.2) * 1e6 * (2.5 if k and c > closes[k - 1] * 1.02 else 1.0)), o * 1.1))
    bars = pl.DataFrame(rows, schema=["listing_id", "date", "open", "high", "low", "close", "volume", "up_limit"], orient="row")
    idx = (bars.group_by("date").agg(pl.col("open").mean(), pl.col("high").mean(), pl.col("low").mean(), pl.col("close").mean())
           .sort("date").with_columns(pl.lit("t").alias("source")))     # the index: the market's average
    st = Store(paths, "CN")
    st.publish({"bars_1d": conform(bars.with_columns(pl.lit(1.0).alias("adj_factor"), pl.lit("t").alias("source")), "bars_1d"),
                "instruments": conform(pl.DataFrame({"listing_id": [f"CN:{600000 + i}" for i in range(n)], "market": "CN",
                                                     "code": [str(600000 + i) for i in range(n)], "status": "L", "source": "t"}),
                                       "instruments"),
                "calendar": conform(pl.DataFrame({"date": ds, "is_open": True, "source": "t"}), "calendar"),
                "index_bars_1d": conform(pl.concat([idx.with_columns(pl.lit(i).alias("index_id"))
                                                    for i in ("CN:IDX:000001", "CN:IDX:399001", "CN:IDX:000300")]), "index_bars_1d")},
               as_of=ds[-1], note="boxes")


@pytest.mark.parametrize("rid", ["tpl_shang_pin", "tpl_tian_pin"])
def test_a_rule_with_structures_reads_the_markets_cache_a_chunk_at_a_time(paths, monkeypatch, rid):
    from wb.workspace.rules import builtin_rules
    boxes_market(paths)
    monkeypatch.setattr(engine, "CHUNK", 7)
    rule = builtin_rules()[rid][0]          # a box, whose touches and events are keyed by the box
    got = market_signals(paths, "CN", [Variant(rule)], *ALL)[0]
    assert got.height > 0 and keys(got) == keys(whole(paths, rule))


def test_a_part_done_step_by_step_reads_back_its_finished_steps_and_drops_a_line_cut_off(tmp_path):
    # a calibration's parameter sets (1.33): one line a step, read back by a run continued later
    keep = engine.Keep(tmp_path / "run.chunks")
    assert keep.rows(("marks", "CN")) == []
    keep.add(("marks", "CN"), [True, False])
    keep.add(("marks", "CN"), [False, False])
    with open(next((tmp_path / "run.chunks").glob("*")), "a") as f:
        f.write("[tru")                                              # the app quit while a line was written
    assert keep.rows(("marks", "CN")) == [[True, False], [False, False]]
    keep.add(("marks", "CN"), [True, True])
    assert keep.rows(("marks", "CN")) == [[True, False], [False, False], [True, True]]
    assert keep.rows(("marks", "JP")) == []
