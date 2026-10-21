"""The daily cycle of each market (spec §4.5, §7.1), run by a scheduler thread inside the API process.

- `update`: from the market's ready window on, on a trading day: P0's update — wait until the day's rows are there
  and stable, catch up any missed day, publish. Not there by the window's end: one 数据延迟 notice, no scan on
  partial data.
- `scan`: a snapshot nobody has scanned yet (today's, a correction, or one published while the app was closed):
  every enabled rule in date mode, compared with its previous run; what changed is queued for Feishu.
- `refetch`: from 08:30 the next morning: the previous trading day again — corrections are published and scanned;
  a day that was missing (a delay, the computer asleep) is caught up instead.
- Notices go out through the outbox whenever a Feishu bot is configured.
- A market without data waits for its backfill. Demo data (`wb devdata`) is never updated by the scheduler: fetched days would mix with made-up ones. A manual
  run scans it without fetching, and what it finds is never sent.

Each step records where the market stands (job_state), so a restart picks up where it left off; the steps of one
market run one after another in their own thread, so a long wait for one market never holds up the other."""
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from wb.ai.client import LLM, AIUnavailable
from wb.backup import backup_shared
from wb.config import Paths
from wb.data.capability import probe_if_stale
from wb.data.pipeline import DataDelayed, Pipeline
from wb.data.verify import crosscheck
from wb.data.store import Staging, Store
from wb.danger import history as danger_history
from wb.devdata import DEMO
from wb.docs.announcements import Cninfo
from wb.db import connect
from wb.engine.l1.frame import OPTIONAL, available_columns
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import RuleError
from wb.engine.l3.run import PHASES, RUN_LOCK, prune_runs, run_rows
from wb.markets import MARKETS, load_market
from wb.notify.brief import brief_on, make_brief
from wb.notify.feishu import Feishu, brief_card
from wb.notify.outbox import Outbox, drain
from wb.realtime.alerts import Alerts
from wb.screening.scan import Paused, enabled_rules, scan
from wb.secrets import get_secret
from wb.workspace.store import Workspace
from wb.workspace.watch import Watch

log = logging.getLogger("wb.jobs")
STATE = ("status", "message", "update_day", "refetch_day", "scanned_snapshot", "scan_at", "scan_summary", "retry_after",
         "pending", "scan_state", "brief_day")
RETRY = timedelta(minutes=10)  # a failed update or re-fetch is tried again after this, not on every tick
BRIEF_BEFORE_OPEN = timedelta(minutes=10)   # the latest a briefing waits for the morning's correction
STEP = {"update": "更新", "refetch": "订正", "scan": "扫描", "rescan": "扫描", "resume": "扫描", "complete": "补齐晚到的数据",
        "brief": "简报"}
QUEUED = "排队中：等别的全市场运行（另一个市场的扫描、验证或校准）做完"
CANCELLED = "已取消：这次扫描没有扫完，今日页还是上一次的结果；要扫就再点扫描"
LATE_NAME = {"valuation": "估值数据", "moneyflow": "资金流向"}
DATASET_NAME = {"calendar": "交易日历", "instruments": "股票列表", "status_intervals": "停牌和 ST", "industry": "行业",
                "corp_actions": "除权除息", "bars_1d": "日线", "valuation": "估值", "index_bars_1d": "指数日线",
                "index_members": "指数成分", "financials": "财务报告"}
DEMO_REFUSED = "这里是演示数据：取来的真实数据会和编出来的混在一起。换到正式档（设置 → 数据档）后再取"
RESUME = "到数据页点「补历史…」接着取；取完之前不做每天的更新"


def _fetching(line: str) -> tuple[str, int, int, int | None] | None:
    """The pipeline's progress lines: (dataset, done, to fetch, of all its units — said once, at the start); None for
    the rest."""
    if m := re.search(r"(\w+) via \S+: (\d+)/(\d+) units to fetch", line):
        return DATASET_NAME.get(m[1], m[1]), 0, int(m[2]), int(m[3])
    if m := re.fullmatch(r"\s+(\w+) (\d+)/(\d+)", line):
        return DATASET_NAME.get(m[1], m[1]), int(m[2]), int(m[3]), None
    return None


READY = re.compile(r"\w+ (\d\d:\d\d) rows (\d+)/(\d+)")   # data/ready.py's look at the day's rows
NAMING = re.compile(r"\s+instruments: naming (\d+) delisted")   # the pipeline naming placeholders from the source


def _progress(line: str) -> str | None:
    """The pipeline's progress lines as the 数据 page says them; None for the rest."""
    if (f := _fetching(line)) is None:
        return None
    name, done, total, units = f
    return f"{name}：要取 {total} 份（共 {units}）" if units is not None else f"{name}：{done} / {total}"


def _clock(t: datetime) -> str:
    """Local wall clock without a zone, as job_state keeps times."""
    return t.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


KEEP_SNAPSHOTS = 10  # snapshots whose files stay (each update rewrites the year's partition)
KEEP_RUNS = 60       # runs per rule and market (a daily scan writes one a day)


