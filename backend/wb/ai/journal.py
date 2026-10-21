"""研究记录 (spec §10.3, §10.4): each research kept with the evidence pack it was given and the model's checked answer; the
next research of the stock is told what the last one said, and shown what changed since. 20 trading days on, each is
settled against the market's benchmark from the first open after it was made — a research made during a session counts
from the next day's open, never from one it could have seen. Forward only: a research always reads the latest day
there is data for (a backtest of AI on history carries what the model learned in training, §10.4)."""
import json
import logging
import statistics
import uuid
from bisect import bisect_right
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from wb.ai.client import LLM, AIUnavailable
from wb.ai.evidence import pack as evidence_pack
from wb.ai.tasks.research import LEANS, research
from wb.config import Paths
from wb.data.store import Store
from wb.db import connect, tx
from wb.devdata import DEMO
from wb.engine.l3.rule import RuleError
from wb.listing import parse_id
from wb.markets import MARKETS, load_market
from wb.validate.outcomes import HOLD, outcomes

log = logging.getLogger(__name__)
ENOUGH = 30   # settled researches of a lean before its numbers are read as more than numbers (the floor of §9.1)


def _db(paths: Paths):
    return connect(paths.shared_db, "shared")


def _row(r, full: bool = True) -> dict:
    d = dict(r)
    d["output"], d["flags"], d["demo"] = json.loads(d["output"]), json.loads(d["flags"]), bool(d["demo"])
    pack = json.loads(d.pop("pack"))
    if full:
        d["pack"] = pack
    else:
        d["name"] = pack.get("name")
    return d


def save(paths: Paths, r: dict) -> dict:
    rid = uuid.uuid4().hex[:12]
    _db(paths).execute(
        "INSERT INTO research (id, listing_id, day, profile, demo, trigger, pack, pack_hash, output, status, flags, call_id, "
        "model, cost, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, r["listing_id"], r["day"], paths.profile, int(r.get("demo", False)), r["trigger"],
         json.dumps(r["pack"], ensure_ascii=False), r["pack"]["hash"], json.dumps(r["output"], ensure_ascii=False), r["status"],
         json.dumps(r["flags"], ensure_ascii=False), r.get("call_id"), r.get("model"), r.get("cost"),
         r.get("created_at") or datetime.now().astimezone().isoformat(timespec="seconds")))
    return get(paths, rid)


