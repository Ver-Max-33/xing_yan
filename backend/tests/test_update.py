"""检查更新: the newest published release of the app's public GitHub repository (Ver-Max-33/xing_yan) against the
version running — asked without any token; the installed app downloads the new installer on your click and checks it
against the release's SHA256SUMS.txt before it is run."""
import hashlib
import json
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from wb import __version__, update

ROOT = Path(__file__).resolve().parents[2]


def github(status: int, body: dict | None = None, seen: list | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body or {})
    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(update, "_kept", None)
    monkeypatch.delenv("WB_GITHUB_API", raising=False)


def test_the_version_running_is_the_one_every_package_says():
    project = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    for pkg in ("desktop", "frontend"):
        assert json.loads((ROOT / pkg / "package.json").read_text(encoding="utf-8"))["version"] == __version__
    assert project == __version__


def test_a_newer_release_is_said_with_its_page_asked_of_the_public_repository_without_a_token():
    seen = []
    r = update.check(transport=github(200, {"tag_name": "v9.0.0", "name": "形研 v9.0.0", "html_url": "https://github.com/Ver-Max-33/xing_yan/releases/tag/v9.0.0",
                                            "published_at": "2026-10-09T08:00:00Z", "body": "改了这些"}, seen))
    assert (r["current"], r["latest"], r["newer"], r["error"]) == (__version__, "9.0.0", True, None) and "token" not in r
    assert r["url"].endswith("/v9.0.0") and r["notes"] == "改了这些" and r["published_at"] == "2026-10-09T08:00:00Z"
    [req] = seen
    assert str(req.url) == "https://api.github.com/repos/Ver-Max-33/xing_yan/releases/latest"
    assert "authorization" not in req.headers


def test_a_github_token_kept_from_before_is_never_sent(monkeypatch):
    # until 0.6.0 the releases were in a private repository read with a token of yours; one may still be kept, and
    # scoped to that repository it is no use here — nothing is sent but the question
    monkeypatch.setenv("WB_SECRET_GITHUB_TOKEN", "github_pat_old")
    seen = []
    assert update.check(transport=github(200, {"tag_name": "v9.0.0"}, seen))["newer"] is True
    assert "authorization" not in seen[0].headers


def test_the_address_can_point_at_a_stand_in(monkeypatch):
    # as WB_AI_BASE_URL does for the models: end-to-end tests point it at a closed port, so no test asks GitHub
    monkeypatch.setenv("WB_GITHUB_API", "http://127.0.0.1:9")
    seen = []
    update.check(transport=github(200, {"tag_name": "v9.0.0"}, seen))
    assert str(seen[0].url) == "http://127.0.0.1:9/repos/Ver-Max-33/xing_yan/releases/latest"


def test_the_releases_are_built_where_the_app_reads_them():
    # build.yml builds and drafts releases in one repository only; the app must ask that one (10-21: the guard still
    # named the private repository and the first build in xing_yan skipped every job)
    import yaml
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"))["jobs"]
    assert jobs["version"]["if"] == f"github.repository == '{update.REPO}'"


def test_the_same_or_an_older_release_is_no_update():
    assert update.check(transport=github(200, {"tag_name": f"v{__version__}"}))["newer"] is False
    assert update.check(force=True, transport=github(200, {"tag_name": "v0.0.1"}))["newer"] is False


@pytest.mark.parametrize(("status", "says"), [(404, "还没有发布的版本"), (403, "稍后再试"), (429, "稍后再试"), (500, "500")])
def test_what_github_refused_is_said(status, says):
    r = update.check(transport=github(status))
    assert r["newer"] is False and r["latest"] is None and says in r["error"]


def test_a_tag_that_is_no_version_and_no_connection_are_said():
    assert "不是 1.2.3" in update.check(transport=github(200, {"tag_name": "nightly"}))["error"]

    def down(request):
        raise httpx.ConnectError("no route")
    assert "连不上 GitHub" in update.check(force=True, transport=httpx.MockTransport(down))["error"]


