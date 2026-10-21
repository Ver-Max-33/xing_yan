"""市场看板的每分钟记录 (design 2026-10-12 §8.2–§8.4): the live thread asks TickFlow for every A share's quote — once a
minute while the market trades (a row of the record), every 10 s while a page wants it, and through the closing
windows (11:30–11:35, 15:00–15:05) until the closing auction's print is in; it keeps the latest quote as the day's
snapshot file (the K-line's forming bar of any stock reads it), fetches today's limit prices from 09:00 until they
are complete, and fills the minutes it missed — the day before, the start of today — from 1-minute bars. Only the
live thread fetches and writes; the page reads what it kept. A failure is the board's own error, never 盯盘's."""
import io
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from wb.data.store import Store
from wb.datause import allowed
from wb.files import move_into_place, tmp_for
from wb.market.board import compute, possible, tape_row, traded
from wb.market.facts import INDEXES, Facts, build_facts, fetch_limits, limits_complete
from wb.markets import load_market
from wb.realtime.clock import clock_for, share
from wb.realtime.providers.tushare_rt import SNAPSHOT
from wb.realtime.session import Session, minute_index, minute_span
from wb.secrets import secret_source
from wb.workspace.store import Workspace

log = logging.getLogger("wb.market")
SH = "CN:IDX:000001"
KEEP_DAYS, KEEP_LIMITS = 250, 5
WANTED = timedelta(seconds=30)      # a page (or a K-line) that asked this recently gets the 10-second cadence
FAST = timedelta(seconds=10)
CLOSING = timedelta(seconds=20)     # in a closing window the print comes late: asked again every 20 s until it ends
LIMIT_TRY = timedelta(minutes=1)    # today's limit prices asked again until two in a row match (§8.3)
LIMIT_WAIT = timedelta(minutes=10)  # a fill of today waits this long for them, then goes ahead without
FILL_PACE = 6.0                     # seconds between 1-minute calls while the market trades: ten a minute, 盯盘 keeps the rest
FULL_DAY = 200                      # a day the record has fewer rows of is filled from 1-minute bars
ROWS = {"i": pl.Int64, "at": pl.Datetime, "traded": pl.Int64, "up": pl.Int64, "down": pl.Int64, "flat": pl.Int64,
        "amount": pl.Float64, "limit_up": pl.Int64, "broken": pl.Int64, "limit_down": pl.Int64, "seal_rate": pl.Float64,
        "pool": pl.Int64, "promoted": pl.Int64, "promo_rate": pl.Float64, "height": pl.Int64, "top_sealed": pl.Boolean,
        "top_name": pl.Utf8, "top_chg": pl.Float64, "second_chg": pl.Float64, "sh_last": pl.Float64, "ladder": pl.Utf8,
        "top1_name": pl.Utf8, "top1_chg": pl.Float64, "second1_chg": pl.Float64,
        "sealed_ids": pl.List(pl.Utf8), "broken_ids": pl.List(pl.Utf8)}


def _read(path: Path) -> pl.DataFrame:
    return pl.read_parquet(io.BytesIO(path.read_bytes()))   # never memory-mapped: Windows could not replace it