def _reads(rule, market: str, paths: Paths, datasets: list[str]) -> bool:
    """Whether a rule's formulas read one of these datasets (a valuation field: PE, 市值 …)."""
    store = Store(paths, market)
    try:
        cols = Matcher.columns(rule, market, available_columns(store, store.latest()))
    except RuleError:
        return False  # it fails in the scan and says why
    return any(OPTIONAL.get(c) in datasets for c in cols)


def housekeep(paths: Paths, market: str) -> None:
    """After a scan: the files of snapshots older than the newest KEEP_SNAPSHOTS and runs beyond KEEP_RUNS go —
    without it they pile up (~10 GB a year per market of rewritten partitions). A failure here never fails the scan."""
    try:
        Store(paths, market).gc(KEEP_SNAPSHOTS)
        prune_runs(paths, KEEP_RUNS)
    except OSError as e:
        log.warning("housekeeping %s: %s", market, e)


def _zh(e: Exception, market: str) -> str:
    """The pipeline's reasons, in the words the 今日 page uses."""
    text = str(e)
    m = re.search(r"(\d{4}-\d{2}-\d{2}): the session has not finished yet", text)
    if m:
        return f"{m.group(1)} 还没收盘"
    m = re.search(r"(\d{4}-\d{2}-\d{2}): data not ready by the end of the window", text)
    if m:
        return f"{m.group(1)} 的数据到就绪窗口结束（{load_market(market).ready_end.strftime('%H:%M')}）还没齐"
    m = re.search(r"calendar has no entry for (\S+) \(unknown is not closed; it ends at (\S+)\)", text)
    if m:
        return (f"交易日历里还没有 {m.group(1)}（只到 {m.group(2)}），不能判断那天开不开市：测试档的日历来自 exchange_calendars，"
                "升级它，或者把这个市场切到正式档（用 Tushare / J-Quants 的日历）")
    m = re.search(r"(\d+) fetched units are not published yet", text)
    if m:
        return f"有 {m.group(1)} 份已取到、还没发布的数据（补历史或更新做到一半）：到数据页点「补历史…」或「立即更新」接着做完"
    m = re.search(r"source returned no bars for (.+)$", text)
    if m:
        return f"数据源没有返回 {m.group(1)} 的日线，下次运行会再要一次"
    if "CERTIFICATE_VERIFY_FAILED" in text:   # 10-17 08:59 JST: Tushare's own certificate ran out; the key is never sent unverified
        if "expired" in text:
            return (f"数据源网站的证书过期了（是对方的问题，不是你的网络或 token）：为了不把 token 发到没验证过的连接上，先不连它，"
                    f"每 {RETRY.seconds // 60} 分钟自动再试，对方续上证书后就会取")
        return "连数据源时证书验证不过（可能是网络里的代理在拦截，或对方的证书有问题）：为了不把 token 发出去，没有连"
    return text


