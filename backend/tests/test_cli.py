import os
import sys
from datetime import date

import pytest

from tests.test_pipeline import DAYS, FakeJP, pipe
from wb.cli import build_parser, main


def test_parser_accepts_documented_commands():
    p = build_parser()
    a = p.parse_args(["--profile", "prod", "backfill", "--market", "JP", "--start", "2016-10-01"])
    assert a.start == date(2016, 10, 1) and a.profile == "prod"
    assert p.parse_args(["update", "--market", "CN", "--no-wait"]).no_wait


def test_cli_snapshots_and_backup(paths, capsys):
    p = pipe(paths, FakeJP())
    p.backfill(DAYS[0], DAYS[-1])
    assert main(["--profile", "prod", "--data-dir", str(paths.root), "snapshots", "--market", "JP"]) == 0
    assert "as_of=2026-10-02" in capsys.readouterr().out
    assert main(["--data-dir", str(paths.root), "backup"]) == 0


def test_probe_runs_once_per_day(paths, monkeypatch):
    import wb.data.capability as C
    from wb.data.providers.base import Probe

    calls = []
    monkeypatch.setattr(C, "probe_market", lambda conn, profile, market: calls.append(market) or
                        C.save_probes(conn, "x", market, [Probe("bars_1d", True)]))
    C.probe_if_stale(paths, "JP")
    C.probe_if_stale(paths, "JP")
    assert calls == ["JP"]


def test_crosscheck_summary_counts_per_field(capsys):
    import polars as pl

    from wb.cli import _report_crosscheck

    res = pl.DataFrame({"reference": ["r"] * 4, "listing_id": ["CN:600519"] * 4,
                        "field": ["open", "open", "close", "close"], "ok": [True, True, True, False]})
    assert _report_crosscheck(res) is False
    out = capsys.readouterr().out
    assert "│ r         ┆ close ┆ 2   ┆ 1   │" in out and "│ r         ┆ open  ┆ 2   ┆ 2   │" in out


def test_errors_print_on_any_windows_console(paths, monkeypatch):
    # stderr carries Chinese text (订正, issue details); a legacy code page must not crash the CLI
    import io
    import sys

    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    main(["--data-dir", str(paths.root), "snapshots", "--market", "JP"])
    assert sys.stderr.encoding.lower().replace("-", "") == "utf8"


def test_market_commands_follow_the_workspace_profile(paths, capsys, monkeypatch):
    # The app's 设置 → 数据档 picks a profile per market; `wb update --market CN` without --profile
    # must use the same one (an explicit --profile or WB_PROFILE still wins).
    from wb.workspace.store import Workspace

    monkeypatch.delenv("WB_PROFILE", raising=False)
    Workspace(paths).set_setting("profile.CN", "prod")
    root = str(paths.root)
    assert main(["--data-dir", root, "snapshots", "--market", "CN"]) == 0
    assert main(["--data-dir", root, "snapshots", "--market", "JP"]) == 0
    assert main(["--profile", "test", "--data-dir", root, "snapshots", "--market", "CN"]) == 0
    out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[profile:")]
    assert [l.split("]")[0] for l in out] == ["[profile: prod", "[profile: test", "[profile: test"]


def test_the_app_says_each_markets_profile_when_it_starts(paths, capsys, monkeypatch):
    # `wb serve` (the app) has no market: it said "[profile: test]" while A 股 ran on the formal profile
    import uvicorn

    from wb.workspace.store import Workspace

    monkeypatch.delenv("WB_PROFILE", raising=False)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: None)
    Workspace(paths).set_setting("profile.CN", "prod")
    assert main(["--data-dir", str(paths.root), "serve", "--token", "t"]) == 0
    assert "[profile: CN prod · JP test]" in capsys.readouterr().out


def test_screen_runs_a_formula_over_the_market(paths, capsys):
    from wb.devdata import write

    write(paths, 320)
    root = str(paths.root)
    assert main(["--data-dir", root, "screen", "--market", "JP", "--formula", "C > MA(C, 20) OR C <= MA(C, 20)"]) == 0
    out = capsys.readouterr().out
    assert "3 只命中，0 只数据不足" in out and "JP:7203" in out
    assert main(["--data-dir", root, "screen", "--market", "JP", "--formula", "C > 5"]) == 2
    assert "RAW_C" in capsys.readouterr().err


def test_structures_computes_and_caches_a_market(paths, capsys):
    from wb.devdata import write

    write(paths, 320)
    root = str(paths.root)
    assert main(["--data-dir", root, "structures", "--market", "JP", "--workers", "1"]) == 0
    out = capsys.readouterr().out
    assert "BOX" in out and "boxes" in out and "BOX_DERIVED" in out
    assert list((paths.profile_dir / "derived").glob("JP-*/l2/BOX-*"))


def test_serve_backs_up_the_workspace_on_the_first_start_of_the_day(paths, monkeypatch, capsys):
    # spec §4.3: labels, drawings and watchlists cannot be re-downloaded — copied on the first start
    # of each day (the desktop app starts the backend with `wb serve`)
    from datetime import date

    import uvicorn

    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: None)
    assert main(["--data-dir", str(paths.root), "serve", "--token", "t"]) == 0
    assert (paths.backup_dir / f"shared-{date.today():%Y%m%d}.sqlite").exists()


