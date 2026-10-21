"""Immutable snapshots of market data (spec §4.3).

Files are content-addressed (`part-<sha256[:16]>.parquet`) and never modified. A snapshot is a
manifest {dataset: {partition: relative path}} stored in app.sqlite. Publishing writes only the
partitions that changed and registers the new manifest in one transaction, so a reader holding
a snapshot id never sees data change underneath it (spec §4.3 "一次扫描固定引用一个快照").
"""
import hashlib
import io
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import polars as pl

from wb.config import Paths
from wb.data.quality import Issue
from wb.datasets import DATASETS, conform, empty
from wb.db import connect, tx
from wb.files import move_into_place, tmp_for


@dataclass(frozen=True)
class Snapshot:
    id: str
    market: str
    as_of: date
    created_at: str
    parent_id: str | None
    manifest: dict[str, dict[str, str]]
    note: str


def write_partition(directory: Path, df: pl.DataFrame) -> str:
    """A content-addressed file (spec §4.3): written to a temporary file, read back and checked against its hash, then
    renamed into place — a short or damaged write never becomes part of a snapshot."""
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd", statistics=True)
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"part-{digest}.parquet"
    path = directory / name
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        tmp = tmp_for(path)
        tmp.write_bytes(data)
        if hashlib.sha256(tmp.read_bytes()).hexdigest()[:16] != digest:
            tmp.unlink(missing_ok=True)
            raise OSError(f"{path}: 写入后校验 hash 不一致（磁盘满或写入出错？），没有发布")
        move_into_place(tmp, path)  # atomic on the same filesystem
    return name


