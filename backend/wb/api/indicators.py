"""指标库 (design 2026-10-10 §4): the catalog, an indicator's text, the check (what is wrong line by line, what it
draws, and its last values on a stock), saving, putting away, and an indicator over a chart's own bars."""
from fastapi import Body, FastAPI, HTTPException, Query

from wb.api.context import Context
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.script import compile_script
from wb.indicators import GROUPS, IndicatorError, IndicatorExists, Indicators, dump, load_indicator
from wb.indicators.compute import compute
from wb.listing import parse_id

SAMPLE = 5   # bars of the stock a check shows


def add_routes(app: FastAPI, ctx: Context) -> None:
    def store() -> Indicators:
        return Indicators(ctx.paths("CN"))

    def guard(fn):
        try:
            return fn()
        except IndicatorExists as e:
            raise HTTPException(409, str(e)) from None
        except IndicatorError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/indicators")
    def catalog():
        return {"groups": GROUPS, "indicators": store().catalog()}

    @app.get("/api/indicators/{iid}")
    def one(iid: str):
        ind, text, version = guard(lambda: store().get(iid))
        return {"id": iid, "version": version, "text": text, "indicator": ind.model_dump(mode="json"),
                "builtin": iid in {r["id"] for r in store().catalog() if r["builtin"]}}

    @app.post("/api/indicators/check")
    def check(body: dict = Body(...)):
        """The text as the editor has it: YAML problems, or the formula's (line by line, and what this system has no
        data for, with the script as it could be drawn without them), what it draws and where, and — given a stock —
        its last few bars' values on that stock's daily chart."""
        try:
            ind = load_indicator(str(body.get("text", "")), check=False)
        except IndicatorError as e:
            return {"ok": False, "errors": [{"line": 0, "message": str(e)}], "unsupported": [], "reduced": None, "items": [],
                    "indicator": None, "sample": None}
        market = next((m for m in ind.markets if m == body.get("market")), ind.markets[0])
        c = compile_script(ind.formula, market, ind.param_values())
        out = {"ok": c.program is not None, "errors": c.errors, "indicator": ind.model_dump(mode="json"),
               "unsupported": [{"why": w, "names": n} for w, n in c.unsupported.items()], "reduced": c.reduced,
               "items": [{"kind": it.kind, "name": it.name, "color": it.color, "hidden": it.hidden, "line": it.line} for it in c.items],
               "pane": ind.pane if ind.pane != "auto" else ("main" if c.price_only else "sub"), "sample": None}
        others = [m for m in ind.markets if m != market]
        for m in others:   # a field only one market has: said for the other
            o = compile_script(ind.formula, m, ind.param_values())
            out["errors"] += [{**e, "message": f"{'日股' if m == 'JP' else 'A 股'}：{e['message']}"} for e in o.errors
                              if e not in c.errors]
        lid = body.get("listing_id")
        if out["ok"] and not out["errors"] and lid:
            res = series_of(ind, lid, "day", "qfq", False)
            if "error" in res:
                out["sample"] = {"listing_id": lid, "error": res["error"]}
            else:
                cols = [(it["name"], it["cols"]["value"]) for it in res["items"] if "value" in it["cols"]]
                rows = [{"t": t, **{name: res["values"][col][i] for name, col in cols}}
                        for i, t in list(enumerate(res["t"]))[-SAMPLE:]]
                out["sample"] = {"listing_id": lid, "rows": rows, "zones": len(res["zones"]), "levels": len(res["levels"])}
        return out

    @app.post("/api/indicators")
    def save(body: dict = Body(...)):
        iid, version, created = guard(lambda: store().save(str(body.get("text", "")), create=bool(body.get("create"))))
        return {"id": iid, "version": version, "created": created}

    @app.put("/api/indicators/{iid}/state")
    def state(iid: str, body: dict = Body(...)):
        guard(lambda: store().set_state(iid, bool(body.get("archived"))))
        return {"ok": True}

    @app.get("/api/indicators/{iid}/export")
    def export(iid: str):
        ind, _, _ = guard(lambda: store().get(iid))
        return {"text": dump(ind)}

    def series_of(ind, listing_id: str, period: str, basis: str, live: bool) -> dict:
        market, _ = parse_id(listing_id)
        if market not in ind.markets:
            return {"error": f"这个指标不适用于{'日股' if market == 'JP' else 'A 股'}"}
        snap, series, temp = app.state.chart_series(listing_id, period, basis, live)
        if series is None:
            return {"error": "这只股票还没有 K 线数据"}
        st = ctx.store(market)
        have = ctx.misc_cache.get(("avail", st.base, snap.id), lambda: available_columns(st, snap))
        paths = ctx.paths(market)

        def frame(columns: set[str]):
            return build_frame(st, market, paths.profile, columns, [listing_id], snap=snap)
        key = ("indicator", snap.id, listing_id, period, basis, tuple(temp.values()) if temp else None, ind.id, ind.formula,
               tuple(ind.param_values().items()), ind.pane, tuple(ind.levels))
        return ctx.misc_cache.get(key, lambda: compute(ind, series, listing_id, market, frame, have, ctx.calendar_open(market)))

    @app.get("/api/indicator-series")
    def indicator_series(listing_id: str, id: str, period: str = Query("day"), basis: str = Query("qfq"),
                         live: bool = False, version: int | None = None):
        """The indicator over a chart's own bars (its basis, period and the bar forming): each output by bar
        (`t`: the bars' times, as /api/bars gives them), the zones and levels, where it is drawn."""
        ind, _, v = guard(lambda: store().get(id, version))
        res = series_of(ind, listing_id, period, basis, live)
        return {"id": id, "version": v, "name": ind.name, **res}
