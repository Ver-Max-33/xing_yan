"""The structure computation and a scan, timed, where the app runs (the Windows check, .github/workflows/windows-check.yml):

    python packaging/scan_check.py structures BACKEND_EXE DATA_DIR WORKERS [MINUTES]   # `wb structures` over CN
    python packaging/scan_check.py scan BACKEND_EXE DATA_DIR [MINUTES]                 # one scan as the 扫描 button runs it
    python packaging/scan_check.py update BACKEND_EXE DATA_DIR                         # 检查更新, the token in the keychain

Every 5 s: how far it is (a scan: which rule, which step, how many chunks), the backend's processes (how many, their
memory) and the memory the machine has left. Past MINUTES (default 20) it counts as hung. A check that fails prints what
each backend process wrote (packaging/wb_backend.py with WB_STACKS_DIR: a crash's stacks, a worker's native errors,
every thread's stack after 9 minutes) and, on Windows, the crashes the system logged."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

RULES = ["long13", "vol_burst", "tpl_shang_pin", "tpl_tian_pin", "tpl_xia_heng_shang", "tpl_xia_pin_turn",
         "tpl_xia_po_hui_xiang", "tpl_ma_bunch"]   # the rules a scan usually runs
PORT, TOKEN = 8765, "check-" + os.urandom(8).hex()


def call(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 headers={"x-wb-token": TOKEN, "content-type": "application/json"},
                                 data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def processes(exe: str) -> str:
    """The backend's processes: how many and their memory (Windows: private bytes), and the machine's free memory."""
    if os.name == "nt":
        ps = ("@{p=@(Get-Process NAME -EA 0 | % { $_.PrivateMemorySize64 }); c=@(Get-Process NAME -EA 0 | % { $_.PriorityClass.ToString() });"
              " f=(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory} | ConvertTo-Json -Compress").replace("NAME", Path(exe).stem)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8")
        try:
            d = json.loads(r.stdout)
        except ValueError:   # never the check's own failure: say what PowerShell said
            return f"（读不到进程：{(r.stderr or r.stdout).strip()[:200]}）"
        return f"{len(d['p'])} 个进程 {sum(d['p']) / 2**30:.1f} GB（{'/'.join(sorted(set(d['c'])))}），机器剩 {d['f'] / 2**20:.1f} GB"
    rss = [int(r.split()[0]) for r in subprocess.run(["ps", "-A", "-o", "rss=,comm="], capture_output=True, text=True).stdout.splitlines()
           if r.split(maxsplit=1)[1:] == [str(Path(exe).resolve())]]
    return f"{len(rss)} 个进程 {sum(rss) / 2**20:.1f} GB"


def start(cmd: list[str]) -> tuple[subprocess.Popen, Path]:
    if os.name == "nt":   # the runner's steps run below normal: from normal, what the backend shows is its own doing
        import ctypes

        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x20)   # NORMAL_PRIORITY_CLASS
    stacks = Path(tempfile.mkdtemp(prefix="wb-stacks-"))
    env = {**os.environ, "WB_TOKEN": TOKEN, "WB_STACKS_EVERY": "540", "WB_STACKS_DIR": str(stacks),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}   # its output goes to a file, not a console
    log = open(stacks / "backend.log", "w", encoding="utf-8")
    return subprocess.Popen([str(Path(cmd[0]).resolve()), *cmd[1:]], env=env, stdout=log, stderr=log), stacks


def stop(proc: subprocess.Popen, stacks: Path, failed: bool) -> None:
    """Stops the backend; when the check failed, what each process wrote (a crash's stacks, a worker's last words, the
    stacks after 9 minutes) and, on Windows, the crashes the system logged."""
    if os.name == "nt":   # the pool's processes with it
        subprocess.run(["taskkill", "/pid", str(proc.pid), "/T", "/F"], capture_output=True)
    proc.kill()
    proc.wait()
    if failed:
        for f in sorted(stacks.glob("stacks-*.txt")):
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                print(f"==== {f.name}\n{text[-8000:]}")
        if os.name == "nt":
            ps = ("Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-2)} -EA 0 | "
                  "? { $_.Message -like '*wb-backend*' } | select -First 6 | % { \"--- $($_.TimeCreated) $($_.ProviderName) $($_.Id)`n$($_.Message)\" }")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, encoding="utf-8", errors="replace")
            print("==== 系统记下的 wb-backend 崩溃\n" + (r.stdout.strip() or r.stderr.strip() or "（没有）"))
    lines = (stacks / "backend.log").read_text(encoding="utf-8", errors="replace").splitlines()
    print("==== 后端输出（最后 40 行）\n" + "\n".join(lines[-40:]), flush=True)


def structures(exe: str, data: str, workers: str, minutes: float = 20) -> int:
    proc, stacks = start([exe, "--data-dir", data, "structures", "--market", "CN", "--workers", workers])
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < minutes * 60:
        print(f"{time.time() - t0:5.0f}s  {processes(exe)}", flush=True)
        time.sleep(5)
    hung = proc.poll() is None
    print(f"{workers} 个进程算全市场结构：" + (f"{minutes:.0f} 分钟没算完" if hung else f"{time.time() - t0:.0f} 秒，退出码 {proc.returncode}"))
    failed = hung or bool(proc.returncode)
    stop(proc, stacks, failed)
    return 1 if failed else 0


def scan(exe: str, data: str, minutes: float = 20) -> int:
    proc, stacks = start([exe, "--data-dir", data, "serve", "--port", str(PORT)])
    hung, status = False, None
    try:
        for _ in range(240):
            try:
                call("/api/health")
                break
            except OSError:
                time.sleep(0.5)
        for r in RULES:
            call(f"/api/rules/{r}/state", "PUT", {"enabled": True})
        t0 = time.time()
        print("开始：", call("/api/jobs/CN/run", "POST"), flush=True)
        while True:
            el = time.time() - t0
            cn = next(m for m in call("/api/jobs")["markets"] if m["market"] == "CN")
            p = cn.get("progress") or {}
            at = f"{p.get('i')}/{p.get('n')} {p.get('rule')}：{p.get('phase')} {p.get('done')}/{p.get('total')}" if p else "—"
            print(f"{el:5.0f}s  {at}  ·  {processes(exe)}", flush=True)
            if not cn["running"] and el > 3:
                status = cn.get("status")
                print(f"扫描结束：{el:.0f} 秒，{status} {cn.get('message') or ''}")
                for later in (30, 60):   # is the memory let go once the scan is over?
                    time.sleep(30)
                    print(f"扫完 {later} 秒：{processes(exe)}", flush=True)
                break
            if el > minutes * 60:
                hung = True
                print(f"{minutes:.0f} 分钟没扫完")
                break
            time.sleep(5)
    finally:
        stop(proc, stacks, hung or status != "done")
        log = Path(data) / "logs" / "backend.log"
        print("==== 应用日志 logs/backend.log（最后 15 行）\n" + ("\n".join(log.read_text(encoding="utf-8").splitlines()[-15:])
                                                          if log.exists() else "（没有这个文件）"), flush=True)
    return 0 if status == "done" else 1


def update(exe: str, data: str) -> int:
    """检查更新 as the app does it on this machine: the token kept in the system's keychain (Windows: Credential Manager)
    through the settings' own endpoint, then the newest published release read from GitHub with it. The token is the
    workflow's own (CHECK_GITHUB_TOKEN, read-only), taken out of the environment before the backend starts; never printed."""
    token = os.environ.pop("CHECK_GITHUB_TOKEN", "")
    proc, stacks = start([exe, "--data-dir", data, "serve", "--port", str(PORT)])
    ok = False
    try:
        for _ in range(240):
            try:
                call("/api/health")
                break
            except OSError:
                time.sleep(0.5)
        keys = ("current", "token", "latest", "newer", "url", "error")
        print("没有令牌时：", {k: call("/api/update?force=true")[k] for k in keys})
        where = call("/api/secrets/github_token", "PUT", {"value": token})["github_token"] if token else None
        print("令牌存到：", where)
        u = call("/api/update?force=true")
        print("检查更新：", {k: u[k] for k in keys})
        ok = where == "keychain" and u["token"] and u["error"] is None and u["latest"] is not None
    finally:
        stop(proc, stacks, not ok)
    print("检查更新正常" if ok else "检查更新不正常")
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):   # a Windows runner's pipe is cp1252: the lines are Chinese
        stream.reconfigure(encoding="utf-8", errors="replace")
    mode, exe, data, *rest = sys.argv[1:]
    if mode == "update":
        sys.exit(update(exe, data))
    if mode == "structures":
        sys.exit(structures(exe, data, rest[0], *map(float, rest[1:])))
    sys.exit(scan(exe, data, *map(float, rest)))
