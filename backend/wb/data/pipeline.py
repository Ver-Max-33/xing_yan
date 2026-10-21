"""Backfill / daily update / next-morning refetch (spec §4.5).

fetch:   provider units -> Staging (resumable: finished units are skipped on rerun)
publish: Staging -> factors, instrument upkeep, quality checks -> Store snapshot -> event
"""
import io
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from wb import events
from wb.config import Paths
from wb.data.capability import load_caps, make_provider, record_error, route
from wb.data.normalize import apply_factors
from wb.data.providers.base import Provider, Unavailable
from wb.data.quality import Issue, blocking, check_bars, check_index_bars, check_intraday, check_moneyflow
from wb.data.ready import wait_ready
from wb.data.store import Snapshot, Staging, Store
from wb.datause import SOURCES, allowed
from wb.datasets import DATASETS, empty
from wb.markets import load_market

ORDER = ["calendar", "instruments", "status_intervals", "industry", "corp_actions", "bars_1d",
         "valuation", "moneyflow", "index_bars_1d", "index_members", "financials", "unlocks", "float_holders"]
DAILY = ["calendar", "instruments", "corp_actions", "bars_1d", "valuation", "index_bars_1d", "financials", "moneyflow", "unlocks",
         "float_holders"]
WEEKLY = ["status_intervals", "industry", "index_members"]  # slow to fetch, rarely change: refreshed on Mondays
LATER = ["valuation", "moneyflow"]  # fetched by the day, often later than the bars: completed on its own when it comes (§4.5)
HISTORY = ["moneyflow", "float_holders"]  # added to the catalog after the backfill: their whole history once, then as usual
READY_SHARE = 0.98     # a day's rows count as there at 98% of its traded listings
CALENDAR_AHEAD = 90    # days of calendar kept ahead: 解禁前 counts 20 open days to an unlock, 33–37 calendar days across 春节 / 国庆
# the close a report's time is read against before a date (財務 PIT): TSE closed at 15:00 until it moved to 15:30 on
# 2024-11-05 — a 15:00 disclosure then came after the close (59,753 of the stored reports)
EARLIER_CLOSE = {"JP": (date(2024, 11, 5), "15:00")}


def quarter_ends(start: date, end: date, back: int = 190) -> list[date]:
    """The report periods whose reports can come in [start, end]: the quarter ends from 190 days before it to its end
    (an annual report is due by the end of April; late filers and new listings bring a period's first figures in May
    or June — later ones, and corrections of an older period, come with a backfill)."""
    lo, out, y = start - timedelta(days=back), [], (start - timedelta(days=back)).year
    while True:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q = date(y, m, d)
            if q > end:
                return out
            if q >= lo:
                out.append(q)
        y += 1


class QualityError(RuntimeError):
    def __init__(self, issues: list[Issue], kept=None):
        super().__init__("; ".join(f"{i.check}: {i.detail}" for i in issues))
        self.issues, self.kept = issues, kept   # where the refused batch was set aside (Staging.set_aside)


class DataDelayed(RuntimeError): ...