class Daily:
    def __init__(self, paths_of: Callable[[str], Paths], *, markets=MARKETS,
                 pipeline: Callable[[Paths, str], Pipeline] | None = None, allow_demo: bool = False,
                 transport: httpx.BaseTransport | None = None,
                 probe: Callable[[Paths, str], None] | None = None, check: Callable | None = None,
                 announcements: Callable | None = None):
        """`probe`, `check`: what the plan covers (§4.9) and the sample against an independent source (§4.6), both over
        the network — by default with the real pipeline only, never beside a stand-in; `announcements`: where a
        briefing's announcements come from (cninfo, likewise)."""
        self.paths_of, self.markets, self.allow_demo, self.transport = paths_of, tuple(markets), allow_demo, transport
        self._sending = threading.Lock()
        self.pipeline = pipeline or (lambda paths, m: Pipeline(paths, m, log=log.info))
        self.probe = probe or (None if pipeline else lambda paths, m: probe_if_stale(paths, m, log=log.info))
        self.check = check or (None if pipeline else crosscheck)
        self.announcements = announcements or (None if pipeline else Cninfo)
        self._running: dict[str, threading.Thread] = {}
        self._doing_now: dict[str, dict] = {}   # what each market's running turn is doing (progress)
        self._pausing: dict[str, threading.Event] = {}   # 暂停 or 取消 asked for: the scan stops at its next step
        self._giving_up: dict[str, bool] = {}            # 取消: and is not kept to continue
        self._claiming = threading.Lock()   # a market's turn is checked and taken in one step
        self._stop = threading.Event()
        self._backed_up: date | None = None   # the day shared.sqlite was last copied (_backup)

    # ---- state -----------------------------------------------------------------------------------------
    def _conn(self, market: str):
        return connect(self.paths_of(market).app_db, "app")  # the market's data profile: each profile has its own cycle

    def state(self, market: str) -> dict:
        row = self._conn(market).execute("SELECT * FROM job_state WHERE market=?", (market,)).fetchone()
        return dict(row) if row else {"market": market, **dict.fromkeys(STATE), "status": "idle", "updated_at": None}

    def _save(self, market: str, **fields) -> None:
        st = {**self.state(market), **fields, "updated_at": datetime.now().isoformat(timespec="seconds")}
        cols = ("market", *STATE, "updated_at")
        self._conn(market).execute(f"INSERT OR REPLACE INTO job_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             tuple(st[c] for c in cols))

    # ---- what is due -------------------------------------------------------------------------------------
    @staticmethod
    def _local(market: str, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(load_market(market).timezone))

    def _demo(self, market: str) -> bool:
        latest = Store(self.paths_of(market), market).latest()
        return not self.allow_demo and latest is not None and latest.note == DEMO

    def running(self, market: str) -> bool:
        return (t := self._running.get(market)) is not None and t.is_alive()

    def progress(self, market: str) -> dict | None:
        """What the market's running turn is doing, for 今日 and the status bar: `step` (update, scan, research, check,
        backfill), when the turn began (`since`), and the step's own counts — a scan's rule i of n, its phase, done of
        total and the share of the scan done (`fraction`). None when nothing runs. Kept in memory: it means nothing
        once the turn is over, and a turn does not outlive the app."""
        return self._doing_now.get(market) if self.running(market) else None

    def _doing(self, market: str, step: str, **counts) -> None:
        since = self._doing_now.get(market, {}).get("since") or datetime.now().isoformat(timespec="seconds")
        self._doing_now[market] = {"since": since, "step": step, **counts}

    def _fetch_log(self, market: str, step: str, also: Callable[[str], None] | None = None) -> Callable[[str], None]:
        """A pipeline's log that says how far its fetch is (dataset, done of total) in the market's progress."""
        def said(line: str) -> None:
            (also or log.info)(line)
            if (f := _fetching(line)) is not None:
                self._doing(market, step, phase=f[0], done=f[1], total=f[2])
            elif m := READY.match(line):   # between the calendar and the day's rows: waiting, not fetching the calendar
                self._doing(market, step, waiting=True, phase=f"等当天的数据（{m[1]} 已出 {int(m[2]):,} / {int(m[3]):,} 只）")
            elif m := NAMING.match(line):
                self._doing(market, step, phase=f"给 {int(m[1]):,} 只退市的股票补上名称和市场（只做一次）")
        return said

    def due(self, market: str, now: datetime) -> list[str]:
        m, t = load_market(market), self._local(market, now)
        st, paths = self.state(market), self.paths_of(market)
        latest = Store(paths, market).latest()
        if latest is None or self._demo(market):  # no data yet: the backfill (`wb backfill`) comes first
            return []
        if st["status"] == "interrupted":   # a 补历史 to finish first: an update would publish half of it, lose where it stopped
            return []
        pipe = self.pipeline(paths, market)
        out = []
        unscanned = latest.id != st["scanned_snapshot"] and not self._held(st, latest.id)   # a paused scan waits for 继续
        if st["retry_after"] and _clock(now) < st["retry_after"]:  # a failed update waits a while; a scan does not
            return ["scan"] if unscanned else []
        trading = pipe.is_trading_day(t.date())
        if st["update_day"] != t.date().isoformat() and ((trading is not False and t.time() >= m.ready_start)
                                                         or (trading is False and pipe.missing_history())):
            out.append("update")   # a day off only for a dataset the catalog gained (10-17: the holder lists waited for Monday)
        # the morning after (until the window opens; all day when the market is closed): the previous trading day
        # again — once the window opens, the day's update catches up whatever is missing by itself
        prev = pipe.previous_trading_day(t.date())
        morning = t.time() >= m.refetch_at and (t.time() < m.ready_start or trading is False)
        if latest is not None and prev is not None and morning and st["refetch_day"] != prev.isoformat() and latest.as_of <= prev:
            out.append("refetch")
        if st["pending"] and _clock(now) >= json.loads(st["pending"])["next"]:
            out.append("complete")
        if unscanned:
            out.append("scan")
        return out

    @staticmethod
    def _held(st: dict, snapshot: str) -> bool:
        """Whether the market's scan of this snapshot is paused (by you, or cut off by quitting the app) or cancelled by
        you: not started again by itself."""
        kept = json.loads(st["scan_state"]) if st.get("scan_state") else None
        return st["status"] in ("paused", "cancelled") and kept is not None and kept["snapshot"] == snapshot

    # ---- the steps ---------------------------------------------------------------------------------------
    def run(self, market: str, step: str, now: datetime) -> None:
        try:
            getattr(self, f"_{step}")(market, now)
        except Exception as e:  # noqa: BLE001 - a failing step is reported, the scheduler goes on
            log.exception("%s %s failed", market, step)
            fields: dict = {"status": "error", "message": f"{STEP.get(step, step)}出错：{_zh(e, market)}"}
            if step in ("update", "refetch"):    # tried again after a while, not on every tick
                fields["retry_after"] = _clock(now + RETRY)
            elif (latest := Store(self.paths_of(market), market).latest()) is not None:
                fields["scanned_snapshot"] = latest.id   # not scanned again on every tick; 立即扫描 scans it again
            self._save(market, **fields)

    def _probe(self, paths: Paths, market: str) -> None:
        try:
            if self.probe:
                self.probe(paths, market)
        except Exception:  # noqa: BLE001 - never-probed sources are still tried (capability.route)
            log.exception("%s: probing the data sources failed", market)

    def _sample(self, pipe, market: str) -> None:
        """Spec §4.6: each publish of the day, 30 stocks against an independent source; reported, never fatal."""
        try:
            if self.check:
                self._doing(market, "check")
                self.check(pipe, 30, 1)
        except Exception:  # noqa: BLE001
            log.exception("%s: the check against the independent source failed", market)

    def _update(self, market: str, now: datetime) -> None:
        paths, day = self.paths_of(market), self._local(market, now).date()
        self._save(market, status="updating", message=None)
        self._doing(market, "update", waiting=True)   # until the day's data is there: the ready window, hours at worst
        self._probe(paths, market)
        pipe = self.pipeline(paths, market)
        pipe.log = self._fetch_log(market, "update")
        try:
            published = pipe.update(day, wait=True) is not None
        except DataDelayed as e:   # said by the state, and by the next morning's briefing
            self._save(market, status="delayed", message=_zh(e, market), update_day=day.isoformat(), retry_after=None)
            return
        late = self.pipeline(paths, market).incomplete(day)   # what comes later than the bars waits on its own
        poll = timedelta(minutes=load_market(market).poll_minutes)
        self._save(market, update_day=day.isoformat(), retry_after=None,
                   pending=json.dumps({"day": day.isoformat(), "datasets": late, "next": _clock(now + poll)}) if late else None)
        self._scan(market, now)
        if published:
            self._sample(pipe, market)   # after the scan: a slow independent source never holds up the day's notices

    def _complete(self, market: str, now: datetime) -> None:
        """Datasets that came later than the bars (spec §4.5): asked again every poll; published, every rule is scanned;
        still short when the ready window ends, the rules that waited are scanned with what there is."""
        paths, st, m = self.paths_of(market), self.state(market), load_market(market)
        p = json.loads(st["pending"])
        if self.pipeline(paths, market).complete(date.fromisoformat(p["day"]), p["datasets"]) is not None:
            self._save(market, pending=None)
            self._scan(market, now)
            return
        t = self._local(market, now)
        if t.time() >= m.ready_end or t.date().isoformat() > p["day"]:
            names = "、".join(LATE_NAME.get(ds, ds) for ds in p["datasets"])
            self._save(market, pending=None)
            self._scan(market, now, force=True, note=f"{names}到就绪窗口结束（{m.ready_end.strftime('%H:%M')}）还没齐："
                       "用到它的规则按缺数据扫描，明早订正后会重扫")
            return
        self._save(market, pending=json.dumps({**p, "next": _clock(now + timedelta(minutes=m.poll_minutes))}))

    def _refetch(self, market: str, now: datetime) -> None:
        paths = self.paths_of(market)
        pipe, latest = self.pipeline(paths, market), Store(paths, market).latest()
        prev = pipe.previous_trading_day(self._local(market, now).date())
        if latest is None or prev is None:
            return
        self._save(market, status="updating", message=None)
        self._doing(market, "update")
        pipe.log = self._fetch_log(market, "update")
        try:
            if latest.as_of < prev:  # a day is missing (a delay, the computer asleep): catch it up — a past day is final
                pipe.update(prev, wait=False)
            else:                    # the previous day again: corrections are published as a new snapshot
                pipe.refetch(prev)
        except DataDelayed as e:
            self._save(market, status="delayed", message=_zh(e, market), refetch_day=prev.isoformat(), retry_after=None)
            return
        self._save(market, refetch_day=prev.isoformat(), retry_after=None)
        self._scan(market, now)

    def _rescan(self, market: str, now: datetime) -> None:
        """A manual run: scan even a snapshot already scanned (a rule enabled since, a rule changed)."""
        self._scan(market, now, force=True)

    def _resume(self, market: str, now: datetime) -> None:
        """继续: the paused scan of the snapshot, from the first rule it had not finished."""
        self._scan(market, now, force=True, resume=True)

    def pause(self, market: str) -> bool:
        """暂停: the running scan stops at its next step (a rule's step, a chunk of structures, 25 listings) and keeps
        the rules it finished — 继续 goes on from the next. False when no scan runs (a fetch is not paused)."""
        p = self.progress(market)
        if not p or p.get("step") != "scan":
            return False
        self._pausing.setdefault(market, threading.Event()).set()
        self._doing(market, "scan", **{k: v for k, v in p.items() if k not in ("since", "step")}, pausing=True)
        return True

    def cancel_scan(self, market: str) -> bool:
        """取消: the running scan stops at its next step and is given up — 今日 keeps the last scan's results, and this
        snapshot is not scanned again by itself (the scan button does it; the next day's data is scanned as usual). A
        paused scan is given up the same way. False: no scan runs or waits paused."""
        p = self.progress(market)
        if p and p.get("step") == "scan":
            self._giving_up[market] = True
            self._pausing.setdefault(market, threading.Event()).set()
            self._doing(market, "scan", **{k: v for k, v in p.items() if k not in ("since", "step", "pausing")}, cancelling=True)
            return True
        st = self.state(market)
        if self.running(market) or st["status"] != "paused":
            return False
        kept = json.loads(st["scan_state"]) if st["scan_state"] else {}
        self._save(market, status="cancelled", message=CANCELLED, scan_state=json.dumps({"snapshot": kept.get("snapshot"), "cancelled": True}))
        return True

    def resume(self, market: str) -> bool:
        """继续 a paused scan in the market's turn. False: nothing is paused, or the market is busy."""
        if self.state(market)["status"] != "paused":
            return False
        return self._claim(market, "resume", lambda: self.run(market, "resume", datetime.now().astimezone()))

    def _scan(self, market: str, now: datetime, force: bool = False, note: str | None = None, resume: bool = False) -> None:
        paths = self.paths_of(market)
        latest, st = Store(paths, market).latest(), self.state(market)
        held = latest is not None and self._held(st, latest.id)
        if held and not force:   # you paused this snapshot's scan: it waits for 继续
            return
        if latest is None or (latest.id == st["scanned_snapshot"] and not force):
            if st["status"] not in ("error", "delayed"):  # nothing to scan: an earlier step's failure stays in view
                self._save(market, status="done", scan_state=None)
            return
        rules = enabled_rules(paths)
        late = json.loads(st["pending"])["datasets"] if st["pending"] else []
        waiting = [(r, v) for r, v in rules if late and _reads(r, market, paths, late)]   # these wait for their data
        todo = [x for x in rules if x not in waiting]
        # what the scan has finished, kept as it goes: a pause, or quitting the app, loses at most the rule under way
        kept = {"snapshot": latest.id, "total": sum(market in r.markets for r, _ in todo),
                "done": json.loads(st["scan_state"])["done"] if resume and held else {}}
        self._save(market, status="scanning", message=None if not st["pending"] else st["message"], scan_state=json.dumps(kept))
        self._doing(market, "scan", fraction=0)
        stop = self._pausing.setdefault(market, threading.Event())

        def said(p: dict) -> None:   # the share done: the rules before, and this one's steps (each counted step by its count)
            if stop.is_set():
                raise Paused
            within = (PHASES.index(p["phase"]) + (p["done"] / p["total"] if p["total"] else 0)) / len(PHASES)
            self._doing(market, "scan", **p, fraction=round((p["i"] - 1 + within) / p["n"], 3))

        def finished(key: str, run_id: str) -> None:
            kept["done"][key] = run_id
            self._save(market, scan_state=json.dumps(kept))
        acquired = RUN_LOCK.acquire(blocking=False)
        if not acquired:  # another whole-market run holds it: said, then waited for — a pause still stops the wait
            self._save(market, message=QUEUED)
            while not stop.is_set() and not (acquired := RUN_LOCK.acquire(timeout=0.5)):
                pass
            self._save(market, message=None if not st["pending"] else st["message"])   # its turn now: no longer waiting
        try:
            if not acquired:
                raise Paused
            if market == "CN":   # 危险提醒's history first: a page opened during the scan finds it, and the two peaks never overlap
                try:
                    danger_history(Store(paths, market), latest)
                except Exception:  # noqa: BLE001 - computed on the first request instead
                    log.exception("%s: 危险提醒 history", market)
            summaries = scan(paths, market, todo, progress=said, done=kept["done"], finished=finished)
            housekeep(paths, market)  # under the lock: no whole-market reader is still on an older snapshot
        except Paused:
            if self._giving_up.get(market):   # 取消: what it finished is not kept to continue
                self._save(market, status="cancelled", message=CANCELLED, scan_state=json.dumps({"snapshot": latest.id, "cancelled": True}))
            else:
                self._save(market, status="paused",
                           message=f"已暂停：{len(kept['done'])} / {kept['total']} 条规则做完了，「继续」从下一条接着扫")
            return
        finally:
            if acquired:
                RUN_LOCK.release()
        self._resolve(paths, market, summaries, now)   # the day's intraday rule alerts, judged by the close (in the app only)
        if not self._demo(market):  # watch entries are shared by the profiles: a made-up close never fires one
            Watch(self.paths_of).check(market, now=now.astimezone().replace(tzinfo=None))   # said in the next briefing
        summary = {s["rule_id"]: {"name": s["name"], "version": s["version"], "error": s["error"]} if "error" in s else
                   {"name": s["name"], "version": s["version"], "day": str(s["day"]), "run_id": s["run_id"],
                    "previous_run": s["previous_run"], "stages": s["stages"], "counts": s["counts"],
                    "changes": {k: len(v) for k, v in s["changes"].items()}} for s in summaries}
        for r, v in waiting:
            summary[r.id] = {"name": r.name, "version": v, "waiting": f"等{'、'.join(LATE_NAME.get(ds, ds) for ds in late)}"}
        failed = [f"规则「{s['name']}」扫描出错：{s['error']}" for s in summaries if "error" in s]
        status, message = ("error", "；".join(failed)) if failed else \
            (("error", st["message"]) if st["retry_after"] else ("done", note))   # a pending update failure stays
        self._save(market, status=status, message=message, scanned_snapshot=latest.id, scan_state=None,
                   scan_at=now.isoformat(timespec="seconds"), scan_summary=json.dumps(summary, ensure_ascii=False))
        if not self._demo(market):
            self._research(market, latest.as_of, summaries, Workspace(paths).settings())

    def _research(self, market: str, day: date, summaries: list[dict], settings: dict) -> None:
        """Spec §10.4: researches whose 20 trading days have passed are settled; §10.3: then up to `research.daily` of
        the day's new signals researched by AI (default 0: none), each stock once a day. AI that cannot run (no key,
        the budget, offline) stops it quietly — the scan is done either way."""
        from wb.ai import journal
        try:
            journal.settle(self.paths_of)
        except Exception:  # noqa: BLE001
            log.exception("%s: settling researches failed", market)
        if market == "CN":   # 价格行为分析 (design 2026-10-20 §5.2, dev branch): its plans and forward excess, settled the same way
            try:
                from wb.pa.settle import settle_all
                settle_all(self.paths_of)
            except Exception:  # noqa: BLE001
                log.exception("%s: settling 价格行为 analyses failed", market)
        cap, paths = int(settings.get("research.daily", 0)), self.paths_of(market)
        if not cap:
            return
        try:
            done = {r["listing_id"] for r in journal.recent(paths, 500) if r["day"] == str(day)}
            new = dict.fromkeys(x["listing_id"] for s in summaries for x in s.get("changes", {}).get("signal", []))
            llm = LLM(paths, transport=self.transport)
        except Exception:  # noqa: BLE001 - the scan is done either way
            log.exception("%s: daily research could not start", market)
            return
        todo = [x for x in new if x not in done][:cap]
        for k, lid in enumerate(todo):
            self._doing(market, "research", done=k, total=len(todo))
            try:
                journal.run(llm, self.paths_of, lid, trigger="daily")
            except AIUnavailable as e:
                log.info("%s: daily research stopped: %s", market, e)
                return
            except Exception:  # noqa: BLE001 - one stock's failure (the model twice wrong, …) leaves the others
                log.exception("%s: daily research of %s failed", market, lid)

    @staticmethod
    def _resolve(paths: Paths, market: str, summaries: list[dict], now: datetime) -> list[dict]:
        """Spec §7.2: each scanned rule's intraday alerts of its day — the stocks it signalled are confirmed, the rest
        revoked; a rule that was not scanned (waiting, failed) keeps its alerts waiting."""
        alerts, out = Alerts(paths), []
        for s in summaries:
            if "run_id" not in s:
                continue
            signalled = run_rows(paths, s["run_id"]).filter(pl.col("status") == "signal")["listing_id"].to_list()
            out += alerts.resolve(market, s["day"], {(s["rule_id"], lid) for lid in signalled},
                                  now=now.astimezone().replace(tzinfo=None), rule=s["rule_id"])
        return out

    # ---- 开盘前简报 (spec §7.1, 1.34) ----------------------------------------------------------------------
    def brief_when(self, market: str, now: datetime, waiting: bool) -> bool:
        """Whether to make the market's briefing now: data that is not made up, the briefing on, a trading day, its time
        reached and the close not, today's not made yet; while the morning's correction or its scan is under way
        (`waiting`) only once it is 10 minutes before the open."""
        paths, m, t = self.paths_of(market), load_market(market), self._local(market, now)
        if Store(paths, market).latest() is None or self._demo(market):
            return False
        ws = Workspace(paths)
        settings = ws.settings()
        if not brief_on(settings, market, ws.profile(market)) or self.state(market)["brief_day"] == t.date().isoformat():
            return False
        if self.pipeline(paths, market).is_trading_day(t.date()) is not True:
            return False
        at, close = clock_time.fromisoformat(settings[f"brief.time.{market}"]), m.sessions[-1][1]
        latest = (datetime.combine(t.date(), m.sessions[0][0]) - BRIEF_BEFORE_OPEN).time()
        return at <= t.time() < close and (not waiting or t.time() >= latest)

    def brief(self, market: str, now: datetime, *, test: bool = False, announcements: bool = True) -> dict | None:
        """The market's briefing as it would go out now (also 预览 and 发一份到飞书): for today before its close, else for
        the next trading day; marked late (补发) when made after today's open."""
        paths, m, t = self.paths_of(market), load_market(market), self._local(market, now)
        pipe = self.pipeline(paths, market)
        day = t.date()
        if pipe.is_trading_day(day) is not True or t.time() >= m.sessions[-1][1]:
            day = next((day + timedelta(days=k) for k in range(1, 15) if pipe.is_trading_day(day + timedelta(days=k))),
                       day + timedelta(days=1))
        s = Workspace(paths).settings()
        latest = Store(paths, market).latest()
        b = make_brief(paths, market, day, pipe.previous_trading_day(day), self.state(market),
                       late=not test and day == t.date() and t.time() >= m.sessions[0][0],
                       note="演示数据：只能预览，不会发到飞书" if latest is not None and latest.note == DEMO else None,
                       cninfo=self.announcements() if announcements and self.announcements else None, per_rule=s["brief.per_rule"],
                       near_pct=s["brief.near_pct"], near_max=s["brief.near_max"], empty=s["brief.empty"] or test)
        if b is not None and test:
            b["title"] += "（测试）"
        return b

    def _brief(self, market: str, now: datetime) -> None:
        """Make today's briefing and queue it as one card, given up at the close if it could not go out; a briefing
        that cannot be made is said on the market's state and not tried again today."""
        paths, m, t = self.paths_of(market), load_market(market), self._local(market, now)
        try:
            b = self.brief(market, now)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: the briefing failed", market)
            self._save(market, brief_day=t.date().isoformat(), message=f"开盘前简报没做成：{_zh(e, market)}")
            return
        if b is not None:
            close = datetime.combine(t.date(), m.sessions[-1][1], tzinfo=t.tzinfo)
            Outbox(paths).enqueue("brief", f"brief|{market}|{t.date()}",
                                  {"title": b["title"], "line": b["line"], "card": brief_card(b),
                                   "expires": close.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")},
                                  market, t.date().isoformat(), now=now)
        self._save(market, brief_day=t.date().isoformat())

    # ---- notices -----------------------------------------------------------------------------------------
    def notify(self, now: datetime) -> int:
        """Send what is due when a Feishu bot is configured (notices wait in the outbox until then) — one sender at a
        time: the live job sends the moment it queues an alert, maybe while this round sends."""
        webhook = get_secret("feishu_webhook")
        if not webhook:
            return 0
        with self._sending:
            box = Outbox(self.paths_of(self.markets[0]))
            if not box.pending(now):  # nothing due: no client, no key read
                return 0
            try:
                bot = Feishu(webhook, get_secret("feishu_secret"), transport=self.transport)
            except ValueError as e:  # the settings page says so (/api/notify: problem)
                log.warning("feishu: %s", e)
                return 0
            return drain(box, bot, now)

    # ---- the scheduler -----------------------------------------------------------------------------------
    def tick(self, now: datetime) -> None:
        """Start each market's due steps (one thread per market at a time) — the briefing after the morning's
        correction and its scan, or beside them once it is late — then send what is due."""
        for market in self.markets:
            try:
                if self.running(market):
                    if self.brief_when(market, now, waiting=True):   # 10 minutes before the open: not after the turn
                        self._brief(market, now)
                    continue
                steps = self.due(market, now)
                waiting = bool({"refetch", "scan", "complete"} & set(steps))
                if self.brief_when(market, now, waiting):
                    steps = ["brief", *steps] if waiting else [*steps, "brief"]
            except Exception:  # noqa: BLE001 - e.g. no data yet for that market
                log.exception("%s: cannot tell what is due", market)
                continue
            if steps:   # unless a job took the market while this was being worked out
                self._claim(market, "daily", lambda m=market, s=steps: [self.run(m, x, datetime.now().astimezone()) for x in s])
        try:
            self.notify(now)
        except Exception:  # noqa: BLE001
            log.exception("notify failed")
        self._backup(now)

    def _backup(self, now: datetime) -> None:
        """Spec §4.3: a copy of shared.sqlite each day — also when the app stays open for days (`wb serve` copies at start
        only). Once a day: one that fails is said in the log, not tried again every tick."""
        if self._backed_up == now.date():
            return
        self._backed_up = now.date()
        try:
            backup_shared(self.paths_of(self.markets[0]), today=now.date())
        except Exception:  # noqa: BLE001 - a failed copy never stops the day's jobs
            log.exception("the daily copy of shared.sqlite failed")

    def recover(self) -> None:
        """At start, a job the app was quit in the middle of: a scan is paused where it was (继续 goes on from the
        rule under way); a 补历史 with units fetched and not published yet is said, and comes before any update; a
        check, or a backfill that had published everything, is simply over."""
        for market in self.markets:
            st = self.state(market)
            if not self.running(market) and st["status"] == "scanning":   # quit in the middle of a scan
                latest = Store(self.paths_of(market), market).latest()
                kept = json.loads(st["scan_state"]) if st["scan_state"] else None
                if kept and latest is not None and kept["snapshot"] == latest.id:   # what it finished is kept: 继续
                    self._save(market, status="paused", message=f"上次扫描没做完（应用关掉了）：{len(kept['done'])} / "
                               f"{kept['total']} 条规则做完了，「继续」从下一条接着扫")
                else:
                    self._save(market, status="done", message=None, scan_state=None)
                continue
            if self.running(market) or st["status"] not in ("backfilling", "checking"):
                continue
            n = Staging(self.paths_of(market), market).pending() if st["status"] == "backfilling" else 0
            if n:
                self._save(market, status="interrupted", message=f"补历史没取完：关掉应用时已取到 {n} 份、还没发布。{RESUME}")
            else:
                self._save(market, status="done", message=None)

    def startup(self) -> None:
        """When the app starts: what was cut off is said (recover), and each market's data sources are probed (§4.9)."""
        self.recover()
        for market in self.markets:
            if not self._demo(market):
                self._probe(self.paths_of(market), market)

    def start(self, every: float = 30.0) -> threading.Thread:
        def loop():
            self.startup()
            while not self._stop.is_set():
                self.tick(datetime.now().astimezone())
                self._stop.wait(every)
        t = threading.Thread(target=loop, name="daily-scheduler", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def fetches(self, market: str, now: datetime | None = None) -> bool:
        """Whether a manual run fetches: not on demo data, not with a 补历史 to finish, not before the market's day is over
        (there is nothing new to fetch) — then it only scans what there is again."""
        if self._demo(market) or self.state(market)["status"] == "interrupted":
            return False
        local, m = self._local(market, now or datetime.now().astimezone()), load_market(market)
        return not (self.pipeline(self.paths_of(market), market).is_trading_day(local.date()) and local.time() < m.ready_start)

    def run_now(self, market: str, now: datetime | None = None) -> bool:
        """A manual run (立即扫描, 立即更新): update without waiting for the window once the day is over, then scan the
        latest snapshot again. False: the market is busy."""
        def go():
            t = now or datetime.now().astimezone()
            paths, failed, pipe = self.paths_of(market), None, None
            if self.fetches(market, t):
                self._save(market, status="updating", message=None)
                self._doing(market, "update")
                self._probe(paths, market)
                pipe = self.pipeline(paths, market)
                pipe.log = self._fetch_log(market, "update")
                try:
                    if pipe.update(None, wait=False) is None:
                        pipe = None   # nothing new published: nothing to sample
                except DataDelayed as e:
                    failed, pipe = {"status": "delayed", "message": _zh(e, market)}, None
                except Exception as e:  # noqa: BLE001
                    failed, pipe = {"status": "error", "message": f"更新出错：{_zh(e, market)}"}, None
            before = self.state(market)
            self.run(market, "rescan", t)   # what there is gets scanned all the same
            if self.state(market)["status"] in ("paused", "cancelled"):   # you paused or cancelled it: that is where it is
                pass
            elif failed:                      # and the update's failure stays in view
                self._save(market, **failed)
            elif before["status"] == "interrupted":   # and so does the 补历史 to finish
                self._save(market, status="interrupted", message=before["message"])
            if pipe is not None:
                self._sample(pipe, market)
        return self._claim(market, "manual", go)

    def _claim(self, market: str, name: str, go: Callable[[], object]) -> bool:
        """Take the market's turn and run `go` in it — checked and taken in one step, so two jobs never run side by side
        on one market (they would share its staging: one's publish would take the other's half)."""
        def turn() -> None:
            try:
                go()
            finally:
                self._doing_now.pop(market, None)
        with self._claiming:
            if self.running(market):
                return False
            self._doing_now[market] = {"since": datetime.now().isoformat(timespec="seconds"), "step": name}
            self._pausing[market] = threading.Event()
            self._giving_up[market] = False
            t = threading.Thread(target=turn, name=f"daily-{market}-{name}", daemon=True)
            self._running[market] = t
            t.start()
            return True

    def _refuse_demo(self, market: str) -> None:
        latest = Store(self.paths_of(market), market).latest()
        if not self.allow_demo and latest is not None and latest.note == DEMO:
            raise ValueError(DEMO_REFUSED)

    def backfill_now(self, market: str, start: date, end: date | None = None, datasets: list[str] | None = None) -> bool:
        """补历史 from the 数据 page (the CLI's `wb backfill`): resumable, how far it is in the market's state, and what it
        brought scanned. False: the market is busy."""
        self._refuse_demo(market)
        # up to yesterday: today's day is the daily update's, once its data is ready (§4.5: a half day is never scanned)
        end = end or self._local(market, datetime.now().astimezone()).date() - timedelta(days=1)

        def go():
            paths, since = self.paths_of(market), start.isoformat()
            self._save(market, status="backfilling", message=f"补历史（{since} 起）：开始")
            self._probe(paths, market)
            pipe = self.pipeline(paths, market)

            def said(line: str) -> None:
                log.info(line)
                if (p := _progress(line)) is not None:
                    self._save(market, message=f"补历史（{since} 起）：{p}")
            pipe.log = self._fetch_log(market, "backfill", said)
            try:
                pipe.backfill(start, end, datasets)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: backfill failed", market)
                n = Staging(paths, market).pending()   # what it had fetched waits for the rest, not for an update
                self._save(market, status="interrupted" if n else "error",
                           message=f"补历史出错：{_zh(e, market)}。" + (f"已取到 {n} 份，{RESUME}" if n else "可以再补一次"))
                return
            self._save(market, status="done", message=None)
            self.run(market, "rescan", datetime.now().astimezone())
        return self._claim(market, "backfill", go)

    def crosscheck_now(self, market: str) -> bool:
        """抽样核对 from the 数据 page (the CLI's `wb crosscheck`): 30 stocks × 5 days against an independent source;
        the market's state is left as it was, with what was found. False: the market is busy."""
        self._refuse_demo(market)

        def go():
            before = self.state(market)
            keep = before["status"] in ("error", "delayed", "interrupted")   # its reason stays; the results are on the 数据 page
            self._save(market, status="checking", message="抽样核对中")
            self._doing(market, "check")
            try:
                res = (self.check or crosscheck)(self.pipeline(self.paths_of(market), market), 30, 5)
            except Exception as e:  # noqa: BLE001
                log.exception("%s: crosscheck failed", market)
                self._save(market, status=before["status"], message=before["message"] if keep else f"抽样核对出错：{_zh(e, market)}")
                return
            n = res.height if res is not None else 0
            ok = int(res["ok"].sum()) if n else 0
            said = "没有可以比对的独立来源数据" if n == 0 else f"{n} 项全部一致" if ok == n else f"{n} 项里 {ok} 项一致，不一致的列在数据页"
            self._save(market, status=before["status"], message=before["message"] if keep else f"抽样核对：{said}")
        return self._claim(market, "crosscheck", go)


def wait_until(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    """For tests and the manual run: poll until `predicate()` or the timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False
