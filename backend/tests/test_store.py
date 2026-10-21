from datetime import date

import polars as pl

from wb.data.quality import Issue
from wb.data.store import Staging, Store
from wb.datasets import conform


def bars(rows):
    return conform(pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "date": pl.Date, "close": pl.Float64},
                                orient="row"), "bars_1d")


def test_publish_partitions_by_year_and_reads_back(paths):
    st = Store(paths, "JP")
    snap = st.publish({"bars_1d": bars([("JP:7203", date(2025, 12, 30), 1.0),
                                        ("JP:7203", date(2026, 1, 5), 2.0)])}, as_of=date(2026, 1, 5))
    assert sorted(snap.manifest["bars_1d"]) == ["year=2025", "year=2026"]
    assert st.read("bars_1d")["close"].to_list() == [1.0, 2.0]
    assert st.latest().id == snap.id


def test_upsert_new_rows_win_and_old_snapshot_unchanged(paths):
    st = Store(paths, "JP")
    s1 = st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 2.0)])}, as_of=date(2026, 1, 5))
    s2 = st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 2.5),
                                      ("JP:7203", date(2026, 1, 6), 3.0)])}, as_of=date(2026, 1, 6))
    assert st.read("bars_1d", s2)["close"].to_list() == [2.5, 3.0]
    assert st.read("bars_1d", s1)["close"].to_list() == [2.0]  # snapshots are immutable
    assert s2.parent_id == s1.id


def test_reimport_identical_data_is_noop(paths):
    st = Store(paths, "JP")
    df = bars([("JP:130A", date(2026, 1, 5), 900.0)])
    s1 = st.publish({"bars_1d": df}, as_of=date(2026, 1, 5))
    s2 = st.publish({"bars_1d": df}, as_of=date(2026, 1, 5))
    assert s1.id == s2.id
    assert st.read("bars_1d").height == 1
    assert len(st.history()) == 1


def test_older_backfill_never_moves_as_of_back(paths):
    # Backfilling older history after newer data exists: "data as of" stays the newest day, and
    # re-importing that older range unchanged is still a no-op.
    st = Store(paths, "JP")
    st.publish({"bars_1d": bars([("JP:130A", date(2026, 1, 5), 900.0)])}, as_of=date(2026, 1, 5))
    old = bars([("JP:130A", date(2025, 12, 30), 880.0)])
    s2 = st.publish({"bars_1d": old}, as_of=date(2025, 12, 30))
    assert s2.as_of == date(2026, 1, 5)
    assert st.publish({"bars_1d": old}, as_of=date(2025, 12, 30)).id == s2.id
    assert len(st.history()) == 2


def test_issues_recorded_with_snapshot(paths):
    st = Store(paths, "CN")
    snap = st.publish({"bars_1d": bars([("CN:600519", date(2026, 1, 5), 1.0)])}, as_of=date(2026, 1, 5),
                      issues={"bars_1d": [Issue("ohlc", "row", "bad", "CN:600519", date(2026, 1, 5))]})
    row = st.conn.execute("SELECT * FROM quality_issue WHERE snapshot_id=?", (snap.id,)).fetchone()
    assert row["check_name"] == "ohlc" and row["date"] == "2026-01-05"


def test_gc_keeps_referenced_files(paths):
    st = Store(paths, "JP")
    st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.0)])}, as_of=date(2026, 1, 5))
    st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.5)])}, as_of=date(2026, 1, 5))
    assert st.gc(keep=1) == 1
    assert st.read("bars_1d")["close"].to_list() == [1.5]


