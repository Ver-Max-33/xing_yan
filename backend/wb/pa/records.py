"""价格行为分析's records (design 2026-10-20 §5.1–§5.3): one row per analysis in pa_analysis, from running to ok, failed
or cancelled; the previous plan PA's continuity check is told about; 盘中 analyses; the first open a plan may use."""
import json
import uuid
from bisect import bisect_right
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect
from wb.markets import load_market
from wb.pa.frame import DailyInput, PreviousPlan
from wb.pa.plans import is_order
from wb.pa.run import Result, code_version, prompt_version

CN = load_market("CN")
TZ = ZoneInfo(CN.timezone)
OPEN = CN.sessions[0][0]          # 09:30
JSON_COLS = ("call_ids", "input", "stage1", "stage2", "ai_plan", "final_plan", "changes")
HEAVY = ("input", "stage1", "stage2")
RESTARTED = "应用重启，分析中断"


def _db(paths: Paths):
    return connect(paths.shared_db, "shared")


def _row(r, full: bool = True) -> dict:
    d = dict(r)
    for c in JSON_COLS:
        if c in d:
            d[c] = json.loads(d[c]) if d[c] is not None else None
    d["demo"] = bool(d["demo"])
    if not full:
        for c in HEAVY:
            d.pop(c)
    return d


def calendar(paths: Paths) -> list[date]:
    """The exchange's open days in the latest CN snapshot, oldest first."""
    store = Store(paths, "CN")
    snap = store.latest()
    return store.read("calendar", snap).filter(pl.col("is_open"))["date"].sort().to_list() if snap else []


def intraday(day: date, now: datetime, calendar: list[date]) -> bool:
    """§5.1 盘中: a trading day after day 0 had opened by `now` — during its session, or after its close before its
    data was stored — so the AI never saw it, and the plan starts after it."""
    local = now.astimezone(TZ)
    return any(day < d and (d < local.date() or (d == local.date() and local.time() >= OPEN)) for d in calendar)


def start_date(created_at: datetime, calendar: list[date]) -> date | None:
    """§5.2: the trading day of the first open after the analysis — journal.settle's rule (a day made on a trading
    day starts the next one); None while the calendar has no day after it."""
    k = bisect_right(calendar, created_at.astimezone(TZ).date()) - 1
    return calendar[k + 1] if k + 1 < len(calendar) else None


def same(paths: Paths, inp: DailyInput, *, previous: PreviousPlan | None, task: dict) -> str | None:
    """The analysis already made from this very input — the same bars to the cent, the same rules (the prompt version and
    our code around PA), the same model and effort, the same last plan — or None. The model's answer is a draw: asked again, it may answer
    otherwise, so the same input keeps the answer it got (10-21)."""
    for r in _db(paths).execute("SELECT id, input, previous_id FROM pa_analysis WHERE listing_id=? AND day=? AND profile=? "
                                "AND status='ok' AND prompt_hash=? ORDER BY rowid DESC",
                                (inp.listing_id, inp.day.isoformat(), inp.profile, prompt_version())):
        was = json.loads(r["input"])
        if (was.get("task") == task and was.get("bars") == inp.bars_json and was.get("code") == code_version()
                and r["previous_id"] == (previous.id if previous else None)):
            return r["id"]
    return None


def create(paths: Paths, inp: DailyInput, *, previous: PreviousPlan | None, now: datetime, model: str,
           task: dict | None = None) -> str:
    cal = calendar(paths)
    conn = _db(paths)
    earlier = conn.execute("SELECT 1 FROM pa_analysis WHERE listing_id=? AND day=? AND profile=? AND status='ok' LIMIT 1",
                           (inp.listing_id, inp.day.isoformat(), inp.profile)).fetchone()
    start = start_date(now, cal)
    f = inp.frame
    aid = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO pa_analysis (id, listing_id, day, profile, trigger, demo, adjusted, intraday, first_of_day, status, "
        "model, prompt_hash, input, previous_id, created_at, start_date) VALUES (?,?,?,?,'manual',?,?,?,?,'running',?,?,?,?,?,?)",
        (aid, inp.listing_id, inp.day.isoformat(), inp.profile, int(inp.demo), int(inp.adjusted),
         int(intraday(inp.day, now, cal)), int(earlier is None), model, prompt_version(),
         json.dumps({"bars": inp.bars_json, "k1_close": inp.k1_close, "adj0": inp.adj0, "next_limit_up": f.next_limit_up,
                     "next_limit_down": f.next_limit_down, "task": task, "code": code_version()},
                    ensure_ascii=False),
         previous.id if previous else None, now.isoformat(timespec="seconds"), start.isoformat() if start else None))
    return aid


