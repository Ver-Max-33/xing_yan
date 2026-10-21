"""The scan after the close (spec §7.1): every enabled rule on a market's snapshot in date mode, recorded like any
run (every listing's judgement, as Parquet), and compared with the rule's previous whole-market run in that market:
new signals, watch entries that moved to a later stage (潜伏推进), watch entries that fell out without a signal
(失效), and new watch entries. What changed is what the notifications (§7.1 飞书) and the 今日 page report."""
import json
import logging
from collections.abc import Callable
from datetime import date

import polars as pl

from wb.config import Paths
from wb.db import connect
from wb.engine.l2.run import engine_hash
from wb.engine.l3.rule import Rule, RuleError, rule_hash
from wb.engine.l3.run import PHASES, run_rows, run_rule
from wb.workspace.rules import Rules

KINDS = ("signal", "advance", "invalid", "watch")
log = logging.getLogger("wb.scan")


class Paused(Exception):  # noqa: N818 - what happened, not an error: the scan was paused
    """Raised from a scan's progress to stop it where it is (暂停); the rules it finished are kept."""


def enabled_rules(paths: Paths) -> list[tuple[Rule, int]]:
    """The rules you enabled (spec §7: an enabled rule is scanned after every close), each at its latest version."""
    store, out = Rules(paths), []
    for r in store.catalog():
        if r["enabled"] and not r["error"]:
            try:
                rule, _, version = store.get(r["id"])
            except RuleError:
                continue
            out.append((rule, version))
    return out


def previous_run(paths: Paths, rule_id: str, market: str, before: date) -> dict | None:
    """The rule's last whole-market run in `market` for a day before `before` — one whose file is there: a record
    whose file is gone (deleted by hand, or kept by a file still open on Windows) is passed over, not read."""
    for row in connect(paths.app_db, "app").execute(
            "SELECT * FROM rule_run WHERE rule_id=? AND market=? AND mode='date' AND pool='null' AND day < ? "
            "ORDER BY day DESC, created_at DESC", (rule_id, market, before.isoformat())):
        if (paths.profile_dir / "runs" / f"{row['id']}.parquet").exists():
            return dict(row)
    return None


def _deepest(detail) -> str | None:
    try:
        return json.loads(detail).get("deepest_stage") if detail else None
    except (ValueError, AttributeError):
        return None


def compare(prev: pl.DataFrame | None, cur: pl.DataFrame, stages: list[str]) -> dict[str, list[dict]]:
    """What changed since the previous run: `signal` (new signals, `from_watch` when the listing was on the watch
    list), `advance` (a watch entry now at a later stage), `invalid` (a watch entry that fell out without a signal —
    an unknown day is not counted as falling out), `watch` (new on the watch list)."""
    structs = [s for s in stages if s in cur.columns]
    order = {s: i for i, s in enumerate(stages)}

    def info(r: dict) -> dict:
        return {"status": r["status"], "signal_date": r.get("signal_date"), "stage": _deepest(r.get("detail")),
                "structure": next((r[s] for s in structs if r.get(s)), None)}

    before = {r["listing_id"]: info(r) for r in prev.iter_rows(named=True)} if prev is not None else {}
    out: dict[str, list[dict]] = {k: [] for k in KINDS}
    for r in cur.iter_rows(named=True):
        now, was = info(r), before.get(r["listing_id"])
        item = {"listing_id": r["listing_id"], "structure": now["structure"], "stage": now["stage"]}
        if now["status"] == "signal" and not (was and was["status"] == "signal"):   # new: not a signal the day before (10-13)
            out["signal"].append({**item, "signal_date": now["signal_date"], "from_watch": bool(was and was["status"] == "watch")})
        elif now["status"] == "watch" and was and was["status"] == "watch":
            if order.get(now["stage"] or "", -1) > order.get(was["stage"] or "", -1):
                out["advance"].append({**item, "was": was["stage"]})
        elif now["status"] == "watch":
            out["watch"].append(item)
        elif now["status"] == "fail" and was and was["status"] == "watch":
            out["invalid"].append({**item, "structure": was["structure"], "stage": was["stage"]})
    return out