def test_a_check_stands_six_hours_unless_asked_again():
    seen = []
    t = github(200, {"tag_name": "v9.0.0"}, seen)
    update.check(transport=t)
    update.check(transport=t)
    assert len(seen) == 1
    update.check(force=True, transport=t)
    assert len(seen) == 2


# ---- 下载并安装: the installer of the newest release for this system, checked against the release's SHA256SUMS.txt ----

API = "https://api.github.com/repos/Ver-Max-33/xing_yan"
EXE = b"MZ the installer " * 1000


def release(tag="v9.0.0", exe=EXE, sums=None, names=("xingyan-9.0.0-win-x64.exe", "xingyan-9.0.0-mac-arm64.dmg", "SHA256SUMS.txt")):
    """GitHub as the app meets it: the latest release, its assets by id (each a redirect to the file's storage), the files."""
    digest = hashlib.sha256(exe).hexdigest()
    sums = sums if sums is not None else f"{'0' * 64}  xingyan-9.0.0-mac-arm64.dmg\n{digest}  xingyan-9.0.0-win-x64.exe\n"
    body = {"name": "SHA256SUMS.txt", "bytes": sums.encode()}
    files = {n: {"bytes": exe} if n.endswith(".exe") else body if n == "SHA256SUMS.txt" else {"bytes": b"dmg"} for n in names}
    assets = [{"id": i, "name": n, "size": len(f["bytes"]), "url": f"{API}/releases/assets/{i}"} for i, (n, f) in enumerate(files.items())]
    by_id = {str(a["id"]): a["name"] for a in assets}
    seen: list[httpx.Request] = []
    fetched: list[str] = []   # the files handed out, by name

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url == f"{API}/releases/latest":
            return httpx.Response(200, json={"tag_name": tag, "assets": assets})
        if url.startswith(f"{API}/releases/assets/"):
            assert request.headers["accept"] == "application/octet-stream"
            return httpx.Response(302, headers={"location": f"https://release-assets.githubusercontent.com/{url.rsplit('/', 1)[1]}?sig=x"})
        if url.startswith("https://release-assets.githubusercontent.com/"):
            name = by_id[request.url.path.strip("/")]
            fetched.append(name)
            return httpx.Response(200, content=files[name]["bytes"])
        return httpx.Response(404)
    return httpx.MockTransport(handle), seen, fetched


def finished(timeout=5.0) -> dict:
    deadline = time.monotonic() + timeout
    while (state := update.download_state())["state"] == "downloading":
        assert time.monotonic() < deadline, "the download never ended"
        time.sleep(0.01)
    return state


@pytest.fixture
def windows_app(monkeypatch, tmp_path):
    """The installed app on Windows (a frozen backend), downloading into a folder of the test's own."""
    monkeypatch.setattr(update, "sys", SimpleNamespace(platform="win32", frozen=True))
    monkeypatch.setattr(update, "_dl", dict(update.IDLE))
    monkeypatch.setenv("WB_UPDATE_DIR", str(tmp_path / "updates"))
    return tmp_path / "updates"


def test_the_installer_is_downloaded_checked_against_the_release_sums_and_kept_where_the_app_said(windows_app):
    transport, seen, fetched = release()
    update.download(transport)
    s = finished()
    assert (s["state"], s["version"], s["error"], s["done"], s["total"]) == ("ready", "9.0.0", None, len(EXE), len(EXE))
    assert s["file"] == str(windows_app / "xingyan-9.0.0-win-x64.exe")
    assert [p.name for p in windows_app.iterdir()] == ["xingyan-9.0.0-win-x64.exe"]   # nothing half-written left
    assert (windows_app / "xingyan-9.0.0-win-x64.exe").read_bytes() == EXE
    assert all("authorization" not in r.headers for r in seen)   # a public repository: no token anywhere
    assert fetched == ["SHA256SUMS.txt", "xingyan-9.0.0-win-x64.exe"]   # this system's installer only


def test_an_installer_that_is_not_the_one_the_release_lists_is_thrown_away(windows_app):
    transport, _, _ = release(sums=f"{'1' * 64}  xingyan-9.0.0-win-x64.exe\n")
    update.download(transport)
    s = finished()
    assert s["state"] == "error" and "对不上" in s["error"] and s["file"] is None
    assert list(windows_app.iterdir()) == []


