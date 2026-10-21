"""The market runner (spec §5.7): listing chunks in a process pool give the same tables as one call,
and results are cached per (snapshot, node hash)."""
import polars as pl
from polars.testing import assert_frame_equal

from tests.test_l2_contract import with_dif
from tests.test_l2_prefix import series
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l2 import run as l2run
from wb.engine.l2.run import compute_market, l2_bars, node_hash, structures
from wb.engine.l2.spec import compute


def market(n: int) -> pl.DataFrame:
    return pl.concat([with_dif(series(seed, 200 + 20 * seed)).with_columns(pl.lit(f"T:{seed}").alias("listing_id"))
                      for seed in range(n)])


def test_chunks_in_processes_equal_one_call():
    bars = market(6)
    one, many = compute(bars), compute_market(bars, workers=2)
    assert one.keys() == many.keys()
    for name, tables in one.items():
        for t, df in tables.items():
            assert_frame_equal(many[name][t], df, check_dtypes=True), f"{name}.{t}"


def test_the_pool_says_how_many_of_its_chunks_are_done():
    seen = []
    compute_market(market(6), workers=2, progress=lambda done, total: seen.append((done, total)))
    total = seen[-1][1]
    assert total > 1 and seen == [(i, total) for i in range(1, total + 1)]      # one by one, up to all of them
    seen.clear()
    bars = market(6)
    here = compute_market(bars, workers=1, progress=lambda done, total: seen.append((done, total)))
    total = seen[-1][1]   # in this process, a chunk at a time too: a whole market in one call needs tens of GB
    assert total > 1 and seen == [(i, total) for i in range(1, total + 1)]
    for name, tables in compute(bars).items():
        for t, df in tables.items():
            assert_frame_equal(here[name][t], df, check_dtypes=True), f"{name}.{t}"


def test_a_stuck_pool_leaves_its_chunks_to_this_process(monkeypatch, caplog):
    # a pool where no chunk finishes for STALL seconds (its workers held by security software, say) gives the chunks
    # it has not finished back to this process, which says so and computes them: the run ends, as one call would
    import threading
    from concurrent.futures import ThreadPoolExecutor

    release, stopped = threading.Event(), []

    class Stuck(ThreadPoolExecutor):   # the first chunk's worker never answers until the pool is stopped
        def __init__(self, workers, mp_context=None):
            super().__init__(workers)
            self.first = True

        def submit(self, fn, job):
            if self.first:
                self.first = False
                return super().submit(lambda j: release.wait() and fn(j), job)
            return super().submit(fn, job)

    def stop(ex):
        stopped.append(ex)
        release.set()

    monkeypatch.setattr(l2run, "ProcessPoolExecutor", Stuck)
    monkeypatch.setattr(l2run, "_stop_workers", stop)
    monkeypatch.setattr(l2run, "STALL", 0.5)
    bars, seen = market(6), []
    with caplog.at_level("WARNING", logger="wb.engine"):
        out = compute_market(bars, workers=2, progress=lambda done, total: seen.append((done, total)))
    assert stopped and "stuck" in caplog.text
    total = seen[-1][1]
    assert seen == [(i, total) for i in range(1, total + 1)]                    # counted on to the end
    for name, tables in compute(bars).items():
        for t, df in tables.items():
            assert_frame_equal(out[name][t], df, check_dtypes=True), f"{name}.{t}"


def test_an_interruption_from_the_progress_stops_the_pool():
    import pytest

    class Stop(Exception):
        pass

    def stop(done, total):
        raise Stop
    with pytest.raises(Stop):                                                   # a pause: the chunks not begun never run
        compute_market(market(6), workers=2, progress=stop)


def test_bars_carry_the_formula_engines_limit_and_dif(paths):
    write(paths, 320)
    st = Store(paths, "CN")
    bars = l2_bars(st, "CN", "test")
    assert bars.columns == ["listing_id", "date", "open", "high", "low", "close", "volume", "limit_up_close", "dif"]
    assert bars["dif"].null_count() == 0 and bars["limit_up_close"].dtype == pl.Boolean


def test_structures_are_cached_per_snapshot_and_parameters(paths, monkeypatch):
    write(paths, 320)
    first = structures(paths, "CN", ["BOX"], workers=1)
    derived = paths.profile_dir / "derived"
    assert len(list(derived.rglob("BOX-*"))) == 1

    def recomputed(*a, **k):
        raise AssertionError("recomputed a cached node")

    monkeypatch.setattr(l2run, "compute_market", recomputed)
    assert_frame_equal(structures(paths, "CN", ["BOX"])["BOX"]["boxes"], first["BOX"]["boxes"])
    monkeypatch.undo()
    structures(paths, "CN", ["BOX"], {"BOX": {"tol": 0.02}}, workers=1)
    assert len(list(derived.rglob("BOX-*"))) == 2
    # a node's hash covers the parameters of what it requires
    assert node_hash("BOX_DERIVED") != node_hash("BOX_DERIVED", {"BOX": {"tol": 0.02}})