def _write(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_for(path)
    df.write_parquet(tmp)
    move_into_place(tmp, path, tries=20)


def _thread(f: Callable[[], None]) -> None:
    threading.Thread(target=f, name="market-fill", daemon=True).start()


class MarketTape:
    def __init__(self, paths_of, source_for, publish: Callable[[dict], None] = lambda e: None, *,
                 fetch_limits: Callable[[date], pl.DataFrame] = fetch_limits, run: Callable = _thread,
                 sleep: Callable[[float], None] | None = None, batch: int = 100, fill: bool = True):
        """`fetch_limits`, `run` (how a fill runs: a thread), `sleep`, `batch` (symbols a 1-minute call): for tests."""
        import time as _time
        self.paths_of, self.source_for, self.publish = paths_of, source_for, publish
        self.fetch_limits, self.run, self.sleep, self.batch, self.fill = fetch_limits, run, sleep or _time.sleep, batch, fill
        self._wanted: dict[str, datetime] = {}
        self._fetched: dict[str, tuple[date, datetime]] = {}
        self._latest: dict[str, dict] = {}
        self._quote: dict[str, pl.DataFrame] = {}
        self._facts: dict[tuple, Facts] = {}
        self._limits: dict[tuple, tuple] = {}      # (market, day) -> (frame | None, complete, last try)
        self._limits_from: dict[tuple, datetime] = {}   # (market, day) -> the first try
        self._clock: dict[str, tuple] = {}         # market -> (day, the volume clock): read once a day, not each fetch
        self._fill: dict[tuple, str] = {}          # (market, day) -> running / done
        self._recorded: dict[tuple, set] = {}      # (market, day) -> the rows' minutes kept
        self._lock = threading.Lock()

    # ---- whether -------------------------------------------------------------------------------------------------
    def why_not(self, market: str) -> str | None:
        """None when the board can be live; else why the page shows the close (design §8.6)."""
        if market != "CN":
            return "日股没有全市场的盘中行情：页面用收盘数据"
        paths = self.paths_of(market)
        if paths.profile != "prod":
            return "测试档没有盘中看板：页面用收盘数据"
        if not secret_source("tickflow_api_key"):
            return "没有 TickFlow 的 key（设置 → 盘中行情）：页面用收盘数据"
        settings = Workspace(paths).settings()
        if not settings.get("market.tape", True):
            return "设置里关了盘中市场记录：页面用收盘数据"
        if not allowed(settings, "tickflow_rt", "store"):
            return "数据使用里关了 TickFlow 的本地存储：页面用收盘数据"
        return None

    def want(self, market: str, now: datetime) -> None:
        """A page or a K-line asked: the 10-second cadence for the next 30 seconds."""
        self._wanted[market] = now

    # ---- the live thread -----------------------------------------------------------------------------------------
    def tick(self, market: str, s: Session, src, now: datetime) -> None:
        if s.state == "closed" or not hasattr(src, "whole_market") or self.why_not(market) is not None:
            return
        t = s.now.time()
        if time(9, 0) <= t < time(15, 5):
            self._limits_step(market, s.day, now)
        if self._window(s):
            last = self._fetched.get(market)
            fresh = last is None or last[0] != s.day
            wanted = market in self._wanted and now - self._wanted[market] <= WANTED
            i = self._row(s)
            kept = self._kept(market, s.day)
            due = (fresh or (wanted and now - last[1] >= FAST) or (i is not None and (not kept or i > max(kept)))
                   or (self._closing(s) and now - last[1] >= CLOSING))
            if due:
                self._fetch(market, s, src, now, i)
        elif s.state in ("lunch", "after") and (self._latest.get(market) or {}).get("day") != s.day:
            # opened at lunch or after the close: nothing of today in memory, so the page would show the day before —
            # one fetch gives the 11:30 / closing prices (again a minute later while it fails); no record row (a fill
            # makes those)
            last = self._fetched.get(market)
            if last is None or last[0] != s.day or now - last[1] >= timedelta(minutes=1):
                self._fetch(market, s, src, now, None, late=True)
        if self.fill:
            self._maybe_fill(market, s, src)

    @staticmethod
    def _window(s: Session) -> bool:
        t = s.now.time()
        return (s.state == "trading" or (s.state == "pre" and time(9, 25) <= t)
                or (s.state == "lunch" and t < time(11, 35)) or (s.state == "after" and t < time(15, 5)))

    @staticmethod
    def _closing(s: Session) -> bool:
        return s.state in ("lunch", "after")

    @staticmethod
    def _row(s: Session) -> int | None:
        """The row a fetch now is: the minutes traded so far (the closing windows: 120, 240); none for the auction's
        result before 9:30, nor in the afternoon's first minute — its count is the morning's, whose row is the morning's
        close kept at lunch (a fetch at 13:00 put the afternoon's first minute in its place, 10-12)."""
        morning = minute_span(*load_market(s.market).sessions[0])
        return None if s.state == "pre" or (s.state == "trading" and s.elapsed == morning) else s.elapsed

    def _fetch(self, market: str, s: Session, src, now: datetime, i: int | None, late: bool = False) -> None:
        self._fetched[market] = (s.day, now)
        try:
            q = src.whole_market()
            ix = src.quotes(list(INDEXES[market]))
        except Exception as e:  # noqa: BLE001 - the board says it; 盯盘's own feed goes on
            log.warning("market %s: whole-market quote: %s", market, e)
            with self._lock:
                self._latest[market] = {**(self._latest.get(market) or {}), "error": f"最近一次 {s.now:%H:%M}：{e}", "error_at": now}
            return
        facts = self.facts(market, s.day)
        q, impossible = possible(q, facts, s.day)   # never on the board, in the snapshot or the page's prices
        if impossible:
            log.warning("market %s: %d prices beyond today's limits left out (not trades)", market, impossible)
        if s.state != "pre" and traded(q, s.day).is_empty():   # no trade of today in it: a blip, not an empty market
            log.warning("market %s: a whole-market quote without today's trades", market)
            with self._lock:
                self._latest[market] = {**(self._latest.get(market) or {}), "error": f"最近一次 {s.now:%H:%M}：TickFlow 没有给出今天的行情",
                                        "error_at": now}
            return
        # a late fetch's board is the session's last minute (its prices are 11:30's / the close's)
        at = _end_of(s.day, s.elapsed, load_market(market).sessions) if late else s.now.replace(tzinfo=None, microsecond=0)
        now_ix = [r for r in ix.iter_rows(named=True) if r["last"] is not None and r["at"].date() == s.day]
        idx, idx_prev = {r["listing_id"]: r["last"] for r in now_ix}, {r["listing_id"]: r["prev_close"] for r in now_ix if r["prev_close"]}
        if self._clock.get(market, (None,))[0] != s.day:
            self._clock[market] = (s.day, clock_for(self.paths_of(market), market))
        done = 1.0 if s.state == "after" else share(self._clock[market][1], max(1, s.elapsed), s.total)
        board = compute(traded(q, s.day), facts, at, done, idx, idx_prev) | {"impossible": impossible}
        self._write_snapshot(market, s.day, q)
        if i is not None:
            self._put_rows(market, s.day, [tape_row(board, i)], replace=True)
        with self._lock:
            self._quote[market] = q
            self._latest[market] = {"at": board["at"], "board": board, "fetched": now, "error": None, "day": s.day}
        self.publish({"type": "board", "market": market, "at": board["at"]})

    # ---- what the page reads ---------------------------------------------------------------------------------------
    def latest(self, market: str) -> dict | None:
        with self._lock:
            return self._latest.get(market)

    def quote(self, market: str) -> pl.DataFrame | None:
        """The last whole-market quote fetched (the page reads your stocks' prices from it)."""
        with self._lock:
            return self._quote.get(market)

    def rows(self, market: str, day: date) -> list[dict]:
        path = self._dir(market) / f"date={day}.parquet"
        return _read(path).sort("i").to_dicts() if path.exists() else []

    def facts(self, market: str, day: date, snap=None) -> Facts:
        """The day's base, cached per data snapshot and whether today's limits are complete."""
        store = Store(self.paths_of(market), market)
        snap = snap or store.latest()
        lim = self._limits.get((market, day))
        frame = lim[0] if lim and lim[1] else self._limits_on_disk(market, day)
        key = (market, day, snap.id if snap else None, frame is not None)
        if key not in self._facts:
            self._facts = {k: v for k, v in self._facts.items() if k[:2] != key[:2]}
            self._facts[key] = build_facts(self.paths_of(market), market, day, limits=frame, snap=snap)
        return self._facts[key]

    # ---- files ---------------------------------------------------------------------------------------------------
    def _dir(self, market: str) -> Path:
        return self.paths_of(market).market_dir(market) / "tape"

    def _kept(self, market: str, day: date) -> set:
        key = (market, day)
        if key not in self._recorded:
            path = self._dir(market) / f"date={day}.parquet"
            self._recorded[key] = set(_read(path)["i"].to_list()) if path.exists() else set()
        return self._recorded[key]

    def _put_rows(self, market: str, day: date, rows: list[dict], replace: bool) -> None:
        """Rows into the day's record: `replace` puts these minutes in place of what is there (a live fetch refreshes
        its minute); otherwise only minutes not kept are added (a fill never overwrites a recorded minute, except one
        recorded before today's limit prices were known when the fill knows them)."""
        if not rows:
            return
        path = self._dir(market) / f"date={day}.parquet"
        new = pl.DataFrame(rows, schema=ROWS)
        with self._lock:
            old = _read(path) if path.exists() else pl.DataFrame(schema=ROWS)
            if replace:
                old = old.filter(~pl.col("i").is_in(new["i"].implode()))
            else:
                redo = new.filter(pl.col("limit_up").is_not_null())["i"]
                old = old.filter(~(pl.col("i").is_in(redo.implode()) & pl.col("limit_up").is_null()))
                new = new.filter(~pl.col("i").is_in(old["i"].implode()))
            df = pl.concat([old, new], how="diagonal_relaxed").sort("i")
            _write(df, path)
            self._recorded[(market, day)] = set(df["i"].to_list())
            for f in sorted(self._dir(market).glob("date=*.parquet"))[:-KEEP_DAYS]:
                f.unlink(missing_ok=True)

    def _write_snapshot(self, market: str, day: date, q: pl.DataFrame) -> None:
        """The quote's stocks that have traded today as the day's snapshot (盯盘's file: the K-line's forming bar)."""
        folder = self.paths_of(market).market_dir(market) / "intraday"
        snap = traded(q, day).select("listing_id", "at", "open", "high", "low", "last", "volume", "amount",
                                     pl.col("prev_close").alias("pre_close")).cast(SNAPSHOT)
        _write(snap, folder / f"snapshot={day}.parquet")
        for f in folder.glob("snapshot=*.parquet"):
            if f.stem != f"snapshot={day}":
                f.unlink(missing_ok=True)

    # ---- today's limit prices ----------------------------------------------------------------------------------------
    def _limits_on_disk(self, market: str, day: date) -> pl.DataFrame | None:
        path = self._dir(market) / f"limits={day}.parquet"
        return _read(path) if path.exists() else None

    def _limits_step(self, market: str, day: date, now: datetime) -> None:
        key = (market, day)
        frame, done, tried = self._limits.get(key, (None, False, None))
        if done:
            return
        if frame is None and (kept := self._limits_on_disk(market, day)) is not None:
            self._limits[key] = (kept, True, now)
            return
        if tried is not None and now - tried < LIMIT_TRY:
            return
        self._limits_from.setdefault(key, now)
        try:
            got = self.fetch_limits(day)
        except Exception as e:  # noqa: BLE001 - without them the limit numbers are 未知, not wrong
            log.warning("market %s: today's limit prices: %s", market, e)
            self._limits[key] = (frame, False, now)
            return
        if limits_complete(frame, got):
            _write(got, self._dir(market) / f"limits={day}.parquet")
            for f in sorted(self._dir(market).glob("limits=*.parquet"))[:-KEEP_LIMITS]:
                f.unlink(missing_ok=True)
            self._limits[key] = (got, True, now)
        else:
            self._limits[key] = (got, False, now)

    # ---- filling the minutes missed (§8.4) ---------------------------------------------------------------------------
    def _maybe_fill(self, market: str, s: Session, src) -> None:
        if not hasattr(src, "minutes"):
            return
        trading = s.state == "trading"
        facts = self.facts(market, s.day)
        prev = facts.prev_day
        if (not trading and prev is not None and (market, prev) not in self._fill and not facts.stale
                and len(self._kept(market, prev)) < FULL_DAY):
            # unpaced only once the day's session is over: before the open (or at lunch) its calls would hold 盯盘's
            # first polls behind the shared K-line limiter
            self._start_fill(market, prev, src, 240, paced=s.state not in ("after", "closed"))
        kept = self._kept(market, s.day)
        if trading and kept and (market, s.day) not in self._fill:
            # today's minutes not recorded (the app was not running), or recorded before today's limits were known:
            # once those are known (or have not come for 10 minutes), their rows would have no limit numbers for good
            known = facts.limits == "today"
            first = self._limits_from.get((market, s.day))
            if not known and (first is None or s.now - first < LIMIT_WAIT):
                return
            last = max(kept)
            lacking = known and any(r["limit_up"] is None for r in self.rows(market, s.day))
            if len(kept) < last or lacking:
                self._start_fill(market, s.day, src, last - 1, paced=True)

    def _start_fill(self, market: str, day: date, src, upto: int, paced: bool) -> None:
        self._fill[(market, day)] = "running"

        def work():
            try:
                self._fill_day(market, day, src, upto, paced)
            except Exception:  # noqa: BLE001 - a fill is a convenience: the live record goes on
                log.exception("market %s: filling %s", market, day)
            finally:
                self._fill[(market, day)] = "done"
        self.run(work)

    def _fill_day(self, market: str, day: date, src, upto: int, paced: bool) -> None:
        facts = self.facts(market, day)
        ids = facts.stocks["listing_id"].to_list()
        parts = []
        for k in range(0, len(ids), self.batch):
            if paced and k:
                self.sleep(FILL_PACE)
            parts.append(src.minutes(ids[k:k + self.batch], day, INDEXES[market] if k == 0 else ()))
        minutes = pl.concat([p for p in parts if p.height], how="diagonal_relaxed") if parts else pl.DataFrame()
        if minutes.is_empty():
            return
        prev = self._prev_closes(market, day, facts)
        self._put_rows(market, day, rebuild(minutes, facts, prev, upto, clock_for(self.paths_of(market), market)), replace=False)

    def _prev_closes(self, market: str, day: date, facts: Facts) -> pl.DataFrame:
        """Each listing's close before as the exchange gives it: that day's stored bars (the day before), today's quote
        (today), else the last close the data knows."""
        store = Store(self.paths_of(market), market)
        got = store.scan("bars_1d").filter(pl.col("date") == day).select("listing_id", pl.col("pre_close").alias("prev_close")).collect()
        q = self._quote.get(market)
        if got.is_empty() and q is not None:
            got = q.select("listing_id", "prev_close")
        return facts.stocks.select("listing_id", pl.col("prev").alias("_p")).join(got, on="listing_id", how="left").select(
            "listing_id", pl.coalesce("prev_close", "_p").alias("prev_close"))


def rebuild(minutes: pl.DataFrame, facts: Facts, prev: pl.DataFrame, upto: int, clock) -> list[dict]:
    """The day's rows 1 … `upto` from its 1-minute bars: row i is the market at the end of the minute i − 1 — each
    stock's last price then, its high, volume and amount so far — computed as a live fetch would be (§8.4)."""
    sessions = load_market(facts.market).sessions
    m = minutes.with_columns(minute_index(sessions, pl.col("at")).alias("m")).filter(pl.col("m") < upto)
    if "amount" not in m.columns:
        m = m.with_columns((pl.col("close") * pl.col("volume")).alias("amount"))
    idx = m.filter(pl.col("listing_id") == SH).select("m", "close")
    sh = dict(zip(idx["m"].to_list(), idx["close"].to_list(), strict=True))
    st = (m.filter(~pl.col("listing_id").str.contains(":IDX:")).sort("listing_id", "m").with_columns(
        pl.col("open").first().over("listing_id").alias("o"), pl.col("high").cum_max().over("listing_id").alias("h"),
        pl.col("low").cum_min().over("listing_id").alias("lo"), pl.col("volume").cum_sum().over("listing_id").alias("v"),
        pl.col("amount").cum_sum().over("listing_id").alias("a"))
        .join(prev, on="listing_id", how="left"))
    day = facts.day
    rows = []
    for i in range(1, upto + 1):
        xs = (st.filter(pl.col("m") <= i - 1).group_by("listing_id").last()
              .select("listing_id", pl.lit(day).alias("_d"), pl.col("o").alias("open"), pl.col("h").alias("high"),
                      pl.col("lo").alias("low"), pl.col("close").alias("last"), "prev_close", pl.col("v").alias("volume"),
                      pl.col("a").alias("amount")))
        at = _end_of(day, i, sessions)
        xs = xs.with_columns(pl.lit(at).alias("at")).drop("_d")
        board = compute(traded(xs, day), facts, at, share(clock, i, 240), {SH: sh[i - 1]} if i - 1 in sh else {})
        rows.append(tape_row(board, i))
    return rows


def _end_of(day: date, i: int, sessions) -> datetime:
    """The end of the market's i-th minute (i = 120: 11:30; 240: 15:00)."""
    left = i
    for a, b in sessions:
        span = (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
        if left <= span:
            return datetime.combine(day, a) + timedelta(minutes=left)
        left -= span
    return datetime.combine(day, sessions[-1][1])