def get(paths: Paths, rid: str) -> dict:
    row = _db(paths).execute("SELECT * FROM research WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise KeyError(f"没有这条研究记录：{rid}")
    return _row(row)


def history(paths: Paths, listing_id: str) -> list[dict]:
    """The stock's researches, newest first."""
    return [_row(r) for r in _db(paths).execute("SELECT * FROM research WHERE listing_id=? ORDER BY rowid DESC", (listing_id,))]


def latest(paths: Paths, listing_id: str) -> dict | None:
    row = _db(paths).execute("SELECT * FROM research WHERE listing_id=? ORDER BY rowid DESC LIMIT 1", (listing_id,)).fetchone()
    return _row(row) if row else None


def recent(paths: Paths, limit: int = -1) -> list[dict]:
    """Every stock's researches, newest first, without their packs."""
    return [_row(r, full=False) for r in _db(paths).execute("SELECT * FROM research ORDER BY rowid DESC LIMIT ?", (limit,))]


def diff(old: dict, new: dict) -> dict:
    """What changed between two evidence packs: facts added, gone, and read differently (by fact id)."""
    a, b = {f["id"]: f for f in old["facts"]}, {f["id"]: f for f in new["facts"]}
    return {"added": [f for k, f in b.items() if k not in a], "removed": [f for k, f in a.items() if k not in b],
            "changed": [{"id": k, "before": a[k]["text"], "after": f["text"]} for k, f in b.items() if k in a and a[k]["text"] != f["text"]]}


def fresh_docs(paths: Paths, lid: str, day: date, source=None) -> list[str]:
    """The stock's announcements of the last 90 days, asked for now (when 本地存储 is on for cninfo), and the newest
    ones' text read from their PDFs; what could not be fetched or read is named in the pack, so the research says
    资料不足 instead of reading what is kept as the whole."""
    from wb.docs.announcements import fetch_texts, for_text, kept, refresh
    try:
        refresh(paths, lid, day - timedelta(days=90), day, source)
    except Exception as e:  # noqa: BLE001 - offline or the site down: the research still runs on what is kept
        log.warning("cninfo %s: %s", lid, e)
        return [f"巨潮资讯公告（这次没取到：{e}）"]
    errors = fetch_texts(paths, for_text(kept(paths, lid, day - timedelta(days=90), day)), source)
    return [f"巨潮公告正文（{len(errors)} 篇没读到：{'；'.join(errors)[:300]}）"] if errors else []


def run(llm: LLM, paths_of: Callable[[str], Paths], listing_id: str, trigger: str = "manual", source=None) -> dict:
    """Research the stock on the latest day there is data for, told what its last research said; kept. `source`: the
    announcements' source (a stand-in in tests)."""
    market, _ = parse_id(listing_id)
    paths = paths_of(market)
    snap = Store(paths, market).latest()
    if snap is None:
        raise ValueError(f"{market} 还没有数据")
    if Store(paths, market).read("instruments", snap).filter(pl.col("listing_id") == listing_id).is_empty():
        raise ValueError(f"{market} 的数据里没有 {listing_id}")
    if not (ready := llm.status()["tasks"]["research"])["ready"]:   # before any fetch: AI waits, nothing is asked
        raise AIUnavailable(ready["reason"])
    demo = snap.note == DEMO   # made-up data is never mixed with fetched announcements
    missing = fresh_docs(paths, listing_id, snap.as_of, source) if market == "CN" and not demo else []
    p = evidence_pack(paths_of, listing_id, snap.as_of, left_out=missing)
    last = latest(paths, listing_id)
    r = research(llm, p, {"day": last["day"], "output": last["output"]} if last else None)
    return save(paths, {"listing_id": listing_id, "day": p["day"], "trigger": trigger, "pack": p, "demo": demo, **r})


def settle(paths_of: Callable[[str], Paths]) -> int:
    """Settle the researches whose 20 trading days have passed, each on the data profile it was made on: from the open
    of the first trading day after the market's date it was made, the stock's return and its excess over the benchmark
    (wb.validate.outcomes). -> researches settled."""
    conn = _db(paths_of(MARKETS[0]))
    rows = conn.execute("SELECT id, listing_id, profile, created_at FROM research WHERE settled_at IS NULL AND demo=0").fetchall()
    done = 0
    for market in sorted({parse_id(r["listing_id"])[0] for r in rows}):
        paths = paths_of(market)
        mine = [r for r in rows if parse_id(r["listing_id"])[0] == market and r["profile"] == paths.profile]
        store = Store(paths, market)
        snap = store.latest() if mine else None
        if snap is None:
            continue
        tz = ZoneInfo(load_market(market).timezone)
        cal = store.read("calendar", snap).filter(pl.col("is_open"))["date"].sort().to_list()
        due = []
        for r in mine:
            made = datetime.fromisoformat(r["created_at"]).astimezone(tz).date()
            k = bisect_right(cal, made) - 1   # the last trading day by then: its next day's open is the first after
            if 0 <= k and made <= cal[-1] and k + HOLD < len(cal) and cal[k + HOLD] <= snap.as_of:
                due.append((r["id"], r["listing_id"], cal[k], cal[k + HOLD]))
        if not due:
            continue
        try:
            o = outcomes(paths, market, pl.DataFrame({"listing_id": [d[1] for d in due], "signal_date": [d[2] for d in due]}))
        except RuleError as e:   # no benchmark bars yet: wait
            log.warning("research settle %s: %s", market, e)
            continue
        by = {(x["listing_id"], x["signal_date"]): x for x in o.iter_rows(named=True)}
        with tx(conn):
            for rid, lid, base, last in due:
                x = by[(lid, base)]
                outcome = "gone" if x["gone"] else "halt" if x["blocked"] == "halt" else "settled"
                ok = outcome == "settled"
                conn.execute("UPDATE research SET entry_date=?, settled_at=?, outcome=?, return20=?, excess20=? WHERE id=?",
                             (str(x["entry_date"]) if x["entry_date"] else None, str(last), outcome,
                              x["r20"] if ok else None, x["x20"] if ok else None, rid))
        done += len(due)
    return done


def evaluation(paths: Paths) -> dict:
    """What followed each lean (§10.4): researches made on real data, settled ones' 20-day excess over the benchmark."""
    rows = [r for r in recent(paths) if not r["demo"]]
    groups = []
    for lean in LEANS:
        mine = [r for r in rows if r["output"].get("lean") == lean]
        xs = [r["excess20"] for r in mine if r["outcome"] == "settled" and r["excess20"] is not None]
        # ponytail: overlapping windows of one stock's researches count separately; thin them if one stock dominates
        groups.append({"lean": lean, "n": len(mine), "settled": len(xs), "mean_excess": statistics.fmean(xs) if xs else None,
                       "median_excess": statistics.median(xs) if xs else None,
                       "win": sum(x > 0 for x in xs) / len(xs) if xs else None})
    return {"groups": groups, "pending": sum(r["settled_at"] is None for r in rows), "hold": HOLD,
            "enough": all(g["settled"] >= ENOUGH for g in groups if g["lean"] != "相当")}