class Store:
    def __init__(self, paths: Paths, market: str):
        self.paths, self.market = paths, market
        self.base = paths.market_dir(market)
        self.derived = paths.profile_dir / "derived"  # derived/{snapshot_id}/...: results computed from a snapshot
        self.conn = connect(paths.app_db, "app")

    # ---- reading -------------------------------------------------------------------------
    def _row(self, r) -> Snapshot:
        return Snapshot(r["id"], r["market"], date.fromisoformat(r["as_of"]), r["created_at"],
                        r["parent_id"], json.loads(r["manifest"]), r["note"])

    def latest(self) -> Snapshot | None:
        r = self.conn.execute(
            "SELECT * FROM snapshot WHERE market=? ORDER BY rowid DESC LIMIT 1",  # insert order, not the clock
            (self.market,)).fetchone()
        return self._row(r) if r else None

    def get(self, snapshot_id: str) -> Snapshot:
        r = self.conn.execute("SELECT * FROM snapshot WHERE id=?", (snapshot_id,)).fetchone()
        if r is None:
            raise KeyError(snapshot_id)
        return self._row(r)

    def history(self) -> list[Snapshot]:
        rows = self.conn.execute(
            "SELECT * FROM snapshot WHERE market=? ORDER BY rowid", (self.market,))
        return [self._row(r) for r in rows]

    def files(self, dataset: str, snap: Snapshot | None = None) -> list[Path]:
        snap = snap or self.latest()
        if snap is None:
            return []
        return [self.base / rel for _, rel in sorted(snap.manifest.get(dataset, {}).items())]

    def read(self, dataset: str, snap: Snapshot | None = None) -> pl.DataFrame:
        files = self.files(dataset, snap)
        return pl.read_parquet(files) if files else empty(dataset)

    def scan(self, dataset: str, snap: Snapshot | None = None) -> pl.LazyFrame:
        files = self.files(dataset, snap)
        return pl.scan_parquet(files) if files else empty(dataset).lazy()

    # ---- writing -------------------------------------------------------------------------
    def _merge_write(self, name: str, part: str, old_rel: str | None, upd: pl.DataFrame) -> str:
        key = list(DATASETS[name].key)
        merged = upd
        if old_rel:
            old = pl.read_parquet(self.base / old_rel, memory_map=False)
            # a catalog that gained a column since `old` was written (industry's 申万二级): old rows get nulls there
            merged = pl.concat([old.join(upd.select(key), on=key, how="anti"), upd], how="diagonal_relaxed")
        fname = write_partition(self.base / name / part, merged.sort(key))
        return f"{name}/{part}/{fname}"

    def publish(self, updates: dict[str, pl.DataFrame], as_of: date, note: str = "",
                issues: dict[str, list[Issue]] | None = None) -> Snapshot:
        """Upsert `updates` (new rows win on the dataset key) on top of the latest snapshot.

        Returns the latest snapshot unchanged when nothing changed (re-importing identical data
        is a no-op).
        """
        parent = self.latest()
        if parent:
            as_of = max(as_of, parent.as_of)  # backfilling older history never moves "data as of" back
        manifest = json.loads(json.dumps(parent.manifest)) if parent else {}
        for name, upd in updates.items():
            upd = conform(upd, name)
            if upd.is_empty():
                continue
            parts = manifest.setdefault(name, {})
            if DATASETS[name].partition == "year":
                with_year = upd.with_columns(pl.col("date").dt.year().alias("_year"))
                for (year,), g in with_year.partition_by("_year", as_dict=True).items():
                    p = f"year={year}"
                    parts[p] = self._merge_write(name, p, parts.get(p), g.drop("_year"))
            else:
                parts["all"] = self._merge_write(name, "all", parts.get("all"), upd)
        if parent and manifest == parent.manifest and as_of == parent.as_of:
            return parent
        snap = Snapshot(f"{self.market}-{as_of:%Y%m%d}-{uuid.uuid4().hex[:8]}", self.market, as_of,
                        datetime.now().isoformat(timespec="microseconds"),
                        parent.id if parent else None, manifest, note)
        with tx(self.conn):
            head = self.conn.execute("SELECT id FROM snapshot WHERE market=? ORDER BY rowid DESC LIMIT 1",
                                     (self.market,)).fetchone()
            if (head["id"] if head else None) != snap.parent_id:  # another writer published meanwhile
                raise RuntimeError(f"{self.market}: snapshot {head['id']} was published meanwhile; "
                                   "nothing was published, run again")
            self.conn.execute(
                "INSERT INTO snapshot (id, market, as_of, created_at, parent_id, manifest, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snap.id, snap.market, snap.as_of.isoformat(), snap.created_at, snap.parent_id,
                 json.dumps(manifest, sort_keys=True), note))
            for ds_name, ds_issues in (issues or {}).items():
                self.conn.executemany(
                    "INSERT INTO quality_issue VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(snap.id, ds_name, i.check, i.severity, i.listing_id,
                      i.date.isoformat() if i.date else None, i.detail) for i in ds_issues])
        return snap

    def gc(self, keep: int = 10) -> int:
        """Delete data files not referenced by the newest `keep` snapshots, and the derived results of
        older snapshots. Returns data files removed."""
        kept = self.history()[-keep:]
        live = {self.base / rel for s in kept for parts in s.manifest.values() for rel in parts.values()}
        for d in self.derived.glob(f"{self.market}-*"):
            if d.name not in {s.id for s in kept}:
                shutil.rmtree(d, ignore_errors=True)  # a cache: recomputed if ever needed
        removed = 0
        for f in self.base.glob("*/*/part-*.parquet"):
            if f not in live:
                try:
                    f.unlink()
                    removed += 1
                except PermissionError:  # Windows: still open by a reader; retry at next gc
                    pass
        return removed


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class Staging:
    """Fetched-but-unpublished data. A unit (date / ticker / "all") is written to disk first and
    then logged, so an interrupted backfill resumes without refetching or duplicating (spec §4.5)."""

    def __init__(self, paths: Paths, market: str):
        self.market = market
        self.dir = paths.market_dir(market) / "staging"
        self.conn = connect(paths.app_db, "app")

    def done(self, provider: str, dataset: str, unit: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM fetch_log WHERE market=? AND provider=? AND dataset=? AND unit=?",
            (self.market, provider, dataset, unit)).fetchone() is not None

    def put(self, provider: str, dataset: str, unit: str, frames: dict[str, pl.DataFrame]) -> None:
        rows = 0
        for name, df in frames.items():
            df = conform(df, name)
            d = self.dir / name
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{provider}__{dataset}__{_UNSAFE.sub('_', unit)}.parquet"
            tmp = tmp_for(path)
            df.write_parquet(tmp)
            move_into_place(tmp, path)
            rows += df.height
        self.conn.execute(
            "INSERT OR REPLACE INTO fetch_log VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (self.market, provider, dataset, unit, rows))

    def forget(self, provider: str, dataset: str, unit: str | None = None) -> None:
        """Drop fetched units (one, or all of a dataset) so the next run asks the source again."""
        name = f"{provider}__{dataset}__{_UNSAFE.sub('_', unit) if unit else '*'}.parquet"
        for f in self.dir.glob(f"*/{name}"):
            f.unlink(missing_ok=True)
        q = "DELETE FROM fetch_log WHERE market=? AND provider=? AND dataset=?"
        args = (self.market, provider, dataset)
        self.conn.execute(q + " AND unit=?" if unit else q, (*args, unit) if unit else args)

    def pending(self) -> int:
        """Fetched units not yet published (e.g. an interrupted backfill)."""
        return self.conn.execute("SELECT COUNT(*) FROM fetch_log WHERE market=?", (self.market,)).fetchone()[0]

    def collect(self, name: str) -> pl.DataFrame:
        files = sorted((self.dir / name).glob("*.parquet"), key=lambda p: p.stat().st_mtime_ns)
        if not files:
            return empty(name)
        df = pl.concat([pl.read_parquet(f, memory_map=False) for f in files])
        return df.unique(subset=list(DATASETS[name].key), keep="last", maintain_order=True)

    def clear(self) -> None:
        for f in self.dir.glob("*/*.parquet"):
            f.unlink()
        self.conn.execute("DELETE FROM fetch_log WHERE market=?", (self.market,))

    def set_aside(self, keep: int = 5) -> Path | None:
        """A batch the quality checks refused (spec §4.6): moved under staging/blocked/<time>/ for inspection (the newest
        `keep` kept) and forgotten, so the next run asks the source again — it may have corrected its answer meanwhile;
        kept in place, every later try (the retry, 立即更新, the morning's refetch) published the same refused batch."""
        files = list(self.dir.glob("*/*.parquet"))
        if not files:
            return None
        dest = self.dir / "blocked" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        for f in files:
            (dest / f.parent.name).mkdir(parents=True, exist_ok=True)
            move_into_place(f, dest / f.parent.name / f.name)
        self.conn.execute("DELETE FROM fetch_log WHERE market=?", (self.market,))
        for old in sorted((self.dir / "blocked").iterdir())[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
        return dest