def test_pool_size_respects_the_windows_limit_and_a_laptop():
    from wb.engine.l2.run import pool_size

    # an explicit count up to Windows' 61; by default at most 8 workers (each holds a chunk in memory)
    assert pool_size(100) == 61 and pool_size(4) == 4 and 1 <= pool_size(None) <= 8


def test_chunks_are_cut_at_listings_and_kept_small():
    from wb.engine.l2.run import chunks

    sizes = [30_000] * 10 + [70_000]
    cut = chunks(sizes, workers=2, cap=50_000)
    assert sum(n for _, n in cut) == sum(sizes)                     # every bar once, in order
    assert all(s2 == s1 + n1 for (s1, n1), (s2, _) in zip(cut, cut[1:], strict=False))
    assert max(n for _, n in cut) == 70_000                         # one listing is never split
    assert all(n <= 60_000 for _, n in cut[:-1])                    # otherwise about the cap


def test_market_entry_points_refuse_to_start_inside_a_starting_worker(paths, monkeypatch):
    # spawn re-runs the calling script in every worker; refusing there, before any data is read,
    # keeps a script without a main guard from loading the whole market once per worker
    import multiprocessing

    import pytest

    monkeypatch.setattr(multiprocessing.current_process(), "_inheriting", True, raising=False)
    with pytest.raises(RuntimeError, match="if __name__"):
        structures(paths, "CN")  # no snapshot here: without the guard this would be a ValueError
    with pytest.raises(RuntimeError, match="if __name__"):
        compute_market(market(6), workers=2)


def test_a_script_without_a_main_guard_stops_with_the_reason(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    script = tmp_path / "no_guard.py"
    script.write_text("from tests.test_l2_run import market\nfrom wb.engine.l2.run import compute_market\n"
                      "compute_market(market(6), workers=2)\n", encoding="utf-8")
    # its words in UTF-8: an English Windows' console code page (1252) writes Chinese as \uXXXX escapes
    r = subprocess.run([sys.executable, str(script)], cwd=backend, capture_output=True, encoding="utf-8", timeout=300,
                       env={**os.environ, "PYTHONPATH": str(backend), "PYTHONIOENCODING": "utf-8"})
    assert r.returncode != 0 and "全市场结构计算" in r.stderr


def test_a_half_deleted_cache_entry_is_recomputed(paths):
    # Windows can leave a node directory half deleted when gc meets an open file: it must not count
    # as cached
    write(paths, 320)
    structures(paths, "CN", ["BOX"], workers=1)
    node = next((paths.profile_dir / "derived").glob("*/l2/BOX-*"))
    (node / "events.parquet").unlink()
    assert not structures(paths, "CN", ["BOX"], workers=1)["BOX"]["events"].is_empty()


def test_the_cache_key_covers_what_the_bars_are_built_from():
    from wb.engine.l2 import run as l2run

    files = l2run._code_files()
    names = {f.name for f in files}
    assert {"quality.py", "datasets.py", "cn.yaml", "jp.yaml"} <= names


def test_two_threads_caching_the_same_structures_both_finish_with_every_table(tmp_path, monkeypatch):
    # the daily scan and a page (the K-line, a research's pack) compute the same snapshot's structures at once: each
    # writer its own temporary folder — one deleting the other's half-written one left a cache with tables missing
    import threading
    both, real, errors = threading.Barrier(2), pl.DataFrame.write_parquet, []

    def write_then_wait(self, path, *a, **k):
        real(self, path, *a, **k)
        if path.name == "a.parquet":       # both have started their folders before either goes on
            both.wait(10)
    monkeypatch.setattr(pl.DataFrame, "write_parquet", write_then_wait)
    tables = {"a": pl.DataFrame({"x": [1]}), "b": pl.DataFrame({"y": [2]})}
    d = tmp_path / "cache" / "node"
    d.parent.mkdir()

    def run():
        try:
            l2run._write(d, tables)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert errors == [] and sorted(p.name for p in d.iterdir()) == ["a.parquet", "b.parquet"]
    assert [p.name for p in d.parent.iterdir()] == ["node"]                     # no temporary folder left behind