class Pipeline:
    def __init__(self, paths: Paths, market: str, providers: dict[str, Provider] | None = None,
                 log: Callable[[str], None] = print,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.paths, self.market, self.log, self.clock = paths, market, log, clock
        self.store = Store(paths, market)
        self.staging = Staging(paths, market)
        self.caps = load_caps(self.store.conn, market)
        self.providers = dict(providers or {})
        self.unavailable: set[str] = set()
        from wb.workspace.store import Workspace   # the shared settings (no cycle at import time)
        settings = Workspace(paths).settings()
        self.kept_off = {s for s in SOURCES if not allowed(settings, s, "store")}   # §4.10: never fetched

    # ---- helpers -------------------------------------------------------------------------
    def provider_for(self, dataset: str) -> Provider | None:
        if dataset in self.unavailable:
            return None
        name = route(self.paths.profile, self.market, dataset, self.caps)
        if name is None or name in self.kept_off:
            return None
        if name not in self.providers:
            self.providers[name] = make_provider(name, self.market, self.caps)
        return self.providers[name]

    def _current(self, name: str) -> pl.DataFrame:
        """Published rows plus staged rows (staged win)."""
        key = list(DATASETS[name].key)
        return pl.concat([self.store.read(name), self.staging.collect(name)]).unique(key, keep="last")

    def trading_days(self, start: date, end: date) -> list[date]:
        cal = self._current("calendar")
        return sorted(cal.filter(pl.col("is_open") & pl.col("date").is_between(start, end))["date"].to_list())

    def is_trading_day(self, day: date) -> bool | None:
        """From the calendar already stored (no fetch): True / False, None when it does not cover `day`."""
        cal = self._current("calendar").filter(pl.col("date") == day)
        return bool(cal["is_open"][0]) if cal.height else None

    def _behind_catalog(self, ds: str, snap: Snapshot | None) -> bool:
        """A weekly dataset stored before the catalog gained a column (the industry rows' 申万二级, design 2026-10-12
        §10): it comes again with the next update instead of waiting for Monday."""
        files = self.store.files(ds, snap) if snap else []
        return bool(files) and bool(set(DATASETS[ds].columns) - set(pl.read_parquet_schema(files[0])))

    def previous_trading_day(self, day: date) -> date | None:
        """The last trading day before `day` in the stored calendar."""
        days = self.trading_days(day - timedelta(days=20), day - timedelta(days=1))
        return days[-1] if days else None

    def today(self) -> date:
        return self.clock().astimezone(ZoneInfo(load_market(self.market).timezone)).date()

    def last_complete_day(self) -> date:
        """Today once the ready window has opened (session closed, spec §4.5), else yesterday.
        Sources like yfinance return a half-finished bar during the session; it must not be stored."""
        m = load_market(self.market)
        t = self.clock().astimezone(ZoneInfo(m.timezone))
        return t.date() if t.time() >= m.ready_start else t.date() - timedelta(days=1)

    # ---- fetch ---------------------------------------------------------------------------
    def fetch(self, start: date, end: date, datasets: list[str] = ORDER) -> None:
        for ds in datasets:
            prov = self.provider_for(ds)
            if prov is None or ds not in prov.unit_kinds:
                if (name := route(self.paths.profile, self.market, ds, self.caps)) in self.kept_off:
                    self.log(f"{self.market} {ds}: {SOURCES[name]} 在 设置 → 数据使用 里关了“本地存储”，不取")
                continue  # not routed here (e.g. JP corp_actions arrive with bars_1d)
            cap = self.caps.get((prov.name, ds))
            lo = max(start, cap.history_start) if cap and cap.history_start else start
            hi = min(end, cap.latest_date) if cap and cap.latest_date else end  # e.g. a plan 12 weeks behind
            if lo > hi:
                self.log(f"{self.market} {ds} via {prov.name}: the plan covers "
                         f"{cap.history_start or '…'} – {cap.latest_date or '…'}, nothing in {start} – {end}")
                continue
            kind = prov.unit_kinds[ds]
            if kind == "date":
                units = [d.isoformat() for d in self.trading_days(lo, hi)]
            elif kind == "all":
                units = ["all"]
            elif kind == "period":   # report periods (quarter ends)
                units = [q.isoformat() for q in quarter_ends(lo, hi)]
            else:
                units = prov.tickers(ds, self._current("instruments"))
            todo = [u for u in units if not self.staging.done(prov.name, ds, u)]
            self.log(f"{self.market} {ds} via {prov.name}: {len(todo)}/{len(units)} units to fetch")
            # a source with per-minute quotas (`workers`) is asked several units at once — its shared limiter keeps
            # the rate — and they are staged in order, so a failure keeps the units before it, as one at a time
            pool = ThreadPoolExecutor(max(1, getattr(prov, "workers", 1)))
            try:
                asked = [pool.submit(prov.fetch, ds, unit, lo, hi) for unit in todo]
                for i, (unit, fut) in enumerate(zip(todo, asked), 1):
                    try:
                        self.staging.put(prov.name, ds, unit, fut.result())
                    except Unavailable as e:
                        self.log(f"{self.market} {ds}: not available on this plan, skipped ({e})")
                        self.unavailable.add(ds)
                        break
                    except Exception as e:   # §4.9: kept for the settings page, then the update stops as before
                        record_error(self.store.conn, self.market, prov.name, ds, unit, f"{type(e).__name__}: {e}")
                        raise
                    if i % 50 == 0:
                        self.log(f"  {ds} {i}/{len(todo)}")
            finally:
                pool.shutdown(cancel_futures=True)

    # ---- publish -------------------------------------------------------------------------
    @staticmethod
    def _nfkc(df: pl.DataFrame) -> pl.DataFrame:
        # JPX writes Latin letters and spaces full-width; one form for search and display.
        return df.with_columns(pl.col(c).str.normalize("NFKC") for c in ("name", "name_en") if c in df.columns)

    def _instruments(self, new: pl.DataFrame | None, bars: pl.DataFrame | None) -> pl.DataFrame | None:
        old = self.store.read("instruments")
        prov = self.provider_for("instruments")
        if new is not None:
            new = self._nfkc(new)
        out = new
        if new is not None and new.height and prov is not None:
            # A full refresh that no longer lists a stock means it was delisted — unless it traded in this batch: then the
            # list left it out (one answer short: 60 trading stocks were marked 已退市 on a replayed 10-12), it stays as it was
            src = new["source"][0]
            gone = old.filter((pl.col("source") == src) & (pl.col("status") == "L")).join(
                new.select("listing_id"), on="listing_id", how="anti")
            trading = (bars.filter(pl.col("close").is_not_null()).select("listing_id").unique() if bars is not None
                       else pl.DataFrame(schema={"listing_id": pl.Utf8}))
            out = pl.concat([new, gone.join(trading, on="listing_id", how="anti").with_columns(pl.lit("D").alias("status")),
                             gone.join(trading, on="listing_id", how="semi")])
        if bars is not None:
            known = pl.concat([old.select("listing_id"), (out if out is not None else empty("instruments")).select("listing_id")])
            missing = bars.select("listing_id").unique().join(known, on="listing_id", how="anti")
            if missing.height:
                status = "D" if prov is not None and prov.complete_list else "U"
                ph = missing.with_columns(
                    pl.lit(self.market).alias("market"),
                    pl.col("listing_id").str.split(":").list.get(1).alias("code"),
                    pl.lit(status).alias("status"), pl.lit("placeholder").alias("source"))
                out = ph if out is None else pl.concat([out, ph], how="diagonal_relaxed")
        return self._named(old, out, bars)

    def _named(self, old: pl.DataFrame, out: pl.DataFrame | None, bars: pl.DataFrame | None) -> pl.DataFrame | None:
        """Listings known only by their code (placeholders: the bars name them, no current list does) as the source can
        still name them — J-Quants: as its master listed them on their last day. One it cannot name is asked again at the
        next publish; a failure never stops one."""
        prov = self.provider_for("instruments")
        if prov is None or not hasattr(prov, "describe"):
            return out
        rows = old if out is None else pl.concat([old, out], how="diagonal_relaxed")
        ids = rows.unique("listing_id", keep="last", maintain_order=True).filter(pl.col("source") == "placeholder")["listing_id"]
        if ids.is_empty():
            return out
        last = (self.store.scan("bars_1d").filter(pl.col("listing_id").is_in(ids.implode())).select("listing_id", "date").collect()
                if self.store.latest() is not None else pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date}))
        if bars is not None:
            last = pl.concat([last, bars.filter(pl.col("listing_id").is_in(ids.implode())).select("listing_id", "date")])
        last = dict(last.group_by("listing_id").agg(pl.col("date").max()).iter_rows())
        self.log(f"  instruments: naming {len(last)} delisted listings")
        try:
            named = prov.describe(last)
        except Exception as e:  # the names wait for the next publish, the data does not
            self.log(f"  instruments: delisted listings not named this time ({type(e).__name__}: {e})")
            return out
        if named.is_empty():
            return out
        named = self._nfkc(named)
        return named if out is None else pl.concat([out.join(named.select("listing_id"), on="listing_id", how="anti"), named],
                                                   how="diagonal_relaxed")

    def _factors(self, bars: pl.DataFrame, new_actions: pl.DataFrame) -> pl.DataFrame:
        if self.market == "CN" and ("corp_actions" in self.unavailable or self.provider_for("corp_actions") is None):
            return bars.with_columns(pl.lit(None, pl.Float64).alias("share_factor"))  # unknown, not 1.0
        key = list(DATASETS["corp_actions"].key)
        old_actions = self.store.read("corp_actions")
        actions = pl.concat([old_actions, new_actions]).unique(key, keep="last")
        # Listings that gained or changed an event (e.g. a corrected ratio): recompute their history.
        cols = [c for c in new_actions.columns if c != "source"]
        affected = new_actions.join(old_actions, on=cols, how="anti", nulls_equal=True).select("listing_id").unique()
        if affected.height:
            hist = self.store.scan("bars_1d").join(affected.lazy(), on="listing_id").collect()
            bars = pl.concat([hist.join(bars.select("listing_id", "date"), on=["listing_id", "date"], how="anti"), bars])
        return apply_factors(bars, actions, set_price=self.market == "JP")

    def _known(self, fin: pl.DataFrame) -> pl.DataFrame:
        """财务 PIT (spec §4.7): the first trading day whose close may read a report — its own day when it was announced
        before the close (JP gives the time), else the next trading day (A-share reports give none: most come after the
        close). Past the stored calendar it stays unknown until the calendar covers it."""
        close = pl.lit(load_market(self.market).sessions[-1][1].strftime("%H:%M"))
        if self.market in EARLIER_CLOSE:
            until, was = EARLIER_CLOSE[self.market]
            close = pl.when(pl.col("ann_date") < until).then(pl.lit(was)).otherwise(close)
        days = self._current("calendar").filter(pl.col("is_open")).select(pl.col("date").alias("known_at")).unique().sort("known_at")
        # HH:MM against HH:MM: J-Quants gave "15:30" until 2018-03 and "15:30:00" since, and "15:30" < "15:30:00" as text
        before = pl.col("ann_time").is_not_null() & (pl.col("ann_time").str.slice(0, 5) < close)
        f = fin.drop("known_at", strict=False).with_columns(
            pl.when(before).then(pl.col("ann_date")).otherwise(pl.col("ann_date") + pl.duration(days=1)).alias("_from"))
        return f.sort("_from").join_asof(days, left_on="_from", right_on="known_at", strategy="forward").drop("_from")

    def _kept_minutes(self, days: list[date]) -> pl.DataFrame | None:
        """The minutes 盘中 kept on these days (read whole: Windows cannot replace a file another holds open)."""
        folder = self.paths.market_dir(self.market) / "intraday"
        files = [f for d in days if (f := folder / f"date={d}.parquet").exists()]
        return pl.concat([pl.read_parquet(io.BytesIO(f.read_bytes())) for f in files], how="diagonal_relaxed") if files else None

    def publish(self, as_of: date, note: str = "") -> Snapshot | None:
        staged = {name: self.staging.collect(name) for name in DATASETS}
        updates = {k: v for k, v in staged.items() if v.height}
        if not updates:
            return None
        bars = updates.get("bars_1d")
        if bars is not None:
            # the batch's own first day: the history _factors adds for a listing whose corporate actions changed is
            # published already, and must not move the day-over-day checks' day before back to the listing's first bar
            first = bars["date"].min()
            bars = self._factors(bars, staged["corp_actions"])
            updates["bars_1d"] = bars
        inst = self._instruments(updates.get("instruments"), bars)
        if inst is not None:
            updates["instruments"] = inst
        if "financials" in updates or "calendar" in updates:   # and the stored ones the calendar did not cover yet
            fin = pl.concat([self.store.read("financials").filter(pl.col("known_at").is_null()),
                             updates.get("financials", empty("financials"))], how="diagonal_relaxed").unique(
                list(DATASETS["financials"].key), keep="last")   # a stored one fetched again: the new copy
            if fin.height:
                updates["financials"] = self._known(fin)
        issues: dict[str, list[Issue]] = {}
        if bars is not None:
            # Day-over-day checks (adj identity, factor jumps, the day as a whole) need yesterday's published rows too —
            # every listing's: also one the batch lacks (a half-published day), and also on a day another listing's corporate
            # action brought its whole history into the batch (that listing has the row already)
            before = self.trading_days(first - timedelta(days=30), first - timedelta(days=1))
            ctx = (self.store.scan("bars_1d").filter(pl.col("date") == before[-1]).collect()
                   .join(bars.select("listing_id", "date"), on=["listing_id", "date"], how="anti")
                   if before else bars.head(0))
            checked = pl.concat([ctx, bars], how="diagonal_relaxed") if ctx.height else bars
            found = check_bars(checked, self.trading_days(checked["date"].min(), bars["date"].max()),
                               halts=set(load_market(self.market).halts))
            issues["bars_1d"] = [i for i in found if i.date is None or i.date >= first]  # ctx is already published
            if blocking(issues["bars_1d"]):   # set aside for inspection; the next run asks the source again
                raise QualityError(blocking(issues["bars_1d"]), self.staging.set_aside())
            kept = self._kept_minutes(bars["date"].unique().to_list())   # 盘中's minutes: the bar is their witness
            if kept is not None:
                issues["intraday"] = check_intraday(kept, bars, load_market(self.market).sessions[-1][1])
        idx = updates.get("index_bars_1d")
        if idx is not None:
            prov = self.provider_for("index_bars_1d")
            issues["index_bars_1d"] = check_index_bars(idx, self.trading_days(idx["date"].min(), idx["date"].max()),
                                                       prov.index_since if prov else None)
        flow = updates.get("moneyflow")
        if flow is not None:   # 资金流向 against the day's bars (design 2026-10-15 §5.1): the published ones and this batch's
            days = flow["date"].unique().to_list()
            have = (self.store.scan("bars_1d").filter(pl.col("date").is_in(days)).select("listing_id", "date", "amount").collect()
                    if self.store.latest() is not None else pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, "amount": pl.Float64}))
            if bars is not None:
                have = pl.concat([have, bars.select("listing_id", "date", "amount")]).unique(["listing_id", "date"], keep="last")
            issues["moneyflow"] = check_moneyflow(flow, have, recent_from=as_of - timedelta(days=10))
            if blocking(issues["moneyflow"]):   # set aside for inspection; the next run asks the source again
                raise QualityError(blocking(issues["moneyflow"]), self.staging.set_aside())
        snap = self.store.publish(updates, as_of=as_of, note=note, issues=issues)
        self.staging.clear()
        events.publish("snapshot.published", {"market": self.market, "snapshot_id": snap.id, "as_of": as_of})
        return snap

    # ---- entry points --------------------------------------------------------------------
    def _stored_here(self) -> None:
        """Spec §4.10: the bars' source kept from storing here is never fetched — said, and never a snapshot without
        its bars (the next update would start after it)."""
        off = route(self.paths.profile, self.market, "bars_1d", self.caps)
        if off in self.kept_off:
            raise RuntimeError(f"{self.market}：日线的数据源 {SOURCES[off]} 在 设置 → 数据使用 里关了“本地存储”，不更新")

    def backfill(self, start: date, end: date | None = None, datasets: list[str] | None = None) -> Snapshot | None:
        """History over [start, end]; `datasets`: only these (financial history added after the bars, say) — then the
        snapshot's day stays the last day that has bars."""
        self._stored_here()
        end = min(end or self.today(), self.last_complete_day())
        self.fetch(start, end, ["calendar"])
        days = self.trading_days(start, end)
        if not days:
            raise RuntimeError(f"{self.market}: calendar has no trading days in {start}..{end}")
        self.fetch(start, end, [ds for ds in ORDER[1:] if datasets is None or ds in datasets])
        # as_of = the last day that really has bars (a run during trading hours has none for today).
        staged, latest = self.staging.collect("bars_1d"), self.store.latest()
        as_of = staged["date"].max() if staged.height else latest.as_of if datasets is not None and latest else days[-1]
        return self.publish(as_of=as_of, note=f"backfill {start}..{end}")

    def update(self, day: date | None = None, *, wait: bool = True,
               sleep: Callable[[float], None] = time.sleep) -> Snapshot | None:
        """Publish `day` and every trading day missed since the last snapshot (catch-up)."""
        self._stored_here()
        bars_source = self.provider_for("bars_1d")
        if bars_source is None or "bars_1d" not in bars_source.unit_kinds:
            raise RuntimeError(f"{self.market}: 没有可用的日线数据源（当前数据档和套餐里没有能提供日线的接口），"
                               "这不是数据延迟")
        day = day or self.today()
        latest = self.store.latest()
        start = min(latest.as_of + timedelta(days=1), day) if latest else day
        self.fetch(min(start, day - timedelta(days=30)), day + timedelta(days=CALENDAR_AHEAD), ["calendar"])
        cal = self._current("calendar")
        if cal.filter(pl.col("date") == day).is_empty():
            raise RuntimeError(f"{self.market}: the calendar has no entry for {day} (unknown is not closed; it ends at "
                               f"{cal['date'].max()}); extend the calendar source before updating")
        if day not in self.trading_days(day, day):
            self.log(f"{self.market} {day} is not a trading day")
            if latest and (fresh := self.missing_history()):   # what the catalog gained: its history on a day off too (10-17)
                first = self.store.scan("bars_1d").select(pl.col("date").min()).collect().item()
                self.fetch(first or latest.as_of, latest.as_of, fresh)
                return self.publish(as_of=latest.as_of, note=f"history {'、'.join(fresh)}")
            return self.publish(as_of=latest.as_of if latest else day, note="calendar")
        waiting = wait and day == self.today()  # a past day is final: nothing to wait for
        if day > self.last_complete_day() and not waiting:
            raise DataDelayed(f"{self.market} {day}: the session has not finished yet")
        if waiting:
            prov = self.provider_for("bars_1d")
            inst = self._current("instruments")
            ok = wait_ready(lambda: prov.ready_count(day, inst), load_market(self.market),
                            now=self.clock, sleep=sleep, log=self.log)
            if not ok:
                events.publish("data.delayed", {"market": self.market, "date": day})
                raise DataDelayed(f"{self.market} {day}: data not ready by the end of the window")
        days = self.trading_days(start, day)
        if len(days) > 1:
            self.log(f"{self.market}: catching up {len(days)} trading days {days[0]}..{day}")
        weekly = WEEKLY if day.weekday() == 0 else [ds for ds in WEEKLY if self._behind_catalog(ds, latest)]
        fresh = [ds for ds in weekly + HISTORY if not (latest and latest.manifest.get(ds))]
        self.fetch(start, day, [ds for ds in DAILY[1:] + weekly if ds not in fresh])
        if fresh:  # added to the catalog after the backfill (index members, money flow): its whole history, not only these days
            first = self.store.scan("bars_1d").select(pl.col("date").min()).collect().item() if latest else None
            self.fetch(first or start, day, fresh)
        have = set(self.staging.collect("bars_1d")["date"].to_list())
        missing = [d for d in days if d not in have]
        if missing:
            # Never publish a snapshot that claims a day without its bars, and do not let the empty
            # answer stick: forget it so the next run asks the source again.
            prov = self.provider_for("bars_1d")
            if prov is not None:
                if prov.unit_kinds.get("bars_1d") == "date":
                    for d in missing:
                        self.staging.forget(prov.name, "bars_1d", d.isoformat())
                else:
                    self.staging.forget(prov.name, "bars_1d")
            events.publish("data.delayed", {"market": self.market, "date": day})
            raise DataDelayed(f"{self.market}: source returned no bars for {', '.join(map(str, missing))}")
        return self.publish(as_of=day, note=f"update {day}")

    def missing_history(self) -> list[str]:
        """The HISTORY datasets this market's sources give that the latest snapshot lacks (the catalog gained them): their
        whole history comes with the next update, a day off's too. Asked on every tick of a day off: by the routes and the
        probes only, no source made (that reads its key)."""
        latest, out = self.store.latest(), []
        for ds in HISTORY:
            name = route(self.paths.profile, self.market, ds, self.caps)
            if (latest and latest.manifest.get(ds)) or name is None or name in self.kept_off or ds in self.unavailable:
                continue
            if (prov := self.providers.get(name)) is not None and ds not in prov.unit_kinds:   # one already made lacks it
                continue
            out.append(ds)
        return out

    def incomplete(self, day: date) -> list[str]:
        """Datasets fetched by the day whose rows for `day` in the latest snapshot fall short of its traded listings
        (READY_SHARE of the day's bars): they come later than the bars (spec §4.5 按数据集分别判定就绪)."""
        latest = self.store.latest()
        if latest is None:
            return []
        traded = self.store.scan("bars_1d", latest).filter(pl.col("date") == day).select(pl.len()).collect().item()
        out = []
        for ds in LATER:
            prov = self.provider_for(ds)
            if not traded or prov is None or prov.unit_kinds.get(ds) != "date":
                continue
            have = self.store.scan(ds, latest).filter(pl.col("date") == day).select(pl.len()).collect().item()
            if have < READY_SHARE * traded:
                out.append(ds)
        return out

    def complete(self, day: date, datasets: list[str]) -> Snapshot | None:
        """Ask again for `day` of datasets that came late; publish once they are there (else nothing, ask later)."""
        latest = self.store.latest()
        todo = [ds for ds in datasets if (prov := self.provider_for(ds)) is not None]
        for ds in todo:
            self.staging.forget(self.provider_for(ds).name, ds, day.isoformat())
        self.fetch(day, day, todo)
        traded = self.store.scan("bars_1d", latest).filter(pl.col("date") == day).select(pl.len()).collect().item()
        short = [ds for ds in todo if self.staging.collect(ds).filter(pl.col("date") == day).height < READY_SHARE * traded]
        if short:  # not yet: forget what came so the next try asks the source again
            for ds in todo:
                self.staging.forget(self.provider_for(ds).name, ds, day.isoformat())
            return None
        return self.publish(as_of=latest.as_of, note=f"{'、'.join(todo)} {day}")

    def refetch(self, day: date) -> Snapshot | None:
        """Re-download one day (spec §4.5, next morning 8:30) and publish only if the source corrected
        something: bars, corporate actions, valuation or index rows, in any column it provides — or added a financial
        statement of that disclosure day (a report-period source re-reads its periods every day instead)."""
        if self.staging.pending():
            raise RuntimeError(f"{self.market}: {self.staging.pending()} fetched units are not published yet; "
                               "rerun the interrupted backfill/update first")
        if self.store.latest() is None:
            raise RuntimeError(f"{self.market}: 还没有快照，没有可订正的数据；先补历史（数据页的「补历史」）")
        self._stored_here()
        self.fetch(day, day, [ds for ds in ("corp_actions", "bars_1d", "valuation", "index_bars_1d", "financials")
                              if (p := self.provider_for(ds)) is not None and p.unit_kinds.get(ds) not in (None, "period")])
        changed = {}
        for name in DATASETS:
            new = self.staging.collect(name)
            if new.height and (n := self._changed(name, new)):
                changed[name] = n
        if not changed:
            self.staging.clear()
            return None
        rows = sum(changed.values())
        snap = self.publish(as_of=self.store.latest().as_of,
                            note=f"订正 {day}: " + "、".join(f"{k} {v} 行" for k, v in changed.items()))
        events.publish("data.corrected", {"market": self.market, "date": day, "rows": rows,
                                          "datasets": changed, "snapshot_id": snap.id if snap else None})
        return snap

    def _changed(self, name: str, new: pl.DataFrame) -> int:
        """Rows of `new` that differ from the published ones in a column the source provided (all-null
        columns are derived later, e.g. JP adj_factor, and are not compared)."""
        key = list(DATASETS[name].key)
        old = self.store.scan(name).join(new.select(key).lazy(), on=key, how="semi").collect()
        cols = [c for c in new.columns if c in old.columns and new[c].null_count() < new.height]
        return new.select(cols).join(old.select(cols), on=cols, how="anti", nulls_equal=True).height
