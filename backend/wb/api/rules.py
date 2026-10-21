"""The API behind rules (spec §5.2, §5.9), the chart's structures and left pressure (§8.2), the
single-stock preview and the sample gallery (§6.3), and market runs (§5.5).

A preview is kept per (snapshot, listing, rule content with its slider values), so clicking bars
asks the same matcher for each day's explanation. Values that JSON cannot carry (an infinite space
when nothing lies above, NaN) go out as null; the explanation text renders them first."""
import json
import math
import threading
from datetime import date, timedelta

import polars as pl
from fastapi import Body, FastAPI, HTTPException, Query

from wb.api.context import LRU, Context
from wb.charts.series import BASES
from wb.charts.structures import factors, overlay
from wb.engine.l2.box import SHARED
from wb.engine.l2.more import left_pressure
from wb.engine.l2.periods import RANGE, bars_of, minute, on_bars, restamp, settings, tick_of
from wb.realtime.micro import DAYS as MICRO_DAYS
from wb.engine.l2.run import l2_bars
from wb.engine.l1.frame import index_membership
from wb.engine.l2.spec import compute
from wb.engine.l3.explain import render
from wb.engine.l3.match import compile_errors
from wb.engine.l3.calibrate import prepare
from wb.engine.l3.preview import gallery, missed, preview, sample
from wb.engine.l3.rule import Rule, RuleError, json_schema, load_rule, rule_hash
from wb.engine.l3.run import RUN_LOCK, run_rows, run_rule, runs
from wb.engine.l3.understand import names, price_mas, understand
from wb.listing import parse_id
from wb.markets import MARKETS
from wb.workspace.labels import HitLabels
from wb.workspace.rules import RuleCannotRun, RuleExists, Rules, builtin_rules, templates

BUSY = "已经有全市场的计算在进行（每日扫描、全市场运行、校准、验证，或要读全市场的截面规则），等它结束再试"
EMPTY = {"boxes": [], "slant": [], "pivots": [], "swings": [], "gold": [], "false_bottoms": []}


def _int(v, name: str, default: int, lo: int = 1) -> int:
    """A whole number from a request body (422 otherwise)."""
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise HTTPException(422, f"{name} 要写整数")
    try:
        n = int(v)
    except ValueError:
        raise HTTPException(422, f"{name} 要写整数") from None
    if n != float(v) or n < lo:
        raise HTTPException(422, f"{name} 要写不小于 {lo} 的整数")
    return n


