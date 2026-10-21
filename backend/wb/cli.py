"""Command line for P0 (spec §13 P0: 命令行回填与更新).

Scheduling is left to the OS until P4 adds the background loop: launchd/cron on macOS,
Task Scheduler on Windows, e.g. `wb --profile prod update --market JP` at 16:25 JST.
Exit codes: 0 ok, 2 data delayed, 3 quality check blocked publishing, 4 spot check mismatch,
5 crosscheck disagreement with the independent source.
"""
import argparse
import getpass
import os
import sys
import threading
from datetime import date

import polars as pl

from wb.backup import backup_shared
from wb.config import get_paths
from wb.data.capability import probe_if_stale, probe_market
from wb.data.pipeline import DataDelayed, Pipeline, QualityError
from wb.data.spotcheck import spotcheck
from wb.data.verify import crosscheck
from wb.data.store import Store
from wb.db import connect
from wb.markets import MARKETS
from wb.secrets import set_secret


def _date(s: str) -> date:
    return date.fromisoformat(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wb", description="Stock workbench data tools")
    p.add_argument("--profile", choices=["test", "prod"], help="data profile (default: WB_PROFILE or test)")
    p.add_argument("--data-dir", help="data root (default: WB_DATA_DIR or ~/WorkbenchData)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("secret", help="store an API token in the OS keychain")
    s.add_argument("action", choices=["set"])
    s.add_argument("name", choices=["tushare_token", "jquants_api_key"])

    s = sub.add_parser("probe", help="detect plan/tier capabilities")
    s.add_argument("--market", choices=[*MARKETS, "all"], default="all")

    s = sub.add_parser("backfill", help="download history (resumable)")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--start", type=_date, required=True)
    s.add_argument("--end", type=_date)
    s.add_argument("--datasets", help="only these, comma-separated (e.g. financials: the reports' history after the bars)")

    s = sub.add_parser("update", help="wait for today's data, fetch, publish")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--date", type=_date)
    s.add_argument("--no-wait", action="store_true", help="skip readiness polling")

    s = sub.add_parser("refetch", help="re-download a day and publish corrections")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--date", type=_date, help="default: last published trading day")

    s = sub.add_parser("snapshots", help="list snapshots")
    s.add_argument("--market", choices=MARKETS, required=True)

    s = sub.add_parser("spotcheck", help="compare stored bars with the source (P0 acceptance)")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--date", type=_date)
    s.add_argument("--seed", type=int, default=0)

    s = sub.add_parser("crosscheck", help="compare a sample with an independent source (never changes data)")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--days", type=int, default=5)
    s.add_argument("--seed", type=int, default=0)

    s = sub.add_parser("serve", help="run the local API (and the built frontend) on 127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", help="session token every /api request must carry (default: WB_TOKEN)")
    s.add_argument("--frontend", help="directory with the built frontend to serve at /")
    s.add_argument("--exit-with-stdin", action="store_true",
                   help="stop when stdin closes: the desktop app holds it, so the server never outlives the app")

    s = sub.add_parser("settings", help="show or change workspace settings (e.g. profile.CN prod)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")

    s = sub.add_parser("screen", help="evaluate a formula over the whole market on one day")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--formula", required=True, help='e.g. "C > MA(C, 20) AND VOL_MULT >= 2"')
    s.add_argument("--date", type=_date, help="default: the latest day in the snapshot")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("structures", help="compute the structure primitives over a market snapshot (cached)")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--workers", type=int, help="processes (default: one per core)")

    s = sub.add_parser("rule", help="list, check, save and run rules (YAML / JSON)")
    rs = s.add_subparsers(dest="rule_cmd", required=True)
    rs.add_parser("list", help="built-in and saved rules")
    r = rs.add_parser("show", help="print a rule as written")
    r.add_argument("rule")
    r.add_argument("--version", type=int)
    r = rs.add_parser("check", help="check a rule file")
    r.add_argument("file")
    r = rs.add_parser("save", help="save a rule file (a new version when its content changed)")
    r.add_argument("file")
    r = rs.add_parser("run", help="run a rule on one day (default: the latest) or over a range")
    r.add_argument("rule", help="a rule id, or a .yaml / .json file")
    r.add_argument("--market", choices=MARKETS, required=True)
    r.add_argument("--date", type=_date)
    r.add_argument("--from", dest="start", type=_date)
    r.add_argument("--to", dest="end", type=_date)
    r.add_argument("--listing", action="append", help="only this listing (repeatable)")
    r.add_argument("--limit", type=int, default=30)
    r.add_argument("--explain", help="print the explanation for this listing (date runs)")
    r.add_argument("--workers", type=int)

    s = sub.add_parser("devdata", help="write deterministic demo data (test profile, no network)")
    s.add_argument("--days", type=int, default=320)

    sub.add_parser("backup", help="copy shared.sqlite to backups/ (once per day)")

    s = sub.add_parser("staging", help="fetched-but-unpublished data: show it, or discard a blocked batch")
    s.add_argument("action", choices=["show", "clear"])
    s.add_argument("--market", choices=MARKETS, required=True)

    s = sub.add_parser("gc", help="delete data files no recent snapshot uses")
    s.add_argument("--market", choices=MARKETS, required=True)
    s.add_argument("--keep", type=int, default=10)
    return p


def _report_crosscheck(res) -> bool:
    """Print a per-field summary; True when everything agreed. Disagreement is reported, never fixed."""
    from wb import events

    if res.is_empty():
        print("crosscheck: no reference data")
        return True
    print(res.group_by("reference", "field").agg(n=pl.len(), ok=pl.col("ok").sum()).sort("reference", "field"))
    print(f"{res['listing_id'].n_unique()} listings had reference data")
    bad = res.filter(~res["ok"])
    if bad.height:
        print(bad.head(20))
        events.publish("data.crosscheck_mismatch", {"rows": bad.height})
    return bad.is_empty()


def _screen(paths, args) -> int:
    from wb.engine.l1.compiler import CompileError, Compiler
    from wb.engine.l1.frame import available_columns, build_frame
    from wb.engine.l1.parser import ParseError
    from wb.engine.l1.run import evaluate

    st = Store(paths, args.market)
    if st.latest() is None:
        print(f"{args.market}: no snapshot yet", file=sys.stderr)
        return 1
    comp = Compiler(args.market, available=available_columns(st))
    try:
        comp.add("hit", args.formula, want="cond")
    except (CompileError, ParseError) as e:
        print(f"公式有误：{e}", file=sys.stderr)
        return 2
    prog = comp.program()
    cal = st.read("calendar").filter(pl.col("is_open"))["date"].to_list()
    out = evaluate(build_frame(st, args.market, paths.profile, prog.columns, end=args.date), prog, cal)
    day = args.date or out["date"].max()
    today = out.filter(pl.col("date") == day)
    if today.is_empty():
        print(f"{day} 没有 K 线：不是交易日（或这一天还没有数据）")
        return 0
    hits = (today.filter(pl.col("hit")).join(st.read("instruments").select("listing_id", "name"), on="listing_id", how="left")
            .sort("listing_id"))
    print(f"{day}: {hits.height} 只命中，{today['hit'].null_count()} 只数据不足（未知），共 {today.height} 只")
    for r in hits.head(args.limit).iter_rows(named=True):
        print(f"  {r['listing_id']:<12} {r['name'] or ''}")
    return 0


def _structures(paths, args) -> int:
    import time

    from wb.engine.l2.run import structures

    t0 = time.perf_counter()
    try:
        res = structures(paths, args.market, workers=args.workers)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    for name, tables in res.items():
        print(f"{name:<12} " + "  ".join(f"{t} {df.height:,}" for t, df in tables.items()))
    print(f"{time.perf_counter() - t0:.1f}s")
    return 0


def _rule(paths, args) -> int:
    from pathlib import Path

    from wb.engine.l3.explain import STATUS, render
    from wb.engine.l3.rule import RuleError, load_rule
    from wb.engine.l3.run import run_rule
    from wb.workspace.rules import Rules

    rules = Rules(paths)
    try:
        if args.rule_cmd == "list":
            for r in rules.catalog():
                kind = "内置" if r["builtin"] else "自建"
                print(f"{r['id']:<26} v{r['version']:<3} {kind}  {'/'.join(r['markets']):<6} {r['name']}"
                      + (f"：{r['error']}" if r.get("error") else ""))
            return 0
        if args.rule_cmd == "show":
            rule, text, version = rules.get(args.rule, args.version)
            print(f"# {rule.id} 版本 {version}\n{text}")
            return 0
        if args.rule_cmd in ("check", "save"):
            text = Path(args.file).read_text(encoding="utf-8")
            if args.rule_cmd == "check":
                load_rule(text)
                print("规则没有问题")
            else:
                rid, version, new = rules.save(text)
                print(f"{rid} 版本 {version}" + ("（新版本）" if new else "（内容没变，没有新版本）"))
            return 0
        f = Path(args.rule)
        if f.suffix in (".yaml", ".yml", ".json") and f.exists():
            rule, version = load_rule(f.read_text(encoding="utf-8")), 0  # 0: not a saved version
        else:
            rule, _, version = rules.get(args.rule)
        run = run_rule(paths, rule, args.market, day=args.date, start=args.start, end=args.end,
                       listings=args.listing, workers=args.workers, version=version)
    except (RuleError, OSError) as e:
        print(f"规则有误：{e}", file=sys.stderr)
        return 2
    for w in run.warnings:
        print(f"注意：{w}", file=sys.stderr)
    if run.mode == "range":
        print(f"{run.start} 至 {run.end}：{run.counts['signal']} 个信号")
        for r in run.rows.head(args.limit).iter_rows(named=True):
            print(f"  {r['signal_date']}  {r['listing_id']:<12} {r['name'] or ''}")
    elif run.rows.is_empty():
        print(f"{run.day} 没有 K 线：不是交易日，或股票池是空的")
    else:
        c = run.counts
        print(f"{run.day}：信号 {c['signal']}、潜伏 {c['watch']}、无法判断 {c['unknown']}、未通过 {c['fail']}"
              f"（股票池 {run.rows.height} 只）")
        for r in run.rows.filter(pl.col("status") != "fail").head(args.limit).iter_rows(named=True):
            print(f"  {r['listing_id']:<12} {r['name'] or '':<12} {STATUS[r['status']]}")
        if args.explain:
            for line in render(run.matcher.detail(args.explain, run.day)):
                print(line)
    print(f"运行记录 {run.id}")
    return 0


def _background() -> None:
    """The app's backend gives way to what you are doing: below normal priority (its pool's workers inherit it), so a
    scan — every core busy for minutes after the close — never makes the window or the rest of the computer stutter."""
    if sys.platform == "win32":
        import ctypes

        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
    else:
        os.nice(10)


_CRASH = None   # faulthandler writes into it until the process ends
_JOB = None     # Windows: the job this backend's own processes live in (_end_with_me)


def _wait_for_exit(pid: int) -> bool:
    """Returns True once process `pid` has ended (the desktop app), False at once when it cannot be watched. On Windows a
    wait on its handle: the app's stdin pipe did not always tell (2026-10-10: the backend outlived a killed app there);
    elsewhere a look every 2 s."""
    import time
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)   # every type said: a handle is 64 bits, a bare int is 32
        k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
        k.CloseHandle.argtypes, k.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        h = k.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
        if not h:
            return ctypes.get_last_error() == 87      # ERROR_INVALID_PARAMETER: no such process — gone already
        k.WaitForSingleObject(h, 0xFFFFFFFF)          # INFINITE
        k.CloseHandle(h)
        return True
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:   # someone else's process with that number: not ours to watch
            return False
        time.sleep(2)


def _end_with_me() -> None:
    """Windows: the processes this backend starts (its pool's workers, the PDF reader) end with it, however it ends —
    there a process outlives its parent, and a backend gone mid-scan left its workers computing."""
    if sys.platform != "win32":
        return
    import ctypes
    import logging
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):   # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    global _JOB
    try:   # never in the way of the app starting: without it, only a backend gone mid-scan may leave workers behind
        k = ctypes.WinDLL("kernel32", use_last_error=True)   # every type said: a handle is 64 bits, a bare int is 32
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes, k.AssignProcessToJobObject.restype = [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL
        k.GetCurrentProcess.argtypes, k.GetCurrentProcess.restype = [], wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if job and k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) \
                and k.AssignProcessToJobObject(job, k.GetCurrentProcess()):   # 9: JobObjectExtendedLimitInformation
            _JOB = job   # held as long as this process lives: when it ends, so do the processes it started
        else:
            logging.getLogger("wb").warning("could not tie the backend's processes to it (Windows error %s)", ctypes.get_last_error())
    except Exception as e:   # noqa: BLE001 - said, and the backend runs on
        logging.getLogger("wb").warning("could not tie the backend's processes to it (%s)", e)


