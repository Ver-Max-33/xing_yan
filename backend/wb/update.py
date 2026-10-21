"""检查更新: the newest published release of the app's own GitHub repository against the version running. The
repository is private, so reading its releases takes a token of yours (secret github_token: a fine-grained token for
this repository only, Contents read-only), sent to api.github.com and nowhere else; without one nothing is asked. A draft
release (the build workflow makes one on a version tag) counts once you publish it.

下载并安装 (spec §3.3, 1.51): in the installed app, on your click, the release's installer for this system is downloaded
(the same token; the file itself comes from GitHub's storage by a signed link, which never sees the token) and checked
against the release's SHA256SUMS.txt; the app (desktop/main.js) then closes and runs it. Nothing is run here."""
import hashlib
import logging
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from wb import __version__
from wb.files import move_into_place
from wb.secrets import get_secret

REPO = "Ver-Max-33/xingyan"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
KEEP = 6 * 3600   # a check stands this long (seconds); asking again (force) checks now
NOTES = 2000      # the release notes, at most this many characters
# the installer of each system, as the build workflow names it (desktop/electron-builder.yml artifactName), and the file
# of the release that lists every file's SHA-256
INSTALLER = {"win32": "xingyan-{v}-win-x64.exe", "darwin": "xingyan-{v}-mac-arm64.dmg"}
SUMS = "SHA256SUMS.txt"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "xingyan-workbench"}
IDLE = {"state": "idle", "version": None, "done": 0, "total": 0, "file": None, "error": None}

log = logging.getLogger(__name__)
_lock = threading.Lock()
_kept: tuple[float, dict] | None = None
_dl_lock = threading.Lock()
_dl: dict = dict(IDLE)   # the download: idle → downloading → ready (file) or error


def version(text: str | None) -> tuple[int, int, int] | None:
    """1.2.3 or v1.2.3; anything else (a pre-release, a name) is no version."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def installable() -> bool:
    """The installed app (a frozen backend) on a system there is an installer for; run from the repository, a new
    version is a git pull."""
    return bool(getattr(sys, "frozen", False)) and sys.platform in INSTALLER


def update_dir() -> Path:
    """Where the installer is downloaded: the folder the app gave (WB_UPDATE_DIR, under the system's temporary folder;
    the app clears it when it starts)."""
    return Path(os.environ.get("WB_UPDATE_DIR") or Path(tempfile.gettempdir()) / "xingyan-updates")


def check(force: bool = False, transport: httpx.BaseTransport | None = None) -> dict:
    """{current, token (whether one is set), installable, latest, newer, url, name, published_at, notes, error,
    checked_at}."""
    global _kept
    with _lock:
        if not force and _kept is not None and time.monotonic() - _kept[0] < KEEP:
            return _kept[1]
        out = _ask(transport)
        _kept = (time.monotonic(), out) if out["token"] else None   # without a token nothing was asked: nothing to keep
        return out


def _ask(transport: httpx.BaseTransport | None) -> dict:
    base = {"current": __version__, "token": False, "installable": installable(), "latest": None, "newer": False,
            "url": None, "name": None, "published_at": None, "notes": None, "error": None,
            "checked_at": datetime.now().isoformat(timespec="seconds")}
    token = get_secret("github_token")
    if not token:
        return base
    base["token"] = True
    try:
        with httpx.Client(transport=transport, timeout=10) as client:
            r = client.get(LATEST, headers={**HEADERS, "Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        return {**base, "error": f"连不上 GitHub（{type(e).__name__}）"}
    if refused := _refusal(r.status_code):
        return {**base, "error": refused}
    rel = r.json()
    latest, current = version(rel.get("tag_name")), version(__version__)
    if latest is None:
        return {**base, "error": f"最新版本的标签 {rel.get('tag_name')!r} 不是 1.2.3 这样的版本号"}
    return {**base, "latest": ".".join(map(str, latest)), "newer": current is not None and latest > current,
            "url": rel.get("html_url"), "name": rel.get("name"), "published_at": rel.get("published_at"),
            "notes": (rel.get("body") or "")[:NOTES] or None}


def _refusal(status: int) -> str | None:
    """What an answer of GitHub's other than 200 means, in words."""
    if status == 200:
        return None
    return {401: "GitHub 令牌无效或过期：在设置里换一个",
            404: "还没有发布的版本（草稿版不算），或者这个令牌读不到这个仓库",
            403: "GitHub 暂时拒绝了请求（太频繁，或令牌权限不够），稍后再试",
            429: "GitHub 暂时拒绝了请求（太频繁，或令牌权限不够），稍后再试"}.get(status, f"GitHub 返回 {status}")


