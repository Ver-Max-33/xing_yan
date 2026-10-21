"""The daily scan (spec §7.1): every enabled rule on the market's snapshot, recorded, and compared with the rule's
previous run — new signals, watch entries that moved on, watch entries that fell out (失效), new watch entries."""
import json
from datetime import date

import polars as pl

from tests.test_l3_preview import TUNED
from wb.devdata import trading_days, write
from wb.engine.l3.rule import load_rule
from wb.engine.l3.run import PHASES
from wb.screening.scan import compare, enabled_rules, scan
from wb.workspace.rules import Rules

STAGES = ["rally", "box", "breakout", "hold"]


def rows(*r):
    return pl.DataFrame([{"listing_id": lid, "status": st, "signal_date": sd, "box": bid,
                          "detail": json.dumps({"deepest_stage": deep}) if deep else None} for lid, st, sd, bid, deep in r],
                        schema={"listing_id": pl.Utf8, "status": pl.Utf8, "signal_date": pl.Date, "box": pl.Utf8, "detail": pl.Utf8})


def test_the_comparison_names_what_changed_since_the_last_run():
    d1, d2 = date(2026, 9, 29), date(2026, 9, 30)
    prev = rows(("CN:1", "watch", None, "b1", "box"), ("CN:2", "watch", None, "b2", "box"), ("CN:3", "watch", None, "b3", "box"),
                ("CN:4", "signal", d1, "b4", "hold"), ("CN:5", "fail", None, None, None))
    cur = rows(("CN:1", "signal", d2, "b1", "hold"),          # the watch entry got its signal
               ("CN:2", "watch", None, "b2", "breakout"),       # moved on to the breakout
               ("CN:3", "fail", None, None, None),             # fell out without a signal: 失效
               ("CN:4", "fail", None, None, None),             # yesterday's signal is simply over
               ("CN:5", "watch", None, "b5", "box"))           # a new box to watch
    c = compare(prev, cur, STAGES)
    assert [(x["listing_id"], x["from_watch"]) for x in c["signal"]] == [("CN:1", True)]
    assert [(x["listing_id"], x["stage"], x["was"]) for x in c["advance"]] == [("CN:2", "breakout", "box")]
    assert [(x["listing_id"], x["structure"]) for x in c["invalid"]] == [("CN:3", "b3")]
    assert [x["listing_id"] for x in c["watch"]] == ["CN:5"]
    first = compare(None, cur, STAGES)                         # no previous run: every signal is new, nothing fell out
    assert [x["listing_id"] for x in first["signal"]] == ["CN:1"] and first["invalid"] == []


def test_the_scan_says_which_rule_it_is_at_and_how_far_that_rule_is(paths):
    write(paths, 320)
    rules = Rules(paths)
    rules.save(TUNED)
    rules.save(TUNED.replace("id: above_ma", "id: above_ma_2").replace("name: 站上 20 日线", "name: 站上 20 日线 · 二"))
    for rid in ("above_ma", "above_ma_2"):
        rules.set_state(rid, enabled=True)
    seen = []
    scan(paths, "CN", enabled_rules(paths), progress=seen.append)
    assert {(x["i"], x["n"], x["rule"]) for x in seen} == {(1, 2, "站上 20 日线"), (2, 2, "站上 20 日线 · 二")}
    assert [x["i"] for x in seen] == sorted(x["i"] for x in seen)               # one rule after the other
    for i in (1, 2):
        steps = [x["phase"] for x in seen if x["i"] == i]
        assert list(dict.fromkeys(steps)) == list(PHASES)                       # every step, in order
        counted = [(x["done"], x["total"]) for x in seen if x["i"] == i and x["phase"] == PHASES[3]]
        assert counted == sorted(counted) and counted[-1][0] == counted[-1][1] > 0   # counts up to all it lists


def test_the_scan_runs_the_enabled_rules_and_compares_with_the_previous_day(paths):
    write(paths, 320)
    rules = Rules(paths)
    rules.save(TUNED)
    rules.set_state("above_ma", enabled=True)
    assert [r.id for r, _ in enabled_rules(paths)] == ["above_ma"]
    days = trading_days(320)
    first = scan(paths, "CN", enabled_rules(paths), day=days[-2])
    second = scan(paths, "CN", enabled_rules(paths), day=days[-1])
    [a], [b] = first, second
    assert (a["rule_id"], a["day"], a["previous_run"]) == ("above_ma", days[-2], None)
    assert b["previous_run"] == a["run_id"] and b["day"] == days[-1]
    assert set(b["changes"]) == {"signal", "advance", "invalid", "watch"} and b["counts"]["signal"] >= 0
    assert scan(paths, "JP", [(load_rule(TUNED.replace("markets: [CN, JP]", "markets: [CN]")), 1)]) == []   # not this market's rule


