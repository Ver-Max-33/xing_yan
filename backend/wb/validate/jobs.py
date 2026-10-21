"""验证的运行 (spec §9): queued and run one at a time in a background thread of the backend, each taking the market
run's turn (after a scan that is running: they read the whole market as a scan does); kept in shared.sqlite with what
they found, so a result stays with the rule version, the data profiles and the split it was computed on. A run can be
paused (it stops after the chunk under way and keeps the chunks it finished: 继续 goes on from there) or cancelled
(nothing of it kept); runs an earlier process left unfinished come back paused. 校准 (§6.5) runs in the same queue
(1.33), its parameter sets kept as it scores them."""
import json
import math
import queue
import shutil
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect, tx
from wb.engine.l3.calibrate import calibrate
from wb.engine.l3.rule import Rule, RuleError, load_rule, rule_hash
from wb.engine.l3.run import RUN_LOCK
from wb.markets import MARKETS, load_market
from wb.validate.backtest import backtest, book_settings, robustness
from wb.validate.course import CHECKS
from wb.validate.engine import KEEP, Keep
from wb.validate.portfolio import Costs, Settings
from wb.validate.study import checkup, event_study, single
from wb.workspace.rules import Rules

KINDS = ("event", "checkup", "single", "backtest", "robust")   # what /api/validate starts; "calibrate" comes from the rule page
TABLES = ("trades", "ledger", "nav")   # a book's, as Parquet beside the run
OOS_YEARS = 2     # the last two years are 样本外 (§9.5), fixed before a run
YEARS = 10        # the default range


class Paused(Exception):  # noqa: N818 - what happened, not an error
    """Raised at a run's next step when 暂停 was asked for: the chunks it finished are kept."""


class Cancelled(Exception):  # noqa: N818
    """Raised at a run's next step when 取消 was asked for: nothing of it is kept."""


def _json(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json(x) for x in v]
    return v.isoformat() if isinstance(v, date) else v


def _costs(d: dict) -> Costs:
    """A stored book's costs: the days its rates changed back from text."""
    return Costs(**{**d, "history": tuple((date.fromisoformat(u), r, v) for u, r, v in d.get("history") or ())})


def split_day(start: date, end: date) -> date:
    """The first day of 样本外: the last OOS_YEARS, but never more than 40% of the range (a short history keeps a
    样本内 to compare with)."""
    return max(end - timedelta(days=365 * OOS_YEARS), start + (end - start) * 0.6)


def setup(paths_of: Callable[[str], Paths], markets: list[str]) -> dict:
    """What a run would read: each market's profile and its first and last day; the default range (ten years, after
    a year of history for the formulas to start from) and its split; each market's book by default (组合回测); the
    stock-picking claims as single-condition tests."""
    info = []
    for m in markets:
        st = Store(paths_of(m), m)
        snap = st.latest()
        first = st.scan("bars_1d", snap).select(pl.col("date").min()).collect().item() if snap else None
        profile = paths_of(m).profile
        info.append({"market": m, "profile": profile, "benchmark": load_market(m).benchmark[profile], "first": first,
                     "last": snap.as_of if snap else None})
    have = [i for i in info if i["last"]]
    books = {m: asdict(book_settings(m)) for m in markets}
    if not have:
        return {"markets": info, "start": None, "end": None, "oos_start": None, "books": books, "checks": CHECKS}
    end = max(i["last"] for i in have)
    start = max(end - timedelta(days=365 * YEARS), min(i["first"] for i in have) + timedelta(days=365))
    return {"markets": info, "start": start, "end": end, "oos_start": split_day(start, end), "books": books, "checks": CHECKS}