def set_stage(paths: Paths, analysis_id: str, text: str) -> None:
    _db(paths).execute("UPDATE pa_analysis SET stage=? WHERE id=?", (text, analysis_id))


def finish(paths: Paths, analysis_id: str, r: Result) -> dict:
    def js(v):
        return json.dumps(v, ensure_ascii=False) if v is not None else None

    _db(paths).execute(
        "UPDATE pa_analysis SET status=?, error=?, stage=NULL, cost=?, latency_ms=?, call_ids=?, "
        "prompt_hash=COALESCE(NULLIF(?, ''), prompt_hash), stage1=?, "
        "stage2=?, ai_plan=?, final_plan=?, changes=?, finished_at=? WHERE id=?",
        (r.status, r.error, r.cost, r.latency_ms, json.dumps(r.call_ids), r.prompt_hash, js(r.stage1),
         js(r.stage2), js(r.ai_plan), js(r.final_plan), js(r.changes),
         datetime.now(TZ).isoformat(timespec="seconds"), analysis_id))
    return get(paths, analysis_id)


def get(paths: Paths, analysis_id: str) -> dict | None:
    row = _db(paths).execute("SELECT * FROM pa_analysis WHERE id=?", (analysis_id,)).fetchone()
    return _row(row) if row else None


def history(paths: Paths, listing_id: str) -> list[dict]:
    """The stock's analyses, newest first."""
    return [_row(r) for r in _db(paths).execute("SELECT * FROM pa_analysis WHERE listing_id=? ORDER BY rowid DESC",
                                                (listing_id,))]


def recent(paths: Paths, limit: int = 200) -> list[dict]:
    """Every stock's analyses, newest first, without the bars and the stages' answers."""
    return [_row(r, full=False) for r in _db(paths).execute("SELECT * FROM pa_analysis ORDER BY rowid DESC LIMIT ?",
                                                            (limit,))]


def previous_plan(paths: Paths, listing_id: str, day: date, *, calendar: list[date]) -> PreviousPlan | None:
    """§5.3: the newest successful analysis of this stock, on this data profile, whose final plan is an order (PA's
    trade log keeps only those, however long ago), from an earlier day 0: another analysis of the same bars is not a
    plan before them (10-21); the trading days between the two day 0s, at least 1."""
    for r in _db(paths).execute("SELECT id, day, created_at, final_plan, input FROM pa_analysis WHERE listing_id=? "
                                "AND profile=? AND status='ok' AND day<? ORDER BY rowid DESC",
                                (listing_id, paths.profile, day.isoformat())):
        plan = json.loads(r["final_plan"]) if r["final_plan"] else None
        if is_order(plan):
            bars = bisect_right(calendar, day) - bisect_right(calendar, date.fromisoformat(r["day"]))
            return PreviousPlan(r["id"], r["created_at"], plan, max(1, bars), json.loads(r["input"]).get("adj0"))
    return None


def recover(paths: Paths) -> int:
    """At startup: an analysis still running was cut off by the app's restart (Review Focus 4)."""
    return _db(paths).execute("UPDATE pa_analysis SET status='failed', error=?, stage=NULL WHERE status='running'",
                              (RESTARTED,)).rowcount


def trades(paths: Paths, analysis_id: str) -> dict:
    """The analysis's settled plans: track (ai / final) → target (tp1 / tp2) → its pa_trade row, None until settled."""
    out = {t: {g: None for g in ("tp1", "tp2")} for t in ("ai", "final")}
    for r in _db(paths).execute("SELECT * FROM pa_trade WHERE analysis_id=?", (analysis_id,)):
        out[r["track"]][r["target"]] = dict(r)
    return out
