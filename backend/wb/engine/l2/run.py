"""Running the structure primitives (spec §5.7).

Structures are computed from each listing's first stored bar to the snapshot's last, never on a tail
window, so the daily scan, the preview and the backtest see the same structures. Every primitive is
per listing, so a market is split into listing chunks computed in a process pool. Results are cached
per (snapshot, node hash) under derived/{snapshot_id}/l2/: the hash covers the primitive, its
parameters, those of the primitives it requires, and the engine code, so changing any of them
recomputes and nothing else does."""
import hashlib
import json
import logging
import multiprocessing
import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from functools import cache
from pathlib import Path

import polars as pl

from wb.config import Paths
from wb.data.store import Snapshot, Store
from wb.engine.l2.spec import compute
from wb.files import tmp_for
from wb.registry import PRIMITIVES, autoload

BAR_COLUMNS = ("listing_id", "date", "open", "high", "low", "close", "volume")
log = logging.getLogger("wb.engine")


def l2_bars(store: Store, market: str, profile: str, listings: list[str] | None = None, end=None,
            snap: Snapshot | None = None, extra: pl.DataFrame | None = None) -> pl.DataFrame:
    """What every primitive reads: hfq OHLCV of traded bars plus LIMIT_UP_CLOSE (撞顶涨停) and
    MACD.DIF (divergences), computed by the formula engine so charts, formulas and structures share
    one definition."""
    from wb.engine.l1.compiler import Compiler
    from wb.engine.l1.frame import available_columns, build_frame
    from wb.engine.l1.run import evaluate

    comp = Compiler(market, available=available_columns(store, snap))
    comp.add("limit_up_close", "LIMIT_UP_CLOSE")
    comp.add("dif", "MACD.DIF", want="value")
    prog = comp.program()
    frame = build_frame(store, market, profile, prog.columns | set(BAR_COLUMNS[2:]), listings, end, snap, extra=extra)
    out = evaluate(frame, prog)
    return frame.select(BAR_COLUMNS).with_columns(out["limit_up_close"], out["dif"])


CHUNK_BARS = 50_000  # a worker's peak memory is ~0.15 GB + ~2 KB per bar of its chunk (measured)


def pool_size(workers: int | None) -> int:
    """At most 8 workers by default (about 0.35 GB each with CHUNK_BARS chunks, so a full market fits
    a laptop next to the parent's ~3–4 GB); an explicit count up to Windows' limit of 61."""
    return min(workers or min(os.cpu_count() or 1, 8), 61)


def chunks(sizes: list[int], workers: int, cap: int = CHUNK_BARS) -> list[tuple[int, int]]:
    """(offset, rows) slices of bars sorted by listing, cut at listing boundaries: several per
    worker, none (beyond a single listing) much larger than `cap`."""
    target = min(sum(sizes) / (workers * 4), cap)
    out, start, rows = [], 0, 0
    for n in sizes:
        if rows and rows + n > target:
            out.append((start, rows))
            start, rows = start + rows, 0
        rows += n
    if rows:
        out.append((start, rows))
    return out


GUARD = ("全市场结构计算不能在进程池的子进程里启动：spawn 会在每个子进程里重新运行调用它的脚本，"
         "脚本要把代码放在 `if __name__ == \"__main__\":` 下面")


def refuse_in_worker() -> None:
    """Stop before any data is read when called while a spawned worker re-runs the calling script
    (or from inside a worker): otherwise a script without a main guard loads the whole market once
    per worker — 12 workers × a few GB."""
    if getattr(multiprocessing.current_process(), "_inheriting", False) or multiprocessing.parent_process() is not None:
        raise RuntimeError(GUARD)


def _job(job):
    bars, names, params = job
    return compute(bars, names, params)


# seconds without a chunk finishing: the pool is stuck — its workers held by security software, or the machine out of
# memory — and the chunks it has not finished are computed in this process. A chunk takes seconds (a worker's first
# one, with its start, up to half a minute on a 2-core Windows runner).
STALL = 180


def _stop_workers(ex: ProcessPoolExecutor) -> None:
    """A stuck pool's workers go — killed: a suspended process ignores a polite stop — so the pool can shut down
    (Python 3.14: ex.kill_workers())."""
    for p in list((getattr(ex, "_processes", None) or {}).values()):
        p.kill()


def _in_pool(bars: pl.DataFrame, cut: list[tuple[int, int]], names, params, workers: int,
             progress: Callable[[int, int], None] | None) -> list[dict | None]:
    """The chunks computed in a process pool, `progress(done, total)` as they finish; None for the ones a stuck pool
    (no chunk finished for STALL seconds) had not finished."""
    # the workers already run in parallel: two Polars threads each (read when a worker imports
    # Polars), instead of one per core in every worker (8 × 12 threads, each with its own arena)
    saved = os.environ.get("POLARS_MAX_THREADS")
    os.environ["POLARS_MAX_THREADS"] = "2"
    try:
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as ex:
            jobs = [ex.submit(_job, (bars.slice(s, n), names, params)) for s, n in cut]
            finished: set = set()
            try:
                while len(finished) < len(jobs):
                    new, _ = wait(set(jobs) - finished, timeout=STALL, return_when=FIRST_COMPLETED)
                    if not new:
                        procs = getattr(ex, "_processes", None) or {}
                        state = {pid: "running" if p.exitcode is None else f"exit {p.exitcode}" for pid, p in procs.items()}
                        log.warning("structures: the pool is stuck — no chunk finished in %s s (%d of %d done; workers %s): "
                                    "the rest are computed in this process", STALL, len(finished), len(jobs), state)
                        for j in jobs:
                            j.cancel()
                        _stop_workers(ex)
                        break
                    done = len(finished)
                    finished |= new
                    if progress:   # one call per chunk
                        for k in range(done + 1, len(finished) + 1):
                            progress(k, len(jobs))
            except BaseException:
                for j in jobs:   # a pause (raised from the progress) or a failure: the chunks not begun never begin
                    j.cancel()
                raise
            # in the listings' order, whichever finished first; a chunk that failed raises here
            return [j.result() if j in finished else None for j in jobs]
    except BrokenProcessPool as e:  # a worker died: usually a script without a main guard, or memory
        raise RuntimeError(f"进程池的子进程意外退出。{GUARD}；也可能是内存不够（可以减少 workers）") from e
    finally:
        if saved is None:
            os.environ.pop("POLARS_MAX_THREADS", None)
        else:
            os.environ["POLARS_MAX_THREADS"] = saved