def test_a_run_whose_file_is_gone_is_passed_over_not_read(paths):
    # a run's record without its file (deleted by hand, or kept by a file still open on Windows): the comparison and
    # the 选股 page use the run before it — reading it failed the next scan of the rule and the page (500)
    from wb.screening.scan import previous_run
    write(paths, 320)
    rules = Rules(paths)
    rules.save(TUNED)
    rules.set_state("above_ma", enabled=True)
    days = trading_days(320)
    [a] = scan(paths, "CN", enabled_rules(paths), day=days[-3])
    [b] = scan(paths, "CN", enabled_rules(paths), day=days[-2])
    (paths.profile_dir / "runs" / f"{b['run_id']}.parquet").unlink()
    assert previous_run(paths, "above_ma", "CN", days[-1])["id"] == a["run_id"]
    [c] = scan(paths, "CN", enabled_rules(paths), day=days[-1])
    assert "error" not in c and c["previous_run"] == a["run_id"]


def test_a_stock_that_was_a_signal_the_day_before_too_is_no_new_signal():
    """You chose (10-13): 「新信号」 is a stock that was not a signal the day before — on 10-12 240 of 横盘蓄势's 270 were
    10-09's signals again (a state the rule holds day after day), each said as new."""
    d1, d2 = date(2026, 9, 29), date(2026, 9, 30)
    prev = rows(("CN:1", "signal", d1, None, "hold"), ("CN:2", "watch", None, "b2", "box"))
    cur = rows(("CN:1", "signal", d2, None, "hold"), ("CN:2", "signal", d2, "b2", "hold"))
    assert [x["listing_id"] for x in compare(prev, cur, STAGES)["signal"]] == ["CN:2"]


def run_record(paths, run_id):
    from wb.db import connect
    return dict(connect(paths.app_db, "app").execute("SELECT * FROM rule_run WHERE id=?", (run_id,)).fetchone())


def test_after_a_rule_changes_the_day_before_is_judged_again_by_it(paths):
    """You chose (10-13): after a rule's change — or the engine's — 今日 compares with the day before judged by the rule
    as it is now (87% of 10-12's 失效 came from 10-11's rule changes, not the market); recorded as that day's latest run."""
    from wb.engine.l3.rule import rule_hash
    write(paths, 320)
    rules = Rules(paths)
    rules.save(TUNED)
    rules.set_state("above_ma", enabled=True)
    days = trading_days(320)
    [a] = scan(paths, "CN", enabled_rules(paths), day=days[-3])
    [b] = scan(paths, "CN", enabled_rules(paths), day=days[-2])
    assert b["previous_run"] == a["run_id"]                                 # the same rule: the run as it was
    rules.save(TUNED.replace("value: 20,", "value: 30,"))                   # a new version
    [(rule, _)] = enabled_rules(paths)
    [c] = scan(paths, "CN", enabled_rules(paths), day=days[-1])
    again = run_record(paths, c["previous_run"])
    assert c["previous_run"] != b["run_id"] and again["day"] == days[-2].isoformat() and again["rule_hash"] == rule_hash(rule)


def test_after_the_engine_changes_the_day_before_is_judged_again(paths):
    from wb.db import connect
    write(paths, 320)
    rules = Rules(paths)
    rules.save(TUNED)
    rules.set_state("above_ma", enabled=True)
    days = trading_days(320)
    [a] = scan(paths, "CN", enabled_rules(paths), day=days[-2])
    connect(paths.app_db, "app").execute("UPDATE rule_run SET engine='an older engine' WHERE id=?", (a["run_id"],))
    [b] = scan(paths, "CN", enabled_rules(paths), day=days[-1])
    assert b["previous_run"] != a["run_id"] and run_record(paths, b["previous_run"])["day"] == days[-2].isoformat()


def test_a_days_signal_says_whether_its_structures_were_completed_before(paths):
    """You chose (10-13): 再次突破 marked — the scan's signal that a backtest does not count (its box broken out of
    before); a signal of a rule without structures, a formula's, is always its first."""
    write(paths, 320)
    rules = Rules(paths)
    rules.save(TUNED)
    rules.set_state("above_ma", enabled=True)
    [s] = scan(paths, "CN", enabled_rules(paths), day=trading_days(320)[-1])
    from wb.engine.l3.run import run_rows
    rows = run_rows(paths, s["run_id"])
    sig = rows.filter(pl.col("status") == "signal")
    assert sig.height and sig["repeat"].to_list() == [False] * sig.height and rows.filter(pl.col("status") != "signal")["repeat"].null_count() == rows.height - sig.height