def clean(v):
    """JSON-safe: non-finite floats become null."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    return v


def _day(v) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        raise HTTPException(422, f"日期要写成 YYYY-MM-DD：{v}") from None


def add_routes(app: FastAPI, ctx: Context) -> None:
    l2 = LRU(16)        # (snapshot, listing) -> (tables, l2 bars) with the primitives' default settings
    weeks = LRU(16)     # (snapshot, listing, forming bar, period) -> (tables, the period's bars): a week's or a month's
    previews = LRU(8)   # (snapshot, listing, rule hash) -> (Preview, lock)

    def rules() -> Rules:
        return Rules(ctx.paths(MARKETS[0]))  # shared.sqlite is common to every profile

    def resolve(body: dict) -> tuple[Rule, int]:
        if not isinstance(body, dict):
            raise HTTPException(422, "请求要写成 {rule_id 或 text, ...}")
        if body.get("text") is not None and not isinstance(body["text"], str):
            raise HTTPException(422, "text 要写规则原文（YAML 或 JSON 文字）")
        if body.get("params") is not None and not isinstance(body["params"], dict):
            raise HTTPException(422, "params 要写成 {参数名: 值}")
        try:
            if body.get("text"):
                rule, version = load_rule(body["text"]), 0
            elif body.get("rule_id"):
                rule, _, version = rules().get(body["rule_id"], body.get("version"))
            else:
                raise HTTPException(422, "要给 rule_id 或 text")
            if body.get("params"):
                rule = rule.with_params(body["params"])
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        return rule, version

    def structures_of(listing_id: str, temp: dict | None = None):
        """`temp`: today's bar still forming, after the stored ones (the structures as they stand now, spec §8.5)."""
        market, _ = parse_id(listing_id)
        snap, daily = ctx.daily(listing_id)
        if snap is None or daily.is_empty():
            return snap, daily, None, None
        extra = pl.DataFrame([{"listing_id": listing_id, **temp}]) if temp else None

        def make():
            bars = l2_bars(ctx.store(market), market, ctx.paths(market).profile, [listing_id], snap=snap, extra=extra)
            return compute(bars), bars
        tables, bars = l2.get((snap.id, listing_id, tuple(temp.values()) if temp else None), make)
        if temp:
            daily = pl.concat([daily, pl.DataFrame([temp])], how="diagonal_relaxed")
        return snap, daily, tables, bars

    # ---- structures ----------------------------------------------------------------------
    @app.get("/api/structures")
    def structures(listing_id: str, basis: str = Query("qfq"), live: bool = False, period: str = Query("day")):
        """`live`: with today's bar still forming counted (the K-line's 盘中未确认 layer). `period`: the structures of that
        chart's own bars (design 2026-10-12-structures-every-period) — `tol` the band they were found with (突破线 is the
        top × (1 + tol)); dates as the chart's bars have them (a minute bar's end, YYYY-MM-DDTHH:MM)."""
        if basis not in BASES:
            raise HTTPException(422, "bad basis")
        if period not in RANGE:
            raise HTTPException(422, f"周期只有 {'、'.join(RANGE)}")
        if minute(period):
            return minute_structures(listing_id, period, basis, live)
        temp = app.state.live.temp_day(listing_id) if live else None
        snap, daily, tables, bars = structures_of(listing_id, temp)
        head = {"listing_id": listing_id, "snapshot_id": snap.id if snap else None, "basis": basis, "period": period,
                "tol": SHARED.tol if period == "day" else settings(period)["BOX"]["tol"]}
        if tables is None:
            return {**head, **EMPTY}
        if period != "day":
            def make():
                pb = bars_of(bars, period)
                return compute(pb, params=settings(period)), pb
            tables, bars = weeks.get((snap.id, listing_id, tuple(temp.values()) if temp else None, period), make)
        return clean({**head, **overlay(tables, daily, basis, bars["date"].to_list())})

    def minute_structures(listing_id: str, period: str, basis: str, live: bool) -> dict:
        """A minute K-line's own structures: on its bars as the chart draws them (its basis; the kept days, as many as
        the 分时小箱 read), the band its period's, never under two ticks of the stock's price."""
        market, _ = parse_id(listing_id)
        snap, series, _, _ = app.state.minute_series(listing_id, period, basis, live, days=MICRO_DAYS)
        head = {"listing_id": listing_id, "snapshot_id": snap.id if snap else None, "basis": basis, "period": period}
        if series is None or series.frame.height < 5:
            return {**head, "tol": settings(period)["BOX"]["tol"], **EMPTY}
        frame = series.frame
        # the two-tick floor is in the exchange's ticks: the price as traded, whatever basis the chart shows (后复权 multiplies
        # it, and a tick read against it is too small) — the same price 盯盘's 分时小箱 read, so both find the same boxes
        raw = frame if basis == "raw" else app.state.minute_series(listing_id, period, "raw", live, days=MICRO_DAYS)[1].frame
        price = float(raw["close"].median())
        params = settings(period, price, tick_of(market, price))
        bars, days = on_bars(frame, listing_id)
        flat = pl.DataFrame({"date": days, "adj_factor": [None] * len(days)}, schema={"date": pl.Date, "adj_factor": pl.Float64})
        back = dict(zip(days, frame["date"].dt.strftime("%Y-%m-%dT%H:%M").to_list(), strict=True))
        return clean({**head, "tol": params["BOX"]["tol"], **restamp(overlay(compute(bars, params=params), flat, "hfq", days), back)})

    @app.get("/api/dates")
    def dates(listing_id: str):
        """The listing's daily bar dates (the replay slider's stops)."""
        _, daily = ctx.daily(listing_id)
        return [] if daily.is_empty() else [str(d) for d in daily.filter(pl.col("close").is_not_null())["date"]]

    @app.get("/api/pressure")
    def pressure(listing_id: str, day: str | None = None, basis: str = Query("qfq"),
                 scales: str = Query("mid,long"), lookback: int = Query(500, ge=1)):
        """Left pressure on a day (spec §5.4): prices in the display basis on that day."""
        if basis not in BASES:
            raise HTTPException(422, "bad basis")
        snap, daily, tables, bars = structures_of(listing_id)
        if tables is None:
            return {"date": None}
        when = _day(day) or bars["date"].max()
        boxes = tables["BOX"]["boxes"].filter(pl.col("scale").is_in(scales.split(",")))
        p = left_pressure(boxes, bars, pl.DataFrame({"listing_id": [listing_id], "date": [when]}), lookback)
        if p.is_empty():
            return {"date": str(when), "space": None, "none_above": False, "box_id": None}
        r = p.row(0, named=True)
        dates, f = factors(daily, basis)
        k = max(0, sum(1 for d in dates if d <= when) - 1)
        px = lambda v: None if v is None else v * f[k]  # noqa: E731
        box = (tables["BOX"]["boxes"].filter(pl.col("box_id") == r["pressure_box_id"]).select("start", "end").row(0, named=True)
               if r["pressure_box_id"] else {"start": None, "end": None})
        space = r["space"]
        return clean({"date": when, "box_id": r["pressure_box_id"], "start": box["start"], "end": box["end"],
                      "bottom": px(r["pressure_bottom"]), "top": px(r["pressure_top"]), "bars": r["pressure_bars"],
                      "heavy_down": r["pressure_heavy_down"], "space": space,
                      "none_above": space is not None and math.isinf(space),
                      "half_year_high": px(r["half_year_high"]), "one_year_high": px(r["one_year_high"]),
                      "space_half_year": r["space_half_year"], "space_one_year": r["space_one_year"]})

    # ---- rules ---------------------------------------------------------------------------
    @app.get("/api/rules")
    def rule_catalog():
        return rules().catalog()

    @app.get("/api/rules/schema")
    def rule_schema():
        return json_schema()

    @app.get("/api/rules/{rule_id}")
    def rule_get(rule_id: str, version: int | None = None):
        store = rules()
        try:
            rule, text, v = store.get(rule_id, version)
        except RuleError as e:
            raw = store.raw(rule_id, version)
            if raw is None:
                raise HTTPException(404, str(e)) from None
            # saved but no longer loads (e.g. a setting renamed since): its text comes back to be fixed
            return {"id": rule_id, "version": raw[1], "builtin": False, "text": raw[0], "versions": store.versions(rule_id),
                    "rule": None, "error": str(e)}
        builtin = any(r["id"] == rule_id and r["builtin"] for r in store.catalog())
        return {"id": rule.id, "version": v, "builtin": builtin, "text": text, "versions": store.versions(rule_id),
                "rule": rule.model_dump(mode="json", by_alias=True), "error": None}

    @app.post("/api/rules/check")
    def rule_check(text: str = Body(..., embed=True)):
        """Whether the text is a rule that runs; if it is, what it says it is (id, name, markets, parameters)
        and 我的理解 — the rule read back from the registry's own descriptions (spec §6.1)."""
        try:
            r = load_rule(text)
        except RuleError as e:
            return {"ok": False, "errors": str(e).split("\n")}
        if errors := compile_errors(r):   # every formula as the run compiles it: a ✓ here means it runs
            return {"ok": False, "errors": errors}
        dump = r.model_dump(mode="json", by_alias=True)
        return {"ok": True, "errors": [], "rule": {**{k: dump[k] for k in ("id", "name", "doc", "markets", "params", "stages")},
                                                   "names": names(r)},   # how its parts read: the sliders say where by them
                "understanding": understand(r), "warnings": unknown_names(r)}

    def unknown_names(r: Rule) -> list[str]:
        """A name in the pool (spec §7.1) that nothing carries — a typo — leaves the pool empty without a word:
        0 hits that read as "nothing qualifies". Compared as the formulas compare them (without case; a board by a
        part of its name)."""
        u, ws, out = r.universe, ctx.workspace(), []
        if u.watchlist and u.watchlist.lower() not in {g["name"].lower() for g in ws.groups()}:
            out.append(f"没有叫「{u.watchlist}」的自选分组")
        tags = {t["tag"].lower() for t in ws.all_tags()}
        out += [f"还没有股票打上「{t}」这个题材标签" for t in u.tags if t.lower() not in tags]
        industries, boards = set(), set()
        for m in r.markets:
            if (snap := ctx.latest(m)) is None:
                continue
            inst = ctx.instruments(m)
            if m == "CN":   # the pool reads 申万一级 (IN_INDUSTRY): the instruments list's own label is Tushare's, never matched
                industries |= ({x.lower() for x in ctx.store(m).scan("industry", snap).select("name").collect()["name"].drop_nulls()}
                               if snap.manifest.get("industry") else set())
            else:
                industries |= {x.lower() for x in inst["industry"].drop_nulls()} if "industry" in inst.columns else set()
            boards |= {x.lower() for x in inst["board"].drop_nulls()} if "board" in inst.columns else set()
        from wb.market.industry import tushare_home
        for x in u.industries:
            if x.lower() in industries:
                continue
            home = tushare_home(ctx.store("CN"), ctx.latest("CN"), x) if "CN" in r.markets else None
            out.append(f"「{x}」是 Tushare 的分类，在申万里属于「{home}」（股票池按申万一级）" if home else f"数据里没有叫「{x}」的行业")
        out += [f"数据里没有名字含「{x}」的板块" for x in u.boards if not any(x.lower() in b for b in boards)]
        if u.index and (snap := ctx.latest(m := u.index[:2])) is not None:
            lists = index_membership(ctx.store(m).scan("index_members", snap).collect())
            first = lists.filter(pl.col("index_id") == u.index)["date"].min()
            if first is None:
                out.append(f"{m} 的数据档还没有 {u.index} 的成分表：股票池会是空的")
            elif first > snap.as_of - timedelta(days=365 * 2):  # the gallery's and calibration's default years
                out.append(f"{u.index} 的成分表从 {first} 起才有，之前的日子不在股票池里（样例图集和校准看的是近两年）")
        return out

    @app.post("/api/rules")
    def rule_save(text: str = Body(..., embed=True), create: bool = Body(False, embed=True)):
        """Save a version; `create`: a new rule, refused (409) when its id is taken."""
        try:
            rid, version, new = rules().save(text, create=create)
        except RuleExists as e:
            raise HTTPException(409, str(e)) from None
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        return {"id": rid, "version": version, "new": new}

    @app.put("/api/rules/{rule_id}/state")
    def rule_state(rule_id: str, body: dict = Body(...)):
        """Enable a rule for the daily scan (spec §7), or put it away; a setting left out stays as it is."""
        enabled, archived = body.get("enabled"), body.get("archived")
        if any(v is not None and not isinstance(v, bool) for v in (enabled, archived)):
            raise HTTPException(422, "enabled / archived 要写 true 或 false")
        try:
            rules().set_state(rule_id, enabled, archived)
        except RuleCannotRun as e:
            raise HTTPException(422, str(e)) from None
        except RuleError as e:
            raise HTTPException(404, str(e)) from None
        return {"ok": True}

    # ---- single-stock preview (spec §6.3) --------------------------------------------------
    def previewed(body: dict):
        rule, version = resolve(body)
        lid = body.get("listing_id") or ""
        market, _ = parse_id(lid)
        snap = ctx.latest(market)
        try:
            p, lock = previews.get((snap.id if snap else None, lid, rule_hash(rule)),
                                   lambda: (preview(ctx.paths(market), rule, market, lid), threading.Lock()))
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        return rule, version, p, lock

    @app.post("/api/preview")
    def preview_run(body: dict = Body(...)):
        basis = body.get("basis", "qfq")
        if basis not in BASES:  # before the preview is computed
            raise HTTPException(422, "bad basis")
        rule, version, p, _ = previewed(body)
        _, daily = ctx.daily(p.listing_id)
        return clean({
            "listing_id": p.listing_id, "snapshot_id": p.snapshot_id, "warnings": p.matcher.warnings,
            "rule": {"id": rule.id, "version": version, "name": rule.name, "mas": price_mas(rule),   # the close MAs it reads
                     "params": [{"id": x.id, "value": x.value, "range": x.range, "step": x.step, "unit": x.unit}
                                for x in rule.params if x.tune]},
            "signals": p.signals.to_dicts(),
            "structures": overlay(p.tables, daily, basis, p.traded) if daily.height else EMPTY,
        })

    @app.post("/api/preview/detail")
    def preview_detail(body: dict = Body(...)):
        _, _, p, lock = previewed(body)
        day = _day(body.get("day"))
        if day is None:
            raise HTTPException(422, "要给 day")
        with lock:
            d = p.matcher.detail(p.listing_id, day)
        return clean({"detail": d, "lines": render(d),
                      "bands": [{"stage": b["stage"], "label": d["names"]["stages"].get(b["stage"], b["stage"]),   # the chart tags it so
                                 "kind": b["kind"], "start": b["start"], "end": b["end"]} for b in d["bindings"]]})

    def sample_of(market: str, s: dict) -> list[str]:
        if not isinstance(s, dict):
            raise HTTPException(422, "sample 要写成 {n, seed} 或 {group_id}")
        if s.get("group_id") is not None:
            gid = _int(s["group_id"], "sample.group_id", 0, lo=0)
            return [i["listing_id"] for i in ctx.workspace().items(gid) if i["listing_id"].startswith(f"{market}:")]
        return sample(ctx.paths(market), market, _int(s.get("n"), "sample.n", 300), _int(s.get("seed"), "sample.seed", 0, lo=0))

    @app.get("/api/sample")
    def market_sample(market: str, n: int = Query(300, ge=1, le=2000), seed: int = Query(0, ge=0)):
        """The market's fixed random sample (stocks traded in the last year), e.g. to pick one to label."""
        if market not in MARKETS:
            raise HTTPException(422, "market 要写 CN 或 JP")
        return sample(ctx.paths(market), market, n, seed)

    @app.post("/api/rules/missed")
    def rule_missed(body: dict = Body(...)):
        """漏检排查 (spec §6.3): where the rule stopped on a listing that should have hit in [start, end]."""
        rule, _ = resolve(body)
        lid = body.get("listing_id") or ""
        market, _ = parse_id(lid)
        start, end = _day(body.get("start")), _day(body.get("end"))
        if start is None or end is None:
            raise HTTPException(422, "要给 start 和 end")
        if start > end:
            raise HTTPException(422, "start 要在 end 之前")
        try:
            out = missed(ctx.paths(market), rule, market, lid, start, end, sample_of(market, body.get("sample") or {}),
                         _int(body.get("years"), "years", 2))
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        return clean(out)

    @app.get("/api/templates")
    def template_list():
        """内置模板库 (spec §6.6): the cards, with each template's name, markets and doc."""
        return clean([{**c, "name": builtin_rules()[c["id"]][0].name, "markets": list(builtin_rules()[c["id"]][0].markets),
                       "doc": builtin_rules()[c["id"]][0].doc} for c in templates()])

    @app.post("/api/rules/calibrate")
    def rule_calibrate(body: dict = Body(...)):
        """网格校准 (spec §6.5): the tuned parameters' grid scored on your Y/N marks of this rule and, when a
        tuned parameter sets a box, on your box samples — the Pareto front, never applied by itself. A run in 验证's
        queue (1.33): what cannot run is said now; the run's id back, followed, paused, continued or cancelled under
        /api/validate like the others."""
        from wb.api.labels import calibration_inputs  # that module imports this one
        rule, version = resolve(body)
        s = body.get("sample") or {}
        settings = {"text": body.get("text") or None, "params": body.get("params") or {}, "years": _int(body.get("years"), "years", 2),
                    "sample": {"n": _int(s.get("n"), "sample.n", 300), "seed": _int(s.get("seed"), "sample.seed", 0, lo=0)}}
        try:
            _, markets, *_ = prepare(ctx.paths, rule, *calibration_inputs(ctx, rule.id))
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        end = max(ctx.latest(m).as_of for m in markets)   # the hits are counted on the last `years`
        start = end - timedelta(days=365 * settings["years"])
        return {"id": app.state.validations.start("calibrate", markets, start, end, end, rule=rule, version=version, settings=settings)}

    @app.post("/api/rules/compare")
    def rule_compare(body: dict = Body(...)):
        """版本对比 (spec §6.3): `b` against `a` ({rule_id, version} or {text}, each with params) on one sample."""
        market = body.get("market")
        if market not in MARKETS:
            raise HTTPException(422, "market 要写 CN 或 JP")
        a, _ = resolve(body.get("a") or {})
        b, _ = resolve(body.get("b") or {})
        key = ["listing_id", "signal_date"]
        # your Y / N verdicts on the rule (spec §6.5): which right hits the change loses, which wrong ones it adds
        marks = pl.DataFrame(HitLabels(ctx.paths(market)).all(b.id), schema={"listing_id": pl.Utf8, "signal_date": pl.Utf8,
                                                                            "verdict": pl.Utf8, "rule_version": pl.Int64,
                                                                            "rule_hash": pl.Utf8, "created_at": pl.Utf8})
        marks = marks.filter(pl.col("listing_id").str.starts_with(f"{market}:"))
        marks = marks.with_columns(pl.col("signal_date").str.to_date()).select(*key, "verdict")
        # the sample, and every listing you marked a hit on (marked on another sample, they still count)
        listings = sorted(set(sample_of(market, body.get("sample") or {})) | set(marks["listing_id"]))
        years = _int(body.get("years"), "years", 2)
        try:
            ga, gb = (gallery(ctx.paths(market), r, market, listings, years) for r in (a, b))
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        right, wrong = (marks.filter(pl.col("verdict") == v).select(key) for v in ("right", "wrong"))
        n = lambda g, m: g.join(m, on=key, how="semi").height  # noqa: E731
        return clean({"listings": len(listings), "added": gb.join(ga.select(key), on=key, how="anti").to_dicts(),
                      "lost": ga.join(gb.select(key), on=key, how="anti").to_dicts(),
                      "kept": ga.join(gb.select(key), on=key, how="semi").height,
                      "hits": [ga.height, gb.height], "right": [n(ga, right), n(gb, right)], "wrong": [n(ga, wrong), n(gb, wrong)],
                      "right_lost": ga.join(right, on=key, how="semi").join(gb.select(key), on=key, how="anti").to_dicts(),
                      "wrong_added": gb.join(wrong, on=key, how="semi").join(ga.select(key), on=key, how="anti").to_dicts()})

    # ---- market runs ---------------------------------------------------------------------
    @app.post("/api/runs")
    def run_start(body: dict = Body(...)):
        rule, version = resolve(body)
        market = body.get("market")
        if market not in MARKETS:
            raise HTTPException(422, "market 要写 CN 或 JP")
        if not RUN_LOCK.acquire(blocking=False):
            raise HTTPException(409, BUSY)
        try:
            run = run_rule(ctx.paths(market), rule, market, day=_day(body.get("day")), start=_day(body.get("start")),
                           end=_day(body.get("end")), version=version)
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        finally:
            RUN_LOCK.release()
        return {"id": run.id, "rule_id": rule.id, "version": version, "market": market, "snapshot_id": run.snapshot_id,
                "mode": run.mode, "day": run.day, "start": run.start, "end": run.end, "counts": run.counts,
                "warnings": run.warnings}

    @app.get("/api/runs")
    def run_list(rule_id: str | None = None, limit: int = Query(50, ge=1, le=500)):
        seen, out = set(), []
        for m in MARKETS:
            for r in runs(ctx.paths(m), rule_id, limit):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
        return sorted(out, key=lambda r: r["created_at"], reverse=True)[:limit]

    @app.get("/api/runs/{run_id}/rows")
    def run_rows_get(run_id: str, status: str | None = None, limit: int = Query(500, ge=1, le=10000), offset: int = 0):
        for m in MARKETS:
            try:
                rows = run_rows(ctx.paths(m), run_id)
            except (RuleError, FileNotFoundError):
                continue
            if status:
                rows = rows.filter(pl.col("status").is_in(status.split(",")))
            out = rows.slice(offset, limit).to_dicts()
            for r in out:
                if isinstance(r.get("detail"), str):
                    r["detail"] = json.loads(r["detail"])
            return clean(out)
        raise HTTPException(404, f"没有运行记录 {run_id}")

    # ---- the sample gallery (spec §6.3) -------------------------------------------------------
    @app.post("/api/gallery")
    def gallery_run(body: dict = Body(...)):
        rule, _ = resolve(body)
        market = body.get("market")
        if market not in MARKETS:
            raise HTTPException(422, "market 要写 CN 或 JP")
        listings = sample_of(market, body.get("sample") or {})
        years = _int(body.get("years"), "years", 2)
        try:
            hits = gallery(ctx.paths(market), rule, market, listings, years) if listings else pl.DataFrame()
        except RuleError as e:
            raise HTTPException(422, str(e)) from None
        return clean({"listings": len(listings), "hits": hits.to_dicts()})