def compute_market(bars: pl.DataFrame, names=None, params: dict | None = None, workers: int | None = None,
                   progress: Callable[[int, int], None] | None = None) -> dict:
    """compute() over listing chunks — in a process pool, or one after another in this process (one worker, a small
    market, or what a stuck pool left): the same tables as one call on all bars, which over a whole market would need
    tens of GB (a box's relations pair it with every other box of its listing). `bars` sorted by listing then date;
    `progress(done, total)` as the chunks finish."""
    refuse_in_worker()
    workers = pool_size(workers)
    sizes = bars.group_by("listing_id", maintain_order=True).len()["len"].to_list()
    here = workers <= 1 or len(sizes) < 2 * workers
    cut = chunks(sizes, 1 if here else workers)
    if not cut:   # no bars: the tables, empty
        return compute(bars, names, params)
    t0, how = time.perf_counter(), "this process" if here else f"{workers} workers"
    log.info("structures: %s bars of %d listings in %d chunks, %s", f"{bars.height:,}", len(sizes), len(cut), how)
    parts = [None] * len(cut) if here else _in_pool(bars, cut, names, params, workers, progress)
    done = sum(p is not None for p in parts)
    for k, (s, n) in enumerate(cut):
        if parts[k] is None:
            parts[k] = compute(bars.slice(s, n), names, params)
            done += 1
            if progress:
                progress(done, len(cut))
    log.info("structures: done in %.0f s", time.perf_counter() - t0)
    out: dict[str, dict[str, pl.DataFrame]] = {}
    for name, tables in parts[0].items():
        out[name] = {}
        for t in list(tables):  # each part's copy is let go as soon as it is merged
            out[name][t] = pl.concat([p[name].pop(t) for p in parts], how="diagonal_relaxed")
    return out


def _code_files() -> list[Path]:
    """What cached tables are computed from: the engine, and what the bars are built from — the
    quality flags, the dataset catalog and the market tables (e.g. the JP price bands)."""
    wb = Path(__file__).resolve().parents[2]
    return sorted([*(wb / "engine").rglob("*.py"), wb / "datasets.py", wb / "data" / "quality.py",
                   *(f for f in (wb / "markets").iterdir() if f.suffix in (".py", ".yaml"))])


@cache
def _code() -> bytes:
    h = hashlib.sha256()
    root = Path(__file__).resolve().parents[2]
    for f in _code_files():
        h.update(f.relative_to(root).as_posix().encode() + f.read_bytes())
    return h.digest()


def engine_hash() -> str:
    """The engine's code as a short hash (what cached tables are computed from, _code_files): a run judged by another
    engine may say otherwise (今日 judges the day before again, wb/screening/scan.py)."""
    return _code().hex()[:16]


def node_hash(name: str, params: dict | None = None) -> str:
    autoload("wb.engine.l2")
    params = params or {}
    h = hashlib.sha256(_code() + name.encode() + json.dumps(params.get(name) or {}, sort_keys=True).encode())
    for r in PRIMITIVES[name].requires:
        h.update(node_hash(r, params).encode())
    return h.hexdigest()[:16]


def _write(d: Path, tables: dict[str, pl.DataFrame]) -> None:
    tmp = tmp_for(d)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for t, df in tables.items():
        df.write_parquet(tmp / f"{t}.parquet")
    try:
        tmp.rename(d)
    except OSError:  # another process cached it first
        shutil.rmtree(tmp, ignore_errors=True)


def structures(paths: Paths, market: str, names=None, params: dict | None = None, snap: Snapshot | None = None,
               workers: int | None = None, progress: Callable[[int, int], None] | None = None) -> dict[str, dict[str, pl.DataFrame]]:
    """A market snapshot's structure tables, computed once per (snapshot, node hash) and cached; `progress` as
    compute_market says it (nothing when every table is cached)."""
    refuse_in_worker()
    autoload("wb.engine.l2")
    st = Store(paths, market)
    snap = snap or st.latest()
    if snap is None:
        raise ValueError(f"{market}: no snapshot yet")
    names = list(names or sorted(PRIMITIVES))
    base = paths.profile_dir / "derived" / snap.id / "l2"
    where = {n: base / f"{n}-{node_hash(n, params)}" for n in PRIMITIVES}

    def cached(n: str) -> bool:
        if all((where[n] / f"{t}.parquet").exists() for t in PRIMITIVES[n].tables):
            return True
        shutil.rmtree(where[n], ignore_errors=True)  # half deleted (Windows gc meeting an open file)
        return False

    missing = [n for n in names if not cached(n)]
    fresh: dict[str, dict[str, pl.DataFrame]] = {}
    if missing:
        fresh = compute_market(l2_bars(st, market, paths.profile, snap=snap), missing, params, workers, progress)
        for n, tables in fresh.items():  # what they required is cached too
            if not cached(n):
                _write(where[n], tables)
    return {n: fresh[n] if n in fresh else {t: pl.read_parquet(where[n] / f"{t}.parquet") for t in PRIMITIVES[n].tables}
            for n in names}