def test_staging_resume_and_dedupe(paths):
    sg = Staging(paths, "JP")
    assert not sg.done("yfinance", "bars_1d", "7203.T")
    sg.put("yfinance", "bars_1d", "7203.T", {"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.0)])})
    sg.put("yfinance", "bars_1d", "^N225", {"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.1)])})
    assert sg.done("yfinance", "bars_1d", "7203.T")
    got = sg.collect("bars_1d")
    assert got.height == 1 and got["close"][0] == 1.1  # later file wins on the same key
    sg.clear()
    assert not sg.done("yfinance", "bars_1d", "7203.T") and sg.collect("bars_1d").height == 0


def test_store_read_empty_has_schema(paths):
    assert Store(paths, "CN").read("calendar").columns == ["date", "is_open", "source"]


def test_gc_tolerates_locked_files(paths, monkeypatch):
    # Windows refuses to delete a file another process has open; gc must skip it, not crash.
    st = Store(paths, "JP")
    st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.0)])}, as_of=date(2026, 1, 5))
    st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.5)])}, as_of=date(2026, 1, 5))
    from pathlib import Path

    def locked(self, *a, **k):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "unlink", locked)
    assert st.gc(keep=1) == 0


def test_latest_is_the_last_published_even_if_the_clock_went_back(paths):
    st = Store(paths, "JP")
    a = st.publish({"bars_1d": bars([("JP:130A", date(2026, 1, 5), 900.0)])}, as_of=date(2026, 1, 5))
    st.conn.execute("UPDATE snapshot SET created_at='2099-01-01T00:00:00' WHERE id=?", (a.id,))
    b = st.publish({"bars_1d": bars([("JP:130A", date(2026, 1, 6), 910.0)])}, as_of=date(2026, 1, 6))
    assert st.latest().id == b.id
    assert [s.id for s in st.history()] == [a.id, b.id]  # gc keeps "the newest" by this order


def test_publish_refuses_to_build_on_a_stale_head(paths, monkeypatch):
    # two writers (the app and a scheduled CLI run) must not silently drop each other's snapshot
    import pytest

    mine, other = Store(paths, "JP"), Store(paths, "JP")
    mine.publish({"bars_1d": bars([("JP:130A", date(2026, 1, 5), 900.0)])}, as_of=date(2026, 1, 5))
    seen = mine.latest()
    other.publish({"bars_1d": bars([("JP:130A", date(2026, 1, 6), 910.0)])}, as_of=date(2026, 1, 6))
    monkeypatch.setattr(mine, "latest", lambda: seen)  # `mine` read the head before `other` wrote
    with pytest.raises(RuntimeError, match="published meanwhile"):
        mine.publish({"bars_1d": bars([("JP:130A", date(2026, 1, 7), 920.0)])}, as_of=date(2026, 1, 7))
    assert other.latest().as_of == date(2026, 1, 6)


def test_gc_drops_derived_results_of_old_snapshots(paths):
    st = Store(paths, "JP")
    old = st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.0)])}, as_of=date(2026, 1, 5))
    new = st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.5)])}, as_of=date(2026, 1, 5))
    for s in (old, new):
        (paths.profile_dir / "derived" / s.id / "l2" / "BOX-x").mkdir(parents=True)
    other = paths.profile_dir / "derived" / "CN-20260105-00000000"  # another market's results stay
    other.mkdir(parents=True)
    st.gc(keep=1)
    assert sorted(p.name for p in (paths.profile_dir / "derived").iterdir()) == sorted([new.id, other.name])


def test_a_file_that_does_not_read_back_as_written_is_never_published(paths, monkeypatch):
    # spec §4.3: 先写临时文件，校验 hash，再原子改名 — a short or damaged write (a full disk, a failing drive) stops the
    # publish; it never becomes part of a snapshot
    import pytest
    from pathlib import Path
    real = Path.write_bytes
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: real(self, data[:-10]))
    st = Store(paths, "JP")
    with pytest.raises(OSError, match="校验"):
        st.publish({"bars_1d": bars([("JP:7203", date(2026, 1, 5), 1.0)])}, as_of=date(2026, 1, 5))
    assert st.latest() is None and not list(paths.market_dir("JP").rglob("*.parquet"))
