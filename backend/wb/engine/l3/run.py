"""Running a rule over a market snapshot (spec §5.5 three modes, §5.9 run records, §7.1 storage).

The structures a rule binds are computed with the rule's own primitive settings — over the whole
market from the cache (`wb.engine.l2.run.structures`), or directly for a list of listings — and the
formula frame holds only the columns the rule reads. A date run gives one row per listing in the pool
(signal / watch / unknown / fail, with the explanation for signals and watches); a range run gives the
signals. Every run is recorded with its rule version and snapshot, and its rows are kept as Parquet
under runs/{run_id}.parquet in the profile (not with the snapshot's caches, which gc removes)."""
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect, tx
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l2.run import compute_market, engine_hash, l2_bars, structures
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import STRUCTURES, Rule, RuleError, _exprs, rule_hash
from wb.registry import PRIMITIVES, autoload

# one full-market run at a time — a scan after the close, a run or calibration from the app, a sample of a
# cross-sectional rule (it reads the whole market): two at once would double the ~4.8 GB peak (spec §5.7). The app's
# own runs and calibrations refuse while it is held, samples wait up to WAIT seconds, the daily scan waits for it;
# the job holding it runs its own samples (a calibration's galleries).
RUN_LOCK = threading.RLock()
WAIT = 60
# a date run's steps, in order, as 今日 shows them while a scan runs; the structures and the explanations are counted
PHASES = ("计算结构", "读取数据", "逐只判定", "整理命中原因", "保存结果")
_KEPT: ContextVar[dict | None] = ContextVar("kept_market_frame", default=None)


@contextmanager
def market_turn(xs: bool) -> Iterator[None]:
    """A sample of a cross-sectional rule reads the whole market (`frame_for`) as a scan does: it takes the run's turn."""
    if not xs:
        yield
        return
    if not RUN_LOCK.acquire(timeout=WAIT):
        raise RuleError("每日扫描、校准或验证正在全市场上计算；这条规则用了截面函数（排名、行业中位数等），要读全市场，等它算完再试")
    try:
        yield
    finally:
        RUN_LOCK.release()


@contextmanager
def one_market_frame() -> Iterator[None]:
    """Inside, the whole-market frame of a cross-sectional rule's samples is built once and reused (the latest one
    only): a calibration counts each candidate's hits with one build per market."""
    token = _KEPT.set({})
    try:
        yield
    finally:
        _KEPT.reset(token)


@dataclass
class Run:
    id: str
    rule: Rule
    market: str
    snapshot_id: str
    mode: str
    day: date | None
    start: date | None
    end: date | None
    counts: dict
    rows: pl.DataFrame
    matcher: Matcher

    @property
    def warnings(self) -> list[str]:
        return self.matcher.warnings


def primitives(rule: Rule) -> list[str]:
    """The primitives a rule's structure stages read, with what they require."""
    autoload("wb.engine.l2")
    out: set[str] = set()
    todo = [STRUCTURES[s.use][0] for s in rule.stages if s.kind == "structure"] + (["BOX"] if rule.reads_pressure else [])
    if any(".false_bottom" in text for _, _, text in _exprs(rule)):   # a box's 假底 (a plan's 失效线) is BOX_DERIVED's
        todo.append("BOX_DERIVED")
    while todo:
        p = todo.pop()
        if p not in out:
            out.add(p)
            todo += list(PRIMITIVES[p].requires)
    return sorted(out)


def frame_for(store: Store, market: str, profile: str, rule: Rule, available: set[str], listings: list[str] | None,
              snap) -> tuple[pl.DataFrame, list[str] | None]:
    """The frame a rule's formulas run on and the listings it judges: a cross-sectional value (a rank among the
    day's listings, an industry's median) on a sample alone would be the sample's, so such a rule reads the
    whole market and keeps the sample (`Matcher(pool=)`)."""
    prog = Matcher.program(rule, market, available)
    if listings is None or not prog.xs:
        return build_frame(store, market, profile, prog.columns, listings, snap=snap), None
    kept, key = _KEPT.get(), (market, profile, snap.id if snap else None, frozenset(prog.columns))
    if kept is None:
        return build_frame(store, market, profile, prog.columns, None, snap=snap), listings
    if key not in kept:
        kept.clear()  # one market's frame at a time
        kept[key] = build_frame(store, market, profile, prog.columns, None, snap=snap)
    return kept[key], listings


def _jsonable(v):
    return v.isoformat() if isinstance(v, date) else v