def _stop_with_app(server, stdin: bool) -> None:
    """The backend never outlives the desktop app: it stops when the app's stdin pipe closes or when the app's process
    (WB_PARENT_PID) ends — quit, crashed or killed — and a stop still hanging after 15 s (a scan running) ends it."""
    import logging

    def gone(how: str) -> None:
        logging.getLogger("wb").warning("the app is gone (%s): stopping", how)
        server.should_exit = True
        t = threading.Timer(15, lambda: os._exit(0))
        t.daemon = True
        t.start()

    def by_pipe() -> None:
        sys.stdin.buffer.read()   # returns at EOF
        gone("its pipe closed")

    def by_process(pid: int) -> None:
        if _wait_for_exit(pid):
            gone(f"process {pid} ended")
        else:
            logging.getLogger("wb").warning("cannot watch the app's process %s", pid)

    watching = []
    if stdin and sys.stdin is not None:
        threading.Thread(target=by_pipe, name="exit-with-app", daemon=True).start()
        watching.append("its pipe")
    if (pid := os.environ.get("WB_PARENT_PID", "")).isdigit():
        threading.Thread(target=by_process, args=(int(pid),), name="exit-with-app-process", daemon=True).start()
        watching.append(f"process {pid}")
    logging.getLogger("wb").info("stopping with the app: %s", ", ".join(watching) or "nothing to watch")


