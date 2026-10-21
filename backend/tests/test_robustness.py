"""The app when things go wrong (review H, 2026-10-13): a market without data, a damaged or full disk, a wrong id,
the daily copy of shared.sqlite while the app stays open."""
import sqlite3
from datetime import date

from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.backup import backup_shared
from wb.config import get_paths
from wb.db import connect
from wb.devdata import write

H = {"X-WB-Token": "t"}


def test_sending_the_briefing_of_a_market_without_data_says_so_not_demo_data(tmp_path, monkeypatch):
    # the user's Windows: A 股 with data, 日股 on 测试档 with nothing; 设置 → 通知 offers 发一份到飞书 for each market
    paths = get_paths("prod", tmp_path)
    write(get_paths("test", tmp_path), 320)                       # demo data on the test profile only
    monkeypatch.setenv("WB_SECRET_FEISHU_WEBHOOK", "https://open.feishu.cn/open-apis/bot/v2/hook/x")  # refused before sending
    c = TestClient(create_app(tmp_path, token="t"))
    assert c.put("/api/settings", json={"key": "profile.JP", "value": "prod"}, headers=H).status_code == 200   # 正式档: empty
    r = c.post("/api/brief/JP/send", headers=H)
    assert r.status_code == 409
    assert "演示" not in r.json()["detail"] and "没有数据" in r.json()["detail"], r.json()["detail"]
    r = c.post("/api/brief/CN/send", headers=H)                    # the demo data is still said as demo data
    assert r.status_code == 409 and "演示数据" in r.json()["detail"]
    del paths


def test_a_damaged_shared_database_is_not_copied_over_the_good_backups(tmp_path):
    paths = get_paths("test", tmp_path)
    conn = connect(paths.shared_db, "shared")
    conn.executemany("INSERT INTO setting VALUES (?, ?)", [(f"k{i}", "x" * 500) for i in range(400)])   # a few pages
    conn.close()
    assert backup_shared(paths, today=date(2026, 10, 12)) is not None          # a good day: copied
    root = sqlite3.connect(paths.shared_db).execute("SELECT rootpage FROM sqlite_master WHERE name='setting'").fetchone()[0]
    size = sqlite3.connect(paths.shared_db).execute("PRAGMA page_size").fetchone()[0]
    with open(paths.shared_db, "r+b") as f:                                   # a torn page in the table (as a disk error)
        f.seek((root - 1) * size)
        f.write(b"\xff" * size)
    try:
        backup_shared(paths, today=date(2026, 10, 13))
    except sqlite3.DatabaseError:
        pass
    made = sorted(p.name for p in paths.backup_dir.glob("shared-*.sqlite"))
    for name in made:   # every copy kept must be a good one
        assert sqlite3.connect(paths.backup_dir / name).execute("PRAGMA quick_check").fetchone()[0] == "ok", name
    assert made == ["shared-20261012.sqlite"]


def test_an_unknown_group_or_market_is_a_404_and_a_group_needs_a_name(tmp_path):
    c = TestClient(create_app(tmp_path, token="t"), raise_server_exceptions=False)
    assert c.post("/api/watchlist/999/items", json={"listing_id": "CN:600519"}, headers=H).status_code == 404   # was a 500
    assert c.get("/api/data/XX/quality", headers=H).status_code == 404
    assert c.get("/api/data/XX/crosscheck", headers=H).status_code == 404
    assert c.post("/api/watchlist/groups", json={"name": "  "}, headers=H).status_code == 422                 # was stored


def test_a_full_disk_is_said_as_such(tmp_path, monkeypatch):
    def full(self, name):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr("wb.workspace.store.Workspace.add_group", full)
    c = TestClient(create_app(tmp_path, token="t"), raise_server_exceptions=False)
    r = c.post("/api/watchlist/groups", json={"name": "x"}, headers=H)
    assert r.status_code == 507 and "磁盘满了" in r.json()["detail"], r.text      # was 500 "Internal Server Error"


def test_the_daily_copy_is_made_while_the_app_stays_open(tmp_path):
    from datetime import datetime, timezone

    from wb.jobs.daily import Daily
    paths = get_paths("test", tmp_path)
    connect(paths.shared_db, "shared").close()
    d = Daily(lambda m: get_paths("test", tmp_path), pipeline=lambda p, m: None)
    d.tick(datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc))      # the app opened days ago and is still running
    assert (paths.backup_dir / "shared-20261014.sqlite").exists()
