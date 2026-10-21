"""Local HTTP API (spec §3.1): FastAPI on 127.0.0.1, every /api request carries the session token.
The renderer never talks to data sources directly; it only reads what the backend computed."""
import hmac
import logging
import sqlite3
import threading
from bisect import bisect_right
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from wb import __version__, registry
from wb.ai.client import AIError, AIUnavailable
from wb.api.ai import add_routes as add_ai_routes
from wb.api.context import Context
from wb.api.jobs import add_routes as add_job_routes
from wb.api.indicators import add_routes as add_indicator_routes
from wb.api.labels import add_routes as add_label_routes
from wb.api.live import add_routes as add_live_routes
from wb.api.market import add_routes as add_market_routes
from wb.api.pa import add_routes as add_pa_routes
from wb.pa import records as pa_records
from wb.api.research import add_routes as add_research_routes
from wb.api.rules import add_routes as add_rule_routes
from wb.api.validate import add_routes as add_validate_routes
from wb.api.danger import add_routes as add_danger_routes
from wb.api.watch import add_routes as add_watch_routes
from wb.charts.series import BASES, MINUTE_DAYS, MINUTE_HISTORY, MINUTE_PERIODS, PERIODS, build, index_series, minute_series, page
from wb.data.capability import last_errors, load_caps
from wb.datause import SOURCES, USES, allowed, source_of
from wb.engine.l3.rule import RuleError
from wb.jobs.daily import Daily
from wb.jobs.live import Live
from wb.listing import BadListingId, parse_id
from wb.markets import MARKETS, default_index  # noqa: F401 - default_index is part of this module's API
from wb.notify.feishu import check_webhook
from wb.realtime.stream import Broadcaster
from wb.secrets import secret_source, set_secret
from wb.workspace.search import search as run_search
from wb.workspace.watch import Watch

VERSION = __version__   # the version running (the four version spots agree; until 0.2.1 this said 0.1.0 always)
SECRETS = ("tushare_token", "jquants_api_key", "deepseek_api_key", "openrouter_api_key", "feishu_webhook", "feishu_secret",
           "tickflow_api_key", "tachibana_auth_id", "tachibana_key_file", "tachibana_url", "tachibana_key_pass")


def _check_tachibana_url(v: str) -> str:
    from wb.realtime.providers.tachibana import TachibanaError, check_url
    try:
        return check_url(v)
    except TachibanaError as e:
        raise ValueError(str(e)) from None


def _check_key_file(v: str) -> str:
    if not Path(v.strip()).expanduser().is_file():
        raise ValueError("找不到这个私钥文件：填它在本机的完整路径（如 ~/.auth/e_api_private_key.pem）")
    return v.strip()


# a value that cannot work is refused before it is stored
CHECKS = {"feishu_webhook": check_webhook, "tachibana_url": _check_tachibana_url, "tachibana_key_file": _check_key_file}


