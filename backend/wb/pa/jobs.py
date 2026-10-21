"""价格行为分析 in the background (design 2026-10-20 §6, §7): the refusals come first, in plain words and before anything
is kept; then one analysis at a time on its own thread, its stage written to its record as it goes, cancellable."""
import logging
import threading
from datetime import datetime
from typing import Callable, NamedTuple

import httpx
import polars as pl
from pa_agent.util.threading import CancelToken

from wb.ai.client import LLM, AIUnavailable, TaskModel
from wb.config import Paths
from wb.data.store import Store
from wb.datause import SOURCES, allowed, source_of
from wb.listing import parse_id
from wb.pa import records
from wb.pa.frame import DailyInput, daily_input, with_previous
from wb.pa.run import analyze
from wb.pa.settings import load_task, status
from wb.workspace.store import Workspace

log = logging.getLogger(__name__)
TIMEOUT = 1800.0     # a long think at high effort, then a long JSON answer
CANCELLING = "取消中：等这一步的回答回来就停（这一步照样计费）"   # a call under way cannot be called back


class Started(NamedTuple):
    id: str
    reused: bool   # the same input was analysed already: its analysis, no model asked


class Busy(Exception):
    def __init__(self, code: str):
        super().__init__(f"正在分析 {code}，等它做完再开始")
        self.code = code


def ready(paths_of: Callable[[str], Paths], listing_id: str) -> tuple[Paths, TaskModel]:
    """The refusals that need no bars, in the order a start checks them: A shares only, a listing the data knows, its
    price source's 「发送给 LLM」 on (ValueError), the model ready (AIUnavailable, with its reason)."""
    market, _ = parse_id(listing_id)
    if market != "CN":
        raise ValueError("价格行为分析只支持 A 股")
    paths = paths_of(market)
    store = Store(paths, market)
    snap = store.latest()
    if snap is None or store.read("instruments", snap).filter(pl.col("listing_id") == listing_id).is_empty():
        raise ValueError(f"CN 的数据里没有 {listing_id}")
    src = source_of(market, paths.profile)
    if not allowed(Workspace(paths).settings(), src, "llm"):
        raise ValueError(f"数据使用里关掉了 {SOURCES.get(src, src)} 的「发送给 LLM」，打开后才能分析")
    tm = load_task(paths)
    if not (st := status(LLM(paths), tm))["ready"]:
        raise AIUnavailable(st["reason"])
    return paths, tm


class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self._running: dict[str, tuple[str, CancelToken, threading.Thread, Paths]] = {}  # id -> (code, cancel, thread, paths)

    def start(self, paths_of: Callable[[str], Paths], listing_id: str, *,
              transport: httpx.BaseTransport | None = None) -> Started:
        """The refusals (ValueError 422, AIUnavailable 503, Busy 409, NotEnoughBars 422), then the analysis already made
        from this very input, or a new record and its run."""
        paths, tm = ready(paths_of, listing_id)
        with self._lock:
            if self._running:
                raise Busy(next(iter(self._running.values()))[0])
            inp = daily_input(paths, listing_id)
            prev = records.previous_plan(paths, listing_id, inp.day, calendar=records.calendar(paths))
            inp = with_previous(inp, prev)
            task = tm.model_dump()
            if done := records.same(paths, inp, previous=prev, task=task):
                return Started(done, True)
            aid = records.create(paths, inp, previous=prev, now=datetime.now(records.TZ), model=tm.model, task=task)
            cancel = CancelToken()
            t = threading.Thread(target=self._run, args=(aid, paths, inp, tm, cancel, transport), name=f"pa-{inp.code}",
                                 daemon=True)
            self._running[aid] = (inp.code, cancel, t, paths)
        t.start()
        return Started(aid, False)

    def _run(self, aid: str, paths: Paths, inp: DailyInput, tm: TaskModel, cancel: CancelToken,
             transport: httpx.BaseTransport | None) -> None:
        try:   # its own client: a SQLite connection stays on the thread that opened it
            r = analyze(inp, LLM(paths, transport=transport, timeout=TIMEOUT), tm, cancel=cancel,
                        on_stage=lambda s: None if cancel.is_set() else records.set_stage(paths, aid, s))
            records.finish(paths, aid, r)
        except Exception:   # the row stays running until the next start's recover; said in the log
            log.exception("价格行为分析 %s 没能存下结果", aid)
        finally:
            with self._lock:
                self._running.pop(aid, None)

    def cancel(self, analysis_id: str) -> bool:
        """Stops the run after the call under way comes back; its record says so at once."""
        with self._lock:
            run = self._running.get(analysis_id)
        if run:
            run[1].set()
            records.set_stage(run[3], analysis_id, CANCELLING)
        return run is not None

    def wait(self, analysis_id: str, timeout: float = 60) -> None:
        with self._lock:
            run = self._running.get(analysis_id)
        if run:
            run[2].join(timeout)


RUNNER = Runner()