def _log_to(logs) -> None:
    """The log in the data folder (logs/backend.log, 3 × 2 MB): the installed app has no console, and this is what tells
    what a scan did on the user's computer. A crash writes every thread's stack to logs/crash.log."""
    import faulthandler
    import logging
    import platform
    from logging.handlers import RotatingFileHandler

    global _CRASH
    logs.mkdir(parents=True, exist_ok=True)
    # the installed app on Windows has no stderr (sys.stderr is None): only the file there
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", handlers=[
        *([logging.StreamHandler()] if sys.stderr else []),
        RotatingFileHandler(logs / "backend.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
    _CRASH = open(logs / "crash.log", "a", encoding="utf-8")
    faulthandler.enable(_CRASH, all_threads=True)
    from wb import __version__
    logging.getLogger("wb").info("start: %s, %s, Python %s, %s CPUs", __version__, platform.platform(),
                                 platform.python_version(), os.cpu_count())


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/redirects default to a legacy code page
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    profile = args.profile or os.environ.get("WB_PROFILE")
    if profile is None and getattr(args, "market", None) in MARKETS:
        from wb.workspace.store import Workspace

        profile = Workspace(get_paths("test", args.data_dir)).profile(args.market)  # 设置 → 数据档
    paths = get_paths(profile, args.data_dir)
    if profile is None and args.cmd == "serve":   # the app: each market on its own profile (设置 → 数据档)
        from wb.workspace.store import Workspace

        try:
            ws = Workspace(paths)
        except Exception as e:  # noqa: BLE001 - its last line is what the app shows when the backend cannot start (main.js)
            msg = str(e)
            why = ("文件已损坏" if "not a database" in msg or "malformed" in msg else "这个文件夹不能写入" if "readonly" in msg
                   else "磁盘满了" if "full" in msg else msg)
            print(f"数据目录打不开：{paths.shared_db}（{why}）。只读或磁盘满时先解决它；文件损坏了的话，"
                  f"{paths.backup_dir} 里有每天的备份（先关掉形研，删掉 shared.sqlite-wal 和 shared.sqlite-shm，再把备份复制成 shared.sqlite）",
                  file=sys.stderr)
            return 1
        print(f"[profile: {' · '.join(f'{m} {ws.profile(m)}' for m in MARKETS)}] {paths.root}")
    else:
        print(f"[profile: {paths.profile}] {paths.root}")

    if args.cmd == "secret":
        try:
            where = set_secret(args.name, getpass.getpass(f"{args.name}: "))
        except ValueError as e:
            print(e)
            return 1
        print("saved to the .env file, in its line" if where == ".env" else "saved to the OS keychain")
        return 0
    if args.cmd == "serve":
        import uvicorn

        from wb.api.app import create_app

        token = args.token or os.environ.get("WB_TOKEN")
        if not token:
            print("refusing to serve without a token (--token or WB_TOKEN)", file=sys.stderr)
            return 1
        _background()
        _log_to(paths.root / "logs")
        try:  # spec §4.3: the first start of the day copies shared.sqlite (labels, drawings, lists)
            done = backup_shared(paths)
            if done:
                print(f"backup: {done}")
        except Exception as e:  # noqa: BLE001 — a failed backup must not keep the app from starting
            print(f"backup failed: {e}", file=sys.stderr)
        # an open 盯盘 page holds its feed open: a stop waits for it at most 3 s
        server = uvicorn.Server(uvicorn.Config(create_app(paths.root, token, args.frontend, scheduler=True), host="127.0.0.1",
                                               port=args.port, log_level="warning", timeout_graceful_shutdown=3))
        _end_with_me()
        _stop_with_app(server, args.exit_with_stdin)
        server.run()
        return 0
    if args.cmd == "screen":
        return _screen(paths, args)
    if args.cmd == "structures":
        return _structures(paths, args)
    if args.cmd == "rule":
        return _rule(paths, args)
    if args.cmd == "staging":
        from wb.data.store import Staging

        sg = Staging(paths, args.market)
        n = sg.pending()
        if args.action == "clear":
            sg.clear()  # downloads only: the next update fetches them again
            print(f"{args.market}：丢弃了 {n} 个已抓取未发布的单元，下次更新会重新抓取")
        else:
            print(f"{args.market}：{n} 个已抓取未发布的单元")
        return 0
    if args.cmd == "devdata":
        from wb.devdata import write

        if paths.profile != "test":
            print("devdata only writes to the test profile", file=sys.stderr)
            return 1
        write(paths, args.days)
        print(f"demo data written to {paths.profile_dir}")
        return 0
    if args.cmd == "settings":
        from wb.workspace.store import Workspace

        ws = Workspace(paths)
        if args.key and args.value is not None:
            ws.set_setting(args.key, args.value)
        for k, v in ws.settings().items():
            print(f"{k} = {v}")
        return 0
    if args.cmd == "backup":
        dest = backup_shared(paths)
        print(dest or "today's backup already exists")
        return 0
    if args.cmd == "probe":
        conn = connect(paths.app_db, "app")
        for m in MARKETS if args.market == "all" else [args.market]:
            for prov, probes in probe_market(conn, paths.profile, m).items():
                for pr in probes:
                    print(f"{m} {prov:17} {pr.dataset:17} {'OK ' if pr.available else 'NO '}"
                          f"{str(pr.history_start or ''):10} {str(pr.latest_date or ''):10} "
                          f"{str(pr.rate_per_min or ''):>4} {pr.detail}")
        return 0

    if args.cmd in ("backfill", "update"):
        probe_if_stale(paths, args.market)
    pipe = Pipeline(paths, args.market) if hasattr(args, "market") and args.market else None
    try:
        if args.cmd == "backfill":
            snap = pipe.backfill(args.start, args.end, args.datasets.split(",") if args.datasets else None)
        elif args.cmd == "update":
            snap = pipe.update(args.date, wait=not args.no_wait)
            if snap is not None:
                _report_crosscheck(crosscheck(pipe, n=30, days=1))
        elif args.cmd == "refetch":
            latest = pipe.store.latest()
            snap = pipe.refetch(args.date or latest.as_of)
            print("no corrections" if snap is None else snap.note)
            return 0
        elif args.cmd == "snapshots":
            for s in Store(paths, args.market).history():
                print(f"{s.id}  as_of={s.as_of}  {s.created_at}  {s.note}")
            return 0
        elif args.cmd == "spotcheck":
            res = spotcheck(pipe, args.n, args.date, args.seed)
            bad = res.filter(~res["ok"])
            print(res.group_by("field").agg(n=pl.len(), ok=pl.col("ok").sum()).sort("field") if res.height else res)
            if bad.height:
                print(bad)
                return 4
            print(f"OK: {res['listing_id'].n_unique()} listings match the source")
            return 0
        elif args.cmd == "crosscheck":
            return 0 if _report_crosscheck(crosscheck(pipe, args.n, args.days, args.seed)) else 5
        elif args.cmd == "gc":
            print(f"removed {Store(paths, args.market).gc(args.keep)} files")
            return 0
    except DataDelayed as e:
        print(f"DELAYED: {e}", file=sys.stderr)
        return 2
    except QualityError as e:
        print(f"QUALITY BLOCKED: {e}", file=sys.stderr)
        print(f"这批数据另存在 {e.kept} 供检查；下次更新会重新向数据源要", file=sys.stderr)
        return 3
    print("nothing new" if snap is None else f"published {snap.id} as_of={snap.as_of}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