def create_app(root: str | Path | None = None, token: str | None = None,
               frontend_dist: str | Path | None = None, scheduler: bool = False) -> FastAPI:
    """`scheduler`: run the daily cycle (§4.5, §7.1) in this process — `wb serve` (the desktop app) does; tests do not."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if scheduler:
            app.state.daily.start()
            app.state.live.start()
        yield
        app.state.daily.stop()
        app.state.live.stop()

    app = FastAPI(title="stock workbench", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)
    # spec §3.5: every registry fully loaded at start, its size logged and reported by /api/health
    logging.getLogger("wb").info("registries %s", registry.load_all())
    ctx = Context(root)
    app.state.ctx = ctx
    pa_records.recover(ctx.paths("CN"))   # 价格行为分析: one still running was cut off by this restart
    app.state.daily = Daily(ctx.paths)
    feed = Broadcaster()
    app.state.live = Live(ctx.paths, publish=feed.publish,   # 盘中 (spec §8.5): polls while a market trades
                          send=lambda: threading.Thread(target=app.state.daily.notify, args=(datetime.now().astimezone(),),
                                                        name="notify", daemon=True).start())

    @app.exception_handler(BadListingId)
    @app.exception_handler(RuleError)  # e.g. "JP 还没有数据快照" from a sample: the reason, never a bare 500
    def bad_request(request: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(sqlite3.OperationalError)   # a full disk: said as such, not "Internal Server Error"
    def database_error(request: Request, e: sqlite3.OperationalError):
        logging.getLogger("wb").error("%s %s: %s", request.method, request.url.path, e, exc_info=e)
        if "disk is full" in str(e):
            return JSONResponse({"detail": "磁盘满了：存不下。腾出一些空间后再试（数据目录所在的盘）"}, status_code=507)
        return JSONResponse({"detail": f"数据库出错：{e}"}, status_code=500)

    @app.exception_handler(AIUnavailable)  # no key, over budget, offline: AI waits, nothing else is affected
    def ai_unavailable(request: Request, e: AIUnavailable):
        return JSONResponse({"detail": str(e)}, status_code=503)

    @app.exception_handler(AIError)
    def ai_error(request: Request, e: AIError):
        return JSONResponse({"detail": str(e), "raw": e.raw}, status_code=502)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if token and path.startswith("/api/") and path != "/api/health":
            supplied = request.headers.get("x-wb-token") or request.cookies.get("wb_token") or ""
            if not hmac.compare_digest(supplied.encode(), token.encode()):  # bytes: any header text is just wrong
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ---- system --------------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "registries": registry.counts()}

    @app.get("/api/status")
    def status():
        ws = ctx.workspace()
        out = []
        for m in MARKETS:
            store = ctx.store(m)
            snap = store.latest()
            q = store.conn.execute(
                "SELECT severity, COUNT(*) FROM quality_issue WHERE snapshot_id=? GROUP BY severity",
                (snap.id,)).fetchall() if snap else []
            cc = store.conn.execute(
                "SELECT COUNT(*), SUM(ok) FROM crosscheck WHERE snapshot_id=(SELECT snapshot_id FROM crosscheck "
                "ORDER BY checked_at DESC LIMIT 1)").fetchone()
            pending = store.conn.execute("SELECT COUNT(*) FROM fetch_log WHERE market=?", (m,)).fetchone()[0]
            sess = app.state.live.session(m, datetime.now().astimezone())
            out.append({
                "market": m, "profile": ws.profile(m),
                "snapshot": {"id": snap.id, "as_of": str(snap.as_of), "created_at": snap.created_at,
                             "note": snap.note} if snap else None,
                "quality": {r[0]: r[1] for r in q},
                "crosscheck": {"checked": cc[0] or 0, "ok": cc[1] or 0},
                "pending_units": pending,
                "session": {"state": sess.state, "label": sess.label},  # where the market is in its day (盯盘)
            })
        return {"markets": out}

    @app.get("/api/capabilities")
    def capabilities(market: str = Query(..., pattern="^(CN|JP)$")):
        store = ctx.store(market)
        caps, errors = load_caps(store.conn, market), last_errors(store.conn, market)   # §4.9: with the last fetch error
        return [{"provider": p, "dataset": ds, "available": c.available,
                 "history_start": str(c.history_start) if c.history_start else None,
                 "latest_date": str(c.latest_date) if c.latest_date else None,
                 "rate_per_min": c.rate_per_min, "detail": c.detail,
                 **{f"last_error{k}": (errors.get((p, ds)) or {}).get(f) for k, f in (("", "error"), ("_unit", "unit"), ("_at", "at"))}}
                for (p, ds), c in sorted(caps.items())]

    # ---- securities ----------------------------------------------------------------------
    @app.get("/api/search")
    def search(q: str = "", limit: int = Query(20, ge=1, le=100)):
        return run_search(ctx.search_index(), q, limit)

    @app.get("/api/instrument/{listing_id}")
    def instrument(listing_id: str):
        market, _ = parse_id(listing_id)
        rows = ctx.instruments(market).filter(pl.col("listing_id") == listing_id)
        if rows.is_empty():
            raise HTTPException(404, f"{listing_id} not found")
        row = {k: (str(v) if isinstance(v, date) else v) for k, v in rows.row(0, named=True).items()}
        ws = ctx.workspace()
        store = ctx.store(market)
        # Quality issues of this security (spec §4.6): the chart warns instead of silently showing numbers built on
        # suspect data — every row of the current snapshot the rules read as unknown (its whole history: a daily
        # snapshot records only its own day's issues, so a row found bad at the backfill would stop warning the next
        # day though nothing corrected it), and the current snapshot's other findings (warnings) beside them
        snap = store.latest()
        issues = []
        if snap is not None:
            from wb.engine.l1.frame import quality_rows
            flags = quality_rows(store, snap).filter(pl.col("listing_id") == listing_id).explode("quality", empty_as_null=False)
            issues = [{"check_name": c, "n": n, "first": str(a), "last": str(b)} for c, n, a, b in flags.group_by("quality").agg(
                pl.len().alias("n"), pl.col("date").min().alias("a"), pl.col("date").max().alias("b")).sort(["n", "quality"], descending=[True, False]).iter_rows()]
            seen = {i["check_name"] for i in issues}
            issues += [dict(r) for r in store.conn.execute(
                "SELECT check_name, COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last FROM quality_issue "
                "WHERE listing_id=? AND snapshot_id=? GROUP BY check_name ORDER BY n DESC", (listing_id, snap.id))
                if r["check_name"] not in seen]
        # its industry in the app's one scheme (申万 一级 · 二级; 日股 33 業種) — `industry` above is the source's own label
        from wb.market.industry import at
        here = at(store, snap, market, snap.as_of).filter(pl.col("listing_id") == listing_id) if snap else None
        l1, l2 = (here["l1"][0], here["l2"][0]) if here is not None and here.height else (None, None)
        return {**row, "index_id": default_index(listing_id, ws.profile(market)),
                "watch_groups": ws.groups_of(listing_id), "quality": issues, "ind_l1": l1, "ind_l2": l2}

    def chart_series(listing_id: str, period: str, basis: str, live: bool, end: date | None = None):
        """(snapshot, the chart's series or None, today's forming bar or None): the bars and indicators the K 线 draws,
        cached per snapshot and forming bar — the indicator library computes on the same series (wb/indicators)."""
        if period not in PERIODS or basis not in BASES:
            raise HTTPException(422, "bad period or basis")
        market, _ = parse_id(listing_id)
        snap, daily = ctx.daily(listing_id)
        if snap is None or daily.is_empty():
            return snap, None, None
        if live and end is None:
            app.state.live.view(listing_id)   # any stock on the K-line page is polled first while you look (design 2026-10-12 §9)
        temp = app.state.live.temp_day(listing_id) if live and end is None else None
        if temp is not None:
            daily = pl.concat([daily, pl.DataFrame([temp])], how="diagonal_relaxed")
        series = ctx.misc_cache.get(("series", snap.id, listing_id, period, basis, tuple(temp.values()) if temp else None), lambda: build(
            daily, listing_id, market, period, basis, ctx.calendar_open(market)))
        return snap, series, temp
    app.state.chart_series = chart_series

    @app.get("/api/bars")
    def bars(listing_id: str, period: str = Query("day"), basis: str = Query("qfq"),
             end: date | None = None, limit: int = Query(500, ge=1, le=5000), live: bool = False):
        """`live`: end with today's bar still forming (spec §8.5), `temp`, its indicators counting it. A minute period
        (MINUTE_PERIODS): the newest `limit` of its last MINUTE_DAYS days of n-minute bars (`end` unused, never `more`)."""
        if period in MINUTE_PERIODS:
            return minute_bars_of(listing_id, period, basis, live, limit)
        snap, series, temp = chart_series(listing_id, period, basis, live, end)
        if series is None:
            return {"listing_id": listing_id, "snapshot_id": snap.id if snap else None, "rows": [],
                    "more": False, "adjusted": False, "period": period, "basis": basis, "scales": []}
        rows, more = page(series, end, limit)
        for r in rows:
            r["temp"] = temp is not None and r["date"] >= temp["date"].isoformat()
        return {"listing_id": listing_id, "snapshot_id": snap.id, "period": period, "basis": basis,
                "adjusted": series.adjusted, "rows": rows, "more": more, "scales": series.scales}

    def minute_series_of(listing_id: str, period: str, basis: str, live: bool, days: int | None = None):
        """(snapshot, the minute K-line's series or None, the daily chart's series, the session): the stock's kept minutes
        (Live.minutes_of) of its last `days` days (its period's MINUTE_DAYS) cut into n-minute bars, each day's prices in
        the daily chart's basis by that day's scale; `live`: today's too. Without `live`, the days the daily chart has."""
        snap, day, _ = chart_series(listing_id, "day", basis, live)   # polls the stock while you look, as the daily chart
        if day is None:
            return snap, None, None, None
        market, lv = parse_id(listing_id)[0], app.state.live
        s = lv.session(market, datetime.now().astimezone())
        m = lv.minutes_of(listing_id, days or MINUTE_HISTORY, s.now)   # its indicators warm up on the days before (10-13)
        if not live:
            m = m.filter(pl.col("at").dt.date() <= snap.as_of)
        changes = [(date.fromisoformat(d), k) for d, k in day.scales]   # the daily chart's (date, scale) change points
        scales = {d: changes[max(bisect_right([c[0] for c in changes], d) - 1, 0)][1] for d in m["at"].dt.date().unique()}
        return snap, minute_series(m, listing_id, market, period, basis, day.adjusted, scales), day, s
    app.state.minute_series = minute_series_of

    def minute_bars_of(listing_id: str, period: str, basis: str, live: bool, limit: int) -> dict:
        """The minute K-line (10-12): minute_series_of's bars, the newest `limit`; with `live`, the bar not yet ended
        `temp` — it moves with the price."""
        snap, series, day, s = minute_series_of(listing_id, period, basis, live)
        out = {"listing_id": listing_id, "snapshot_id": snap.id if snap else None, "period": period, "basis": basis,
               "rows": [], "more": False, "adjusted": False, "scales": []}
        if series is None:
            return out
        shown = series.frame["date"].dt.date().unique().sort().tail(MINUTE_DAYS[period])   # its period's days, worked out on more
        series = replace(series, frame=series.frame.filter(pl.col("date").dt.date().is_in(shown.implode())))
        rows, _ = page(series, None, limit)
        now = s.now.strftime("%Y-%m-%dT%H:%M")
        for r in rows:
            r["temp"] = live and s.state == "trading" and r["date"] > now
        return {**out, "rows": rows, "adjusted": day.adjusted}

    @app.get("/api/index_bars")
    def index_bars(index_id: str, period: str = Query("day"), end: date | None = None,
                   limit: int = Query(500, ge=1, le=5000)):
        market = index_id.split(":")[0]
        if market not in MARKETS or period not in PERIODS:
            raise HTTPException(422, "bad index id or period")
        store = ctx.store(market)
        snap = store.latest()
        if snap is None:
            return {"index_id": index_id, "rows": [], "more": False}
        series = ctx.misc_cache.get(("index", snap.id, index_id, period), lambda: index_series(
            store.read("index_bars_1d", snap).filter(pl.col("index_id") == index_id), index_id, period,
            ctx.calendar_open(market)))
        rows, more = page(series, end, limit)
        return {"index_id": index_id, "rows": rows, "more": more}

    # ---- workspace -----------------------------------------------------------------------
    def _quotes(listing_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for m in MARKETS:
            ids = [i for i in listing_ids if i.startswith(m + ":")]
            store = ctx.store(m)
            snap = store.latest()
            if not ids or snap is None:
                continue
            recent = (store.scan("bars_1d", snap)
                      .filter(pl.col("listing_id").is_in(ids) & (pl.col("date") >= snap.as_of - timedelta(days=20))
                              & pl.col("close").is_not_null())
                      .sort("date").group_by("listing_id").tail(2).collect())
            inst = ctx.instruments(m).filter(pl.col("listing_id").is_in(ids)).select("listing_id", "name")
            names = dict(zip(inst["listing_id"], inst["name"], strict=True))
            for lid, g in recent.group_by("listing_id"):
                closes = g.sort("date")["close"].to_list()
                pre = g.sort("date")["pre_close"].to_list()[-1]
                base = pre if pre is not None else (closes[-2] if len(closes) > 1 else None)
                out[lid[0]] = {"close": closes[-1], "change": (closes[-1] / base - 1) if base else None,
                               "date": str(g["date"].max())}
            for lid in ids:
                out.setdefault(lid, {})["name"] = names.get(lid)
            # in the session, until the day is published: the day's whole-market quote — what the K-line's forming bar
            # reads (Live.temp_day) — its change against the quote's own close before; not traded yet: the daily close
            s = app.state.live.session(m, datetime.now().astimezone())
            day = app.state.live._snapshot(m, s.day) if s.state != "closed" and snap.as_of < s.day else None
            for r in (day.filter(pl.col("listing_id").is_in(ids)).iter_rows(named=True) if day is not None else []):
                out[r["listing_id"]].update(close=r["last"], date=str(s.day), at=r["at"].isoformat(timespec="seconds"),
                                            change=r["last"] / r["pre_close"] - 1 if r["pre_close"] else None)
        return out

    @app.get("/api/watchlist")
    def watchlist():
        ws = ctx.workspace()
        groups = ws.groups()
        items = {g["id"]: ws.items(g["id"]) for g in groups}
        quotes = _quotes(sorted({i["listing_id"] for its in items.values() for i in its}))
        return [{**g, "items": [{**i, **quotes.get(i["listing_id"], {})} for i in items[g["id"]]]} for g in groups]

    @app.post("/api/watchlist/groups")
    def add_group(name: str = Body(..., embed=True)):
        if not (name := name.strip()):
            raise HTTPException(422, "分组要有个名字")
        try:
            return {"id": ctx.workspace().add_group(name)}
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"分组 {name} 已经有了") from None

    def unwatched(lids: list[str]) -> None:
        """A stock in no group any more is no longer watched: its entry is closed (spec §11)."""
        ws = ctx.workspace()
        for lid in lids:
            if not ws.groups_of(lid):
                Watch(ctx.paths).close(lid)

    @app.delete("/api/watchlist/groups/{group_id}")
    def delete_group(group_id: int):
        ws = ctx.workspace()
        lids = [i["listing_id"] for i in ws.items(group_id)]
        ws.delete_group(group_id)
        unwatched(lids)
        return {"ok": True}

    @app.post("/api/watchlist/{group_id}/items")
    def add_item(group_id: int, listing_id: str | None = Body(None, embed=True), listing_ids: list[str] | None = Body(None, embed=True),
                 source: str = Body("", embed=True)):
        """One stock into a group, or many at once (`listing_ids`: 选股's 全选 → 加入观察) — each watched, its 观察条目
        (spec §11)."""
        if all(g["id"] != group_id for g in ctx.workspace().groups()):   # deleted meanwhile (another window): said, not a 500
            raise HTTPException(404, f"没有这个分组（{group_id}）：可能刚被删了")
        if listing_ids is None:
            if listing_id is None:
                raise HTTPException(422, "要写 listing_id 或 listing_ids")
            parse_id(listing_id)
            ctx.workspace().add_item(group_id, listing_id)
            Watch(ctx.paths).add(listing_id, source=source)
            return {"ok": True}
        for lid in listing_ids:
            parse_id(lid)
        ws = ctx.workspace()
        for lid in listing_ids:
            ws.add_item(group_id, lid)
        return {"ok": True, "added": Watch(ctx.paths).add_many(listing_ids, source=source)}

    @app.delete("/api/watchlist/{group_id}/items/{listing_id}")
    def remove_item(group_id: int, listing_id: str):
        ctx.workspace().remove_item(group_id, listing_id)
        unwatched([listing_id])
        return {"ok": True}

    @app.get("/api/recent")
    def recent():
        ids = ctx.workspace().recent()
        quotes = _quotes(ids)
        return [{"listing_id": i, **quotes.get(i, {})} for i in ids]

    @app.post("/api/recent")
    def touch_recent(listing_id: str = Body(..., embed=True)):
        parse_id(listing_id)
        ctx.workspace().touch_recent(listing_id)
        return {"ok": True}

    @app.get("/api/drawings")
    def drawings(listing_id: str):
        return ctx.workspace().drawings(listing_id)

    @app.put("/api/drawings/{drawing_id}")
    def save_drawing(drawing_id: str, body: dict = Body(...)):
        try:
            ctx.workspace().save_drawing(body["listing_id"], body["kind"], body["points"], body.get("styles"),
                                         bool(body.get("alert", False)), drawing_id)
        except (KeyError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        return {"id": drawing_id}

    @app.delete("/api/drawings/{drawing_id}")
    def delete_drawing(drawing_id: str):
        ctx.workspace().delete_drawing(drawing_id)
        return {"ok": True}

    @app.get("/api/settings")
    def settings():
        return ctx.workspace().settings()

    @app.get("/api/datause")
    def datause():
        """数据使用开关 (spec §4.10): each data source's four uses (changed through /api/settings), and where each
        market's prices come from in its data profile now."""
        ws = ctx.workspace()
        s = ws.settings()
        return {"sources": [{"id": k, "name": v, **{u: allowed(s, k, u) for u in USES}} for k, v in SOURCES.items()],
                "markets": {m: source_of(m, ws.profile(m)) for m in MARKETS}}

    @app.put("/api/settings")
    def put_setting(key: str = Body(...), value: object = Body(...)):
        market = key.removeprefix("profile.")
        if market in MARKETS and app.state.daily.running(market):   # its update or scan would go on in the other profile
            raise HTTPException(409, f"{'A 股' if market == 'CN' else '日股'}正在更新或扫描：等它做完（或在今日页取消）再换数据档")
        try:
            ctx.workspace().set_setting(key, value)
        except (KeyError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
        return ctx.workspace().settings()

    @app.get("/api/update")
    def update_check(force: bool = False):
        """检查更新: the newest published release against the version running (kept 6 hours; force: now)."""
        from wb.update import check
        return check(force=force)

    @app.get("/api/update/download")
    def update_download_state():
        """下载并安装: how far the download of the new version's installer is (the app runs it once it is ready)."""
        from wb.update import download_state
        return download_state()

    @app.post("/api/update/download")
    def update_download():
        """Download the newest release's installer for this system and check it against the release's SHA256SUMS.txt
        (spec §3.3, 1.51): the installed app only."""
        from wb.update import download, installable
        if not installable():
            raise HTTPException(409, "从仓库运行的开发版不自己安装：git pull 后重新打开就是新版本")
        return download()

    @app.get("/api/secrets")
    def secrets():
        """Where each key comes from; values never leave the backend (spec §3.2)."""
        return {n: secret_source(n) for n in SECRETS}

    @app.put("/api/secrets/{name}")
    def put_secret(name: str, value: str = Body(..., embed=True)):
        if name not in SECRETS or not value.strip():
            raise HTTPException(422, "unknown secret or empty value")
        try:
            value = CHECKS.get(name, str.strip)(value)
            set_secret(name, value.strip())   # where it is read from: the .env file's line, else the keychain
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {n: secret_source(n) for n in SECRETS}

    # ---- data quality --------------------------------------------------------------------
    @app.get("/api/data/{market}/snapshots")
    def snapshots(market: str, limit: int = 30):
        if market not in MARKETS:
            raise HTTPException(404, market)
        hist = ctx.store(market).history()[-limit:]
        return [{"id": s.id, "as_of": str(s.as_of), "created_at": s.created_at, "note": s.note,
                 "datasets": sorted(s.manifest)} for s in reversed(hist)]

    @app.get("/api/data/{market}/quality")
    def quality(market: str, snapshot_id: str | None = None):
        if market not in MARKETS:
            raise HTTPException(404, market)
        store = ctx.store(market)
        sid = snapshot_id or (store.latest().id if store.latest() else None)
        rows = store.conn.execute(
            "SELECT dataset, check_name, severity, listing_id, date, detail FROM quality_issue WHERE snapshot_id=? "
            "ORDER BY severity, check_name LIMIT 500", (sid,)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/data/{market}/crosscheck")
    def crosscheck(market: str):
        if market not in MARKETS:
            raise HTTPException(404, market)
        store = ctx.store(market)
        last = store.conn.execute("SELECT snapshot_id FROM crosscheck ORDER BY checked_at DESC LIMIT 1").fetchone()
        if not last:
            return {"snapshot_id": None, "summary": [], "mismatches": []}
        sid = last[0]
        summary = store.conn.execute(
            "SELECT reference, field, COUNT(*) AS n, SUM(ok) AS ok FROM crosscheck WHERE snapshot_id=? "
            "GROUP BY reference, field ORDER BY reference, field", (sid,)).fetchall()
        bad = store.conn.execute(
            "SELECT reference, listing_id, date, field, stored, ref_value FROM crosscheck WHERE snapshot_id=? AND ok=0 "
            "LIMIT 200", (sid,)).fetchall()
        return {"snapshot_id": sid, "summary": [dict(r) for r in summary], "mismatches": [dict(r) for r in bad]}

    add_rule_routes(app, ctx)  # rules, structures, preview, runs, gallery
    add_indicator_routes(app, ctx)  # 指标库 (design 2026-10-10)
    add_label_routes(app, ctx)  # the labelling tool (spec §5.10)
    add_research_routes(app, ctx)  # 个股研究 and its record (spec §10.3, §10.4)
    add_pa_routes(app, ctx)  # 价格行为分析 (design 2026-10-20, dev branch)
    add_ai_routes(app, ctx)  # AI settings, status, the call log (spec §10.1)
    add_job_routes(app, ctx)  # the daily cycle, the 今日 feed, notifications (spec §7.1)
    add_market_routes(app, ctx, app.state.live)  # the 市场 page (spec §7.3, design 2026-10-12) and 题材 tags
    add_watch_routes(app, ctx)  # 观察条目 (spec §11)
    add_danger_routes(app, ctx)  # 危险提醒 (design 2026-10-15)
    add_validate_routes(app, ctx)  # 验证 (spec §9)
    add_live_routes(app, ctx, app.state.live, feed)  # 盘中 (spec §8.5)

    if frontend_dist and Path(frontend_dist).is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app
