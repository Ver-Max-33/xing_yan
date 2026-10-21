"""设置 → 数据档 switched while that market's update and scan run (review H, 2026-10-13): before, the run went on — its scan in
the other profile's data, its state written there, the profile it started on left at "updating". Now the switch waits."""
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tests.test_daily import FakePipeline
from tests.test_l3_preview import TUNED
from wb.api.app import create_app
from wb.config import get_paths
from wb.devdata import write
from wb.jobs.daily import Daily, wait_until
from wb.workspace.rules import Rules

H = {"X-WB-Token": "t"}
release = threading.Event()


class Paused(FakePipeline):
    def update(self, day=None, *, wait=True):
        release.wait(30)            # the update is under way when the profile is switched
        return super().update(day, wait=wait)


def test_the_profile_of_a_market_whose_update_runs_is_not_switched_under_it(tmp_path):
    for profile in ("test", "prod"):
        write(get_paths(profile, tmp_path), 320)
        Rules(get_paths(profile, tmp_path)).save(TUNED)
    Rules(get_paths("test", tmp_path)).set_state("above_ma", enabled=True)
    app = create_app(tmp_path, token="t")
    d = app.state.daily = Daily(app.state.ctx.paths, pipeline=lambda p, m: Paused(p, m), allow_demo=True)
    c = TestClient(app)
    assert d.run_now("CN", datetime(2026, 10, 12, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")))   # after the close: it fetches
    assert wait_until(lambda: d.running("CN"))
    r = c.put("/api/settings", json={"key": "profile.CN", "value": "prod"}, headers=H)
    assert r.status_code == 409 and "做完" in r.json()["detail"], r.text
    assert c.put("/api/settings", json={"key": "profile.JP", "value": "prod"}, headers=H).status_code == 200   # the other market: free
    release.set()
    assert wait_until(lambda: not d.running("CN"))
    assert c.put("/api/settings", json={"key": "profile.CN", "value": "prod"}, headers=H).status_code == 200