def run_rule(paths: Paths, rule: Rule, market: str, *, day: date | None = None, start: date | None = None,
             end: date | None = None, listings: list[str] | None = None, workers: int | None = None,
             version: int | None = None, record: bool = True,
             progress: Callable[[str, int, int], None] | None = None) -> Run:
    """`record=False` for previews and single-stock debugging (spec §6.3): nothing is stored. `progress(step, done,
    total)` at each of PHASES (done and total 0 where it is not counted)."""
    def say(step: str, done: int = 0, total: int = 0) -> None:
        if progress:
            progress(step, done, total)
    store = Store(paths, market)
    snap = store.latest()
    if snap is None:
        raise RuleError(f"{market} 还没有数据快照")
    available = available_columns(store, snap)
    with market_turn(listings is not None and Matcher.cross_sectional(rule, market, available)):
        names, params = primitives(rule), rule.primitive_params()
        say(PHASES[0])
        if not names:
            tables = {}
        elif listings is None:
            tables = structures(paths, market, names, params, snap=snap, workers=workers,
                                progress=lambda done, total: say(PHASES[0], done, total))
        else:  # chosen listings (a gallery sample): in the process pool when there are many
            tables = compute_market(l2_bars(store, market, paths.profile, listings, snap=snap), names, params, workers)
        say(PHASES[1])
        frame, pool = frame_for(store, market, paths.profile, rule, available, listings, snap)
        calendar = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
        m = Matcher(rule, market, frame, tables, calendar, available, pool)
        info = store.read("instruments", snap).select("listing_id", "name")
        say(PHASES[2])
        if start is not None or end is not None:
            mode = "range"
            start = start or frame["date"].min()
            end = end or frame["date"].max()
            rows = m.signals(start, end).join(info, on="listing_id", how="left")
            counts = {"signal": rows.height}
        else:
            mode = "date"
            day = day or frame["date"].max()
            rows = (m.at(day).join(info, on="listing_id", how="left", maintain_order="left")
                    .join(m.depths(day), on="listing_id", how="left", maintain_order="left"))
            shown = rows.filter(pl.col("status").is_in(["signal", "watch", "unknown"]))["listing_id"].to_list()
            details = {}
            say(PHASES[3], 0, len(shown))
            for k, lid in enumerate(shown, 1):   # one by one: thousands on a wide watch list (4,290 潜伏)
                details[lid] = json.dumps(m.detail(lid, day), ensure_ascii=False, default=_jsonable)
                if k % 25 == 0 or k == len(shown):
                    say(PHASES[3], k, len(shown))
            rows = rows.with_columns(pl.col("listing_id").replace_strict(details, default=None, return_dtype=pl.Utf8).alias("detail"))
            # 再次突破 (you chose, 10-13): a signal that is not its structures' first completion — the scan says it, a backtest
            # counts the first only
            again = {lid: not m.first(lid, day) for lid in rows.filter(pl.col("status") == "signal")["listing_id"].to_list()}
            rows = rows.with_columns(pl.col("listing_id").replace_strict(again, default=None, return_dtype=pl.Boolean).alias("repeat")
                                     if again else pl.lit(None, pl.Boolean).alias("repeat"))
            counts = {k: int((rows["status"] == k).sum()) for k in ("signal", "watch", "unknown", "fail")}
        run = Run(uuid.uuid4().hex[:12], rule, market, snap.id, mode, day, start, end, counts, rows, m)
        say(PHASES[4])
        if record:
            _record(paths, run, rule.version if version is None else version, listings)
        return run


def _record(paths: Paths, run: Run, version: int, listings: list[str] | None) -> None:
    folder = paths.profile_dir / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    run.rows.write_parquet(folder / f"{run.id}.parquet")
    conn = connect(paths.app_db, "app")
    conn.execute("INSERT INTO rule_run (id, rule_id, version, rule_hash, market, snapshot_id, mode, day, start, end, pool, counts, "
                 "created_at, engine) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run.id, run.rule.id, version, rule_hash(run.rule), run.market, run.snapshot_id, run.mode,
                  _jsonable(run.day), _jsonable(run.start), _jsonable(run.end), json.dumps(listings),
                  json.dumps(run.counts), datetime.now().isoformat(timespec="seconds"), engine_hash()))


def prune_runs(paths: Paths, keep: int = 60) -> int:
    """Each rule's newest `keep` runs in each market stay (今日 compares with the previous one, 选股 reads the last
    five days); older run records and their files go. A file still open elsewhere (Windows) stays for the next time.
    -> runs removed."""
    conn = connect(paths.app_db, "app")
    old = [r[0] for r in conn.execute(
        "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY rule_id, market ORDER BY created_at DESC, rowid DESC) "
        "AS n FROM rule_run) WHERE n > ?", (keep,))]
    gone = []
    for rid in old:
        try:
            (paths.profile_dir / "runs" / f"{rid}.parquet").unlink(missing_ok=True)
            gone.append(rid)
        except PermissionError:
            pass
    with tx(conn):
        conn.executemany("DELETE FROM rule_run WHERE id=?", [(rid,) for rid in gone])
    return len(gone)


def runs(paths: Paths, rule_id: str | None = None, limit: int = 50) -> list[dict]:
    conn = connect(paths.app_db, "app")
    q = "SELECT * FROM rule_run" + (" WHERE rule_id=?" if rule_id else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    rows = conn.execute(q, (rule_id, limit) if rule_id else (limit,))
    return [{**dict(r), "pool": json.loads(r["pool"]), "counts": json.loads(r["counts"])} for r in rows]


def run_rows(paths: Paths, run_id: str) -> pl.DataFrame:
    conn = connect(paths.app_db, "app")
    if conn.execute("SELECT 1 FROM rule_run WHERE id=?", (run_id,)).fetchone() is None:
        raise RuleError(f"没有运行记录 {run_id}")
    return pl.read_parquet(paths.profile_dir / "runs" / f"{run_id}.parquet")