class Validations:
    def __init__(self, paths_of: Callable[[str], Paths], inputs: Callable[[str], tuple] | None = None):
        """`inputs(rule_id)`: what a calibration of the rule runs on — your Y / N marks and box samples, read when it
        runs (as calibrate() takes them)."""
        self.paths_of, self.inputs = paths_of, inputs
        base = paths_of(MARKETS[0])
        self.folder, self.db = base.root / "validation", base.shared_db
        conn = self._conn()   # what an earlier process left unfinished (the app quit): paused, to be continued
        conn.execute("UPDATE validation_run SET status='paused', progress='应用关掉时还没算完：做完的部分保留，点「继续」接着算' "
                     "WHERE status='running'")
        conn.execute("UPDATE validation_run SET status='paused', progress='应用关掉时还在排队：点「继续」运行' WHERE status='queued'")
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._asked: dict[str, str] = {}   # run id → "pause" or "cancel": taken at the run's next step

    def _conn(self):  # a connection a thread: requests and the worker each open their own
        return connect(self.db, "shared")

    # ---- starting ---------------------------------------------------------------------------------------------
    def start(self, kind: str, markets: list[str], start: date, end: date, oos_start: date,
              rule: Rule | None = None, version: int | None = None, expr: str | None = None, settings: dict | None = None,
              axes: list[str] | None = None) -> str:
        """`settings`: a book's (组合回测、稳健性), by market — a calibration's: the draft's text if it was not a saved
        version, the slider values, the years and the sample; `axes`: the heatmap's two parameters (稳健性)."""
        rid = uuid.uuid4().hex[:12]
        conn = self._conn()
        with tx(conn):
            conn.execute(
                "INSERT INTO validation_run (id, kind, rule_id, version, rule_hash, expr, markets, profiles, start, end, "
                "oos_start, settings, axes, status, progress, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '排队中', ?)",
                (rid, kind, rule.id if rule else None, version, rule_hash(rule) if rule else None, expr, json.dumps(markets),
                 json.dumps({m: self.paths_of(m).profile for m in markets}), start.isoformat(), end.isoformat(),
                 oos_start.isoformat(), json.dumps(_json(settings if kind == "calibrate" else {m: asdict(s) for m, s in settings.items()}))
                 if settings else None,
                 json.dumps(axes) if axes else None, datetime.now().isoformat(timespec="seconds")))
        self._queue.put((rid, kind, rule, expr, markets, start, end, oos_start, settings, axes))
        self._work()
        return rid

    def _work(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="validations", daemon=True)
                self._worker.start()

    # ---- 暂停、继续、取消 ----------------------------------------------------------------------------------------
    def _status(self, rid: str) -> str | None:
        r = self._conn().execute("SELECT status FROM validation_run WHERE id=?", (rid,)).fetchone()
        return r["status"] if r else None

    def _drop(self, rid: str) -> None:
        shutil.rmtree(self.folder / f"{rid}.chunks", ignore_errors=True)

    def _check(self, rid: str) -> None:
        """At a run's step: stop it here when 暂停 or 取消 was asked for."""
        asked = self._asked.get(rid)
        if asked == "cancel":
            raise Cancelled
        if asked == "pause":
            raise Paused

    def pause(self, rid: str) -> bool:
        """暂停: a running run stops after the chunk under way and keeps the chunks it finished. False: it is not running
        (a queued one is cancelled, not paused)."""
        if self._status(rid) != "running":
            return False
        self._asked[rid] = "pause"
        self._set(self._conn(), rid, progress="正在暂停：做完这一步就停")
        return True

    def cancel(self, rid: str) -> bool:
        """取消: a running run stops at its next step, a queued or paused one at once; nothing of it is kept. False: it
        has finished."""
        st = self._status(rid)
        if st not in ("running", "queued", "paused"):
            return False
        self._asked[rid] = "cancel"
        if st == "running":
            self._set(self._conn(), rid, progress="正在取消：做完这一步就停")
        else:
            self._set(self._conn(), rid, status="cancelled", progress=None, finished_at=datetime.now().isoformat(timespec="seconds"))
            self._drop(rid)
        return True

    def resume(self, rid: str) -> bool:
        """继续 a paused run: queued again, the chunks it finished read back when it runs. False: it is not paused.
        ValueError: its rule version is gone."""
        conn = self._conn()
        row = conn.execute("SELECT * FROM validation_run WHERE id=?", (rid,)).fetchone()
        if row is None or row["status"] != "paused":
            return False
        rule, s = None, json.loads(row["settings"]) if row["settings"] else None
        if row["kind"] != "single":
            try:
                if row["kind"] == "calibrate" and s["text"]:   # a draft, as it was
                    rule = load_rule(s["text"])
                else:
                    rule = Rules(self.paths_of(MARKETS[0])).get(row["rule_id"], row["version"])[0]
                if row["kind"] == "calibrate" and s["params"]:
                    rule = rule.with_params(s["params"])
            except RuleError as e:
                raise ValueError(f"没法继续：{e}。重新运行一次") from None
        settings = s if row["kind"] == "calibrate" else \
            {m: Settings(**{**d, "costs": _costs(d["costs"])}) for m, d in s.items()} if s else None
        day = date.fromisoformat
        self._asked.pop(rid, None)
        self._set(conn, rid, status="queued", progress="排队中")
        self._queue.put((rid, row["kind"], rule, row["expr"], json.loads(row["markets"]), day(row["start"]), day(row["end"]),
                         day(row["oos_start"]), settings, json.loads(row["axes"]) if row["axes"] else None))
        self._work()
        return True

    def _set(self, conn, rid: str, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE validation_run SET {sets} WHERE id=?", (*cols.values(), rid))

    def _loop(self) -> None:
        conn = self._conn()
        while True:
            job = self._queue.get()
            rid, kind, rule, expr, markets, start, end, oos_start, settings, axes = job
            if self._status(rid) != "queued":   # cancelled while it waited
                self._asked.pop(rid, None)
                continue
            token, last = KEEP.set(Keep(self.folder / f"{rid}.chunks")), ["开始"]

            def say(text: str, rid: str = rid) -> None:   # a step done: where a pause or a cancel stops the run
                last[0] = text
                self._check(rid)
                self._set(conn, rid, progress=text)
            try:
                self._set(conn, rid, progress="等正在进行的全市场计算（每日扫描等）结束")
                while not RUN_LOCK.acquire(timeout=0.5):   # waited for — a cancel still stops the wait
                    self._check(rid)
                try:
                    self._set(conn, rid, status="running", progress="开始")
                    if kind == "event":
                        out = event_study(self.paths_of, rule, markets, start, end, oos_start, say)
                        self.folder.mkdir(parents=True, exist_ok=True)
                        out["rows"].write_parquet(self.folder / f"{rid}.parquet")
                        result = {"summary": out["summary"]}
                    elif kind == "backtest":
                        out = backtest(self.paths_of, rule, markets, start, end, oos_start, settings, say)
                        self.folder.mkdir(parents=True, exist_ok=True)
                        for t in TABLES:
                            out[t].write_parquet(self.folder / f"{rid}.{t}.parquet")
                        result = {"books": out["books"]}
                    elif kind == "robust":
                        result = robustness(self.paths_of, rule, markets, start, end, oos_start, settings, axes, say)
                    elif kind == "checkup":
                        result = checkup(self.paths_of, rule, markets, start, end, oos_start, say)
                    elif kind == "calibrate":
                        marks, labels = self.inputs(rule.id)   # as they are when it runs
                        result = calibrate(self.paths_of, rule, marks, labels, years=settings["years"], sample_n=settings["sample"]["n"],
                                           seed=settings["sample"]["seed"], progress=say, keep=KEEP.get())
                    else:
                        result = single(self.paths_of, expr, markets, start, end, oos_start, say)
                finally:
                    RUN_LOCK.release()
                self._set(conn, rid, status="done", progress=None, result=json.dumps(_json(result), ensure_ascii=False),
                          finished_at=datetime.now().isoformat(timespec="seconds"))
                self._drop(rid)
            except Paused:
                self._set(conn, rid, status="paused", progress=f"已暂停，做到：{last[0]}。做完的部分保留，「继续」接着算")
            except Cancelled:
                self._set(conn, rid, status="cancelled", progress=None, finished_at=datetime.now().isoformat(timespec="seconds"))
                self._drop(rid)
            except Exception as e:  # a run that fails says why; the next one still runs
                traceback.print_exc()
                self._drop(rid)
                try:
                    self._set(conn, rid, status="error", progress=None, error=str(e) or type(e).__name__,
                              finished_at=datetime.now().isoformat(timespec="seconds"))
                except Exception:  # noqa: BLE001 - the worker goes on; get() says the run did not finish
                    traceback.print_exc()
            finally:
                KEEP.reset(token)
                self._asked.pop(rid, None)

    # ---- reading ----------------------------------------------------------------------------------------------
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["markets"], d["profiles"] = json.loads(d["markets"]), json.loads(d["profiles"])
        if "settings" in d:
            d["settings"] = json.loads(d["settings"]) if d["settings"] else None
        if "result" in d:
            d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def get(self, rid: str) -> dict:
        conn = self._conn()
        r = conn.execute("SELECT * FROM validation_run WHERE id=?", (rid,)).fetchone()
        if r is None:
            raise KeyError(f"没有验证运行 {rid}")
        if r["status"] in ("queued", "running") and (self._worker is None or not self._worker.is_alive()):
            self._set(conn, rid, status="error", progress=None, error="验证的后台线程停了，没跑完：重新运行一次",
                      finished_at=datetime.now().isoformat(timespec="seconds"))   # never shown running forever
            r = conn.execute("SELECT * FROM validation_run WHERE id=?", (rid,)).fetchone()
        return {**self._row(r), "asked": self._asked.get(rid)}   # a pause or a cancel waiting for the run's next step

    def runs(self, rule_id: str | None = None, kind: str | None = None, limit: int = 20) -> list[dict]:
        cols = "id, kind, rule_id, version, expr, markets, profiles, start, end, oos_start, status, progress, error, created_at, finished_at"
        where, args = [], []
        for col, v in (("rule_id", rule_id), ("kind", kind)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        q = f"SELECT {cols} FROM validation_run" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        return [self._row(r) for r in self._conn().execute(q, (*args, limit))]

    def table(self, rid: str, name: str) -> pl.DataFrame:
        """A book's trades, cash ledger or NAV."""
        path = self.folder / f"{rid}.{name}.parquet"
        if name not in TABLES or not path.exists():
            raise KeyError(f"验证运行 {rid} 没有{ {'trades': '成交', 'ledger': '资金账本', 'nav': '净值'}.get(name, name) }")
        return pl.read_parquet(path)

    def rows(self, rid: str) -> pl.DataFrame:
        path = self.folder / f"{rid}.parquet"
        if not path.exists():
            raise KeyError(f"验证运行 {rid} 没有逐个信号的结果")
        return pl.read_parquet(path)