def _again(paths: Paths, market: str, rule: Rule, version: int, prev: dict) -> dict:
    """The day before judged again by the rule and engine as they are now — recorded as that day's latest run, so 今日
    compares like with like (you chose, 10-13: 87% of 10-12's 失效 came from 10-11's rule changes, not the market); the
    old run when that day cannot be judged again."""
    try:
        run = run_rule(paths, rule, market, day=date.fromisoformat(prev["day"]), version=version, record=True)
    except Exception:  # noqa: BLE001 - the comparison with the old run is still a comparison
        log.exception("%s %s: the day before not judged again", market, rule.id)
        return prev
    return dict(connect(paths.app_db, "app").execute("SELECT * FROM rule_run WHERE id=?", (run.id,)).fetchone())


def _summary(paths: Paths, market: str, rule: Rule, version: int, run_id: str, day: date, snapshot_id: str,
             counts: dict, rows: pl.DataFrame) -> dict:
    prev = previous_run(paths, rule.id, market, day)
    if prev is not None and (prev["rule_hash"] != rule_hash(rule) or prev.get("engine") != engine_hash()):
        prev = _again(paths, market, rule, version, prev)
    changes = compare(run_rows(paths, prev["id"]) if prev else None, rows, [s.id for s in rule.stages])
    return {"rule_id": rule.id, "version": version, "name": rule.name, "market": market, "run_id": run_id,
            "stages": [s.id for s in rule.stages], "snapshot_id": snapshot_id, "day": day, "counts": counts,
            "changes": changes, "previous_run": prev["id"] if prev else None}


def scan(paths: Paths, market: str, rules: list[tuple[Rule, int]], day: date | None = None,
         progress: Callable[[dict], None] | None = None, done: dict[str, str] | None = None,
         finished: Callable[[str, str], None] | None = None) -> list[dict]:
    """Each rule that names `market`, run on its latest snapshot (or `day`), recorded and compared. A rule that fails
    (a field this data profile lacks …) is reported with its reason, `{…, "error"}`; the others still run.
    `progress({i, n, rule, phase, done, total})`: the i-th of the n rules, at which step of its run (run_rule) — it may
    raise Paused to stop the scan there. `done`: rules a paused scan finished (→ their run ids), read back from their
    records, not run again (keyed "rule id@version"); `finished(key, run id)` as each rule is done."""
    out, mine = [], [(r, v) for r, v in rules if market in r.markets]
    for i, (rule, version) in enumerate(mine, 1):
        def say(phase: str, done: int, total: int, i: int = i, name: str = rule.name) -> None:
            if progress:
                progress({"i": i, "n": len(mine), "rule": name, "phase": phase, "done": done, "total": total})
        key = f"{rule.id}@{version}"   # a rule changed since the pause is run again
        if (rid := (done or {}).get(key)) is not None:
            rec = connect(paths.app_db, "app").execute("SELECT day, snapshot_id, counts FROM rule_run WHERE id=?", (rid,)).fetchone()
            if rec is not None:   # its run is still there: as it was
                say(PHASES[-1], 1, 1)
                out.append(_summary(paths, market, rule, version, rid, date.fromisoformat(rec["day"]), rec["snapshot_id"],
                                    json.loads(rec["counts"]), run_rows(paths, rid)))
                continue
        try:
            run = run_rule(paths, rule, market, day=day, version=version, record=True, progress=say)
        except Paused:
            raise
        except Exception as e:  # noqa: BLE001 - one rule's failure must not stop the scan of the others
            log.exception("%s %s failed", market, rule.id)
            out.append({"rule_id": rule.id, "version": version, "name": rule.name, "market": market, "error": str(e)})
            continue
        out.append(_summary(paths, market, rule, version, run.id, run.day, run.snapshot_id, run.counts, run.rows))
        if finished:
            finished(key, run.id)
    return out