@pytest.mark.parametrize(("kwargs", "says"), [
    ({"names": ("xingyan-9.0.0-mac-arm64.dmg", "SHA256SUMS.txt")}, "xingyan-9.0.0-win-x64.exe"),   # no Windows installer
    ({"names": ("xingyan-9.0.0-win-x64.exe",)}, "SHA256SUMS.txt"),                               # nothing to check it by
    ({"sums": "0000  other.exe\n"}, "SHA256SUMS.txt 里没有"),
    ({"tag": f"v{__version__}"}, "没有比"),                                                        # not newer
])
def test_a_release_with_nothing_to_install_says_why_and_downloads_nothing(windows_app, kwargs, says):
    transport, _, fetched = release(**kwargs)
    update.download(transport)
    s = finished()
    assert s["state"] == "error" and says in s["error"]
    assert not windows_app.exists() or list(windows_app.iterdir()) == []
    assert "xingyan-9.0.0-win-x64.exe" not in fetched


def test_without_a_connection_the_download_says_so(windows_app):
    def down(request):
        raise httpx.ReadTimeout("stalled")
    update.download(httpx.MockTransport(down))
    assert "下载断了" in finished()["error"]


def test_a_release_file_github_will_not_hand_over_is_named(windows_app):
    transport, _, _ = release()

    def gone(request):
        if request.url.host == "release-assets.githubusercontent.com" and request.url.path.strip("/") == "0":
            return httpx.Response(404)   # the installer's storage link: gone
        return transport.handle_request(request)
    update.download(httpx.MockTransport(gone))
    assert finished()["error"] == "GitHub 没给出 xingyan-9.0.0-win-x64.exe（404）"


def test_a_new_download_clears_what_an_earlier_one_left(windows_app):
    windows_app.mkdir()
    (windows_app / "xingyan-8.0.0-win-x64.exe").write_bytes(b"old")
    (windows_app / "xingyan-9.0.0-win-x64.exe.part").write_bytes(b"half")
    update.download(release()[0])
    assert finished()["state"] == "ready"
    assert [p.name for p in windows_app.iterdir()] == ["xingyan-9.0.0-win-x64.exe"]


def test_one_download_at_a_time(windows_app):
    gate = threading.Event()
    transport, seen, _ = release()

    def slow(request):
        gate.wait(5)
        return transport.handle_request(request)
    update.download(httpx.MockTransport(slow))
    again = update.download(httpx.MockTransport(slow))
    assert again["state"] == "downloading"
    gate.set()
    assert finished()["state"] == "ready"
    assert sum(1 for r in seen if str(r.url).endswith("/releases/latest")) == 1


def test_only_the_installed_app_installs_and_says_so(monkeypatch):
    monkeypatch.setattr(update, "sys", SimpleNamespace(platform="win32", frozen=True))
    assert update.installable() is True
    assert update.check(transport=github(200, {"tag_name": "v9.0.0"}))["installable"] is True
    monkeypatch.setattr(update, "sys", SimpleNamespace(platform="darwin"))       # run from the repository: git pull
    assert update.installable() is False
    monkeypatch.setattr(update, "sys", SimpleNamespace(platform="linux", frozen=True))   # no installer for it
    assert update.installable() is False


def test_a_run_from_the_repository_downloads_nothing_and_the_state_is_there_to_read(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from wb.api.app import create_app
    monkeypatch.setattr(update, "_dl", dict(update.IDLE))
    monkeypatch.setenv("WB_GITHUB_API", "http://127.0.0.1:9")   # the check below asks nobody
    c, h = TestClient(create_app(tmp_path, token="t")), {"X-WB-Token": "t"}
    r = c.post("/api/update/download", headers=h)
    assert r.status_code == 409 and "git pull" in r.json()["detail"]
    assert c.get("/api/update/download", headers=h).json() == update.IDLE
    assert c.get("/api/update", headers=h).json()["installable"] is False
    assert c.get("/api/health").json()["version"] == __version__   # what an update test asks the app it opened again