def test_a_blocked_staging_batch_can_be_discarded(paths, capsys):
    # a quality-blocked batch stays in staging for inspection; without a way to discard it every later
    # update would hit the same block
    from datetime import date

    import polars as pl

    from wb.data.store import Staging
    from wb.datasets import conform

    sg = Staging(paths, "CN")
    sg.put("tushare", "bars_1d", "20260105", {"bars_1d": conform(pl.DataFrame(
        {"listing_id": ["CN:600000"], "date": [date(2026, 1, 5)], "close": [10.0]}), "bars_1d")})
    root = str(paths.root)
    assert main(["--data-dir", root, "staging", "show", "--market", "CN"]) == 0
    assert "1 个已抓取未发布的单元" in capsys.readouterr().out
    assert main(["--data-dir", root, "staging", "clear", "--market", "CN"]) == 0
    assert Staging(paths, "CN").pending() == 0 and not list((paths.market_dir("CN") / "staging").glob("*/*.parquet"))


def test_screen_on_a_day_without_bars_says_so(paths, capsys):
    from wb.devdata import write

    write(paths, 320)
    assert main(["--data-dir", str(paths.root), "screen", "--market", "CN", "--date", "2000-01-01",
                 "--formula", "C > O"]) == 0
    assert "不是交易日" in capsys.readouterr().out


def test_rule_commands_list_check_save_and_run(paths, capsys, tmp_path):
    from tests.test_l3_run import ABOVE_MA
    from wb.devdata import write

    write(paths, 320)
    root = str(paths.root)
    assert main(["--data-dir", root, "rule", "list"]) == 0
    assert "long13 " in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: x\nname: y\nmarkets: [CN]\nstages:\n  - {id: a, kind: window}\nsignal_at: a.end\n", encoding="utf-8")
    assert main(["--data-dir", root, "rule", "check", str(bad)]) == 2
    assert "规则有误" in capsys.readouterr().err
    good = tmp_path / "above_ma.yaml"
    good.write_text(ABOVE_MA, encoding="utf-8")
    assert main(["--data-dir", root, "rule", "run", str(good), "--market", "CN", "--explain", "CN:000001"]) == 0
    out = capsys.readouterr().out
    assert "股票池 2 只" in out and "CN:000001 " in out and "运行记录" in out
    assert main(["--data-dir", root, "rule", "save", str(good)]) == 0
    assert "above_ma 版本 1（新版本）" in capsys.readouterr().out
    assert main(["--data-dir", root, "rule", "run", "above_ma", "--market", "JP", "--from", "2000-01-01"]) == 0
    assert "个信号" in capsys.readouterr().out


def test_rule_run_lists_the_listings_it_could_not_judge(paths, capsys, tmp_path):
    from tests.test_l3_run import ABOVE_MA
    from wb.devdata import write

    write(paths, 320)
    f = tmp_path / "long.yaml"
    f.write_text(ABOVE_MA.replace("MA(C, 20)", "MA(C, 500)"), encoding="utf-8")   # longer than the history
    assert main(["--data-dir", str(paths.root), "rule", "run", str(f), "--market", "CN"]) == 0
    out = capsys.readouterr().out
    assert "无法判断 2" in out
    assert any("CN:000001" in line and "无法判断" in line for line in out.splitlines()[1:])


def test_quitting_never_waits_on_an_open_live_page(paths, monkeypatch):
    # an open 盯盘 page holds its feed (SSE) open: without a bound, a stop request waits for the page forever
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: seen.update(vars(self.config)))
    assert main(["--data-dir", str(paths.root), "serve", "--token", "t"]) == 0
    assert 0 < seen["timeout_graceful_shutdown"] <= 5


def test_the_server_stops_by_itself_once_the_app_that_started_it_is_gone(paths, tmp_path):
    # the desktop app holds the backend's stdin (--exit-with-stdin): when the app is gone — quit, crashed or killed —
    # the pipe closes and the backend stops instead of running on with its scheduler
    import os
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    from wb.devdata import write
    write(paths, 320)                                   # demo data: the scheduler neither fetches nor probes
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("WB_SECRET_")}
    env |= {"WB_TOKEN": "t", "WB_ENV_FILE": str(tmp_path / "none.env"), "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    p = subprocess.Popen([sys.executable, "-c", "import sys; from wb.cli import main; sys.exit(main(sys.argv[1:]))",
                          "--data-dir", str(paths.root), "serve", "--port", str(port), "--exit-with-stdin"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        end = time.time() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except OSError:
                assert p.poll() is None and time.time() < end, "the server never came up"
                time.sleep(0.2)
        p.stdin.close()                                 # the app is gone
        assert p.wait(timeout=20) == 0
    finally:
        if p.poll() is None:
            p.kill()


def test_the_backend_notices_the_apps_process_ending():
    # WB_PARENT_PID: the desktop app's process. On Windows its stdin pipe did not always tell a killed app (2026-10-10)
    import subprocess
    import threading
    import time

    from wb.cli import _wait_for_exit
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    threading.Thread(target=p.wait, daemon=True).start()   # reaped as the app's own parent would
    t = time.time()
    assert _wait_for_exit(p.pid) is True and time.time() - t < 10
    if sys.platform != "win32":
        assert _wait_for_exit(1) is False                    # someone else's process: not ours to watch


@pytest.mark.skipif(sys.platform != "win32", reason="Windows: there a process outlives its parent")
def test_on_windows_the_backends_own_processes_end_with_it(tmp_path):
    import subprocess
    import time
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    out = tmp_path / "pid.txt"
    code = ("import os, subprocess, sys; from wb.cli import _end_with_me; _end_with_me(); "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
            f"open(r'{out}', 'w').write(str(p.pid)); os._exit(0)")   # a worker running, the backend gone
    subprocess.run([sys.executable, "-c", code], cwd=backend, env={**os.environ, "PYTHONPATH": str(backend)}, timeout=60, check=True)
    pid = int(out.read_text(encoding="utf-8"))
    time.sleep(2)
    left = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    assert str(pid) not in left