class _Refused(Exception):
    """Why there is nothing to install, in words."""


def _file_refusal(status: int, name: str) -> str | None:
    """A release file GitHub would not hand over: the token's trouble said as for the release, anything else by name."""
    return None if status == 200 else _refusal(status) if status in (401, 403, 429) else f"GitHub 没给出 {name}（{status}）"


def download_state() -> dict:
    """{state: idle | downloading | ready | error, version, done, total (bytes), file (when ready), error}."""
    with _dl_lock:
        return dict(_dl)


def download(transport: httpx.BaseTransport | None = None) -> dict:
    """Start downloading the newest release's installer for this system — one download at a time; a click while one
    runs is that one."""
    with _dl_lock:
        if _dl["state"] != "downloading":
            _dl.update(IDLE, state="downloading")
            threading.Thread(target=_download, args=(transport,), name="update-download", daemon=True).start()
        return dict(_dl)


def _download(transport: httpx.BaseTransport | None) -> None:
    try:
        version, file = _fetch(transport)
        out = {"state": "ready", "version": version, "file": str(file)}
    except _Refused as e:
        out = {"state": "error", "error": str(e)}
    except httpx.HTTPError as e:
        out = {"state": "error", "error": f"下载断了（{type(e).__name__}）：再点一次"}
    except OSError as e:
        out = {"state": "error", "error": f"安装包存不下（{e.strerror or e}）"}
    except Exception as e:   # whatever else: said, never left "downloading"
        log.exception("update download failed")
        out = {"state": "error", "error": f"下载出错（{type(e).__name__}：{e}）"}
    with _dl_lock:
        _dl.update(out)


def _fetch(transport: httpx.BaseTransport | None) -> tuple[str, Path]:
    """The newest release's installer for this system, downloaded and checked: (its version, the file)."""
    token = get_secret("github_token")
    if not token:
        raise _Refused("还没配 GitHub 令牌")
    if sys.platform not in INSTALLER:
        raise _Refused("这个系统没有安装包")
    # follow_redirects: an asset answers with its storage's signed link; httpx drops the token for another host
    with httpx.Client(transport=transport, timeout=30, follow_redirects=True,
                      headers={**HEADERS, "Authorization": f"Bearer {token}"}) as client:
        r = client.get(LATEST)
        if refused := _refusal(r.status_code):
            raise _Refused(refused)
        rel = r.json()
        latest, current = version(rel.get("tag_name")), version(__version__)
        if latest is None or current is None or latest <= current:
            raise _Refused(f"GitHub 上没有比 {__version__} 新的版本")
        v = ".".join(map(str, latest))
        name = INSTALLER[sys.platform].format(v=v)
        assets = {a.get("name"): a for a in rel.get("assets") or []}
        if name not in assets:
            raise _Refused(f"{v} 没有这个系统的安装包（{name}）")
        if SUMS not in assets:
            raise _Refused(f"{v} 没有 {SUMS}，安装包核对不了，没有下载")
        sums = client.get(assets[SUMS]["url"], headers={"Accept": "application/octet-stream"})
        if refused := _file_refusal(sums.status_code, SUMS):
            raise _Refused(refused)
        want = next((line.split()[0].lower() for line in sums.text.splitlines()
                     if len(line.split()) == 2 and line.split()[1].lstrip("*") == name), None)
        if want is None:
            raise _Refused(f"{SUMS} 里没有 {name}，安装包核对不了，没有下载")
        folder = update_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():   # an earlier download, used or given up
            try:
                old.unlink()
            except OSError:
                pass
        with _dl_lock:
            _dl.update(version=v, total=int(assets[name].get("size") or 0))
        part, digest = folder / f"{name}.part", hashlib.sha256()
        with client.stream("GET", assets[name]["url"], headers={"Accept": "application/octet-stream"}) as got:
            if refused := _file_refusal(got.status_code, name):
                raise _Refused(refused)
            with part.open("wb") as f:
                for chunk in got.iter_bytes(1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
                    with _dl_lock:
                        _dl["done"] += len(chunk)
        if digest.hexdigest() != want:
            part.unlink(missing_ok=True)
            raise _Refused(f"下载的安装包和 {SUMS} 对不上，已删掉、没有安装：再点一次")
        file = folder / name
        move_into_place(part, file)
        return v, file
