"""The labelling tool's API (spec §5.10, §8.2 样例框). Prices travel in the chart's display basis and
are stored as exchange prices on their own days (a box's edges on its first bar), like drawings;
agreement converts them to the computation basis and runs the box primitive with the settings
being tuned."""
from bisect import bisect_right
from datetime import date

import polars as pl
from fastapi import Body, FastAPI, HTTPException, Query

from wb.api.context import Context
from wb.api.rules import clean
from wb.charts.series import BASES
from wb.charts.structures import display_per_raw
from wb.engine.l2.agreement import THRESHOLDS, agreement
from wb.engine.l2.run import compute_market, l2_bars
from wb.engine.l2.spec import check_settings
from wb.markets import MARKETS
from wb.workspace.labels import HitLabels, LabelError, Labels


def _scale(ctx: Context, listing_id: str, basis: str):
    """day -> display price ÷ exchange price."""
    if basis not in BASES:
        raise HTTPException(422, "bad basis")
    _, daily = ctx.daily(listing_id)
    if daily.is_empty():
        return lambda d: 1.0
    dates, s = display_per_raw(daily, basis)
    return lambda d: s[max(0, bisect_right(dates, d if isinstance(d, date) else date.fromisoformat(str(d))) - 1)]


def convert(ctx: Context, x: dict, basis: str, to_raw: bool) -> dict:
    at = _scale(ctx, x["listing_id"], basis)
    k = (lambda d: 1.0 / at(d)) if to_raw else at
    y = dict(x)
    for f in ("top", "bottom"):
        if y.get(f) is not None:
            y[f] = float(y[f]) * k(y["start"])
    y["touches"] = [{**t, "price": float(t["price"]) * k(t["date"])} for t in y.get("touches") or []]
    y["exclusions"] = [{**e, "price": float(e["price"]) * k(e["date"])} for e in y.get("exclusions") or []]
    return y


def computation_basis(ctx: Context, labels: list[dict]) -> list[dict]:
    """Labels as agreement() and calibration take them: prices in the computation basis (后复权), dates as dates."""
    return [{**convert(ctx, x, "hfq", to_raw=False), **{k: date.fromisoformat(x[k]) for k in ("cutoff", "start", "end")}}
            for x in labels]


def calibration_inputs(ctx: Context, rule_id: str) -> tuple[list[dict], list[dict]]:
    """What a calibration of a rule runs on: your Y / N on its hits, and your box samples in the computation basis."""
    shared = ctx.paths(MARKETS[0])  # shared.sqlite is common to every profile
    marks = [{**m, "signal_date": date.fromisoformat(m["signal_date"])} for m in HitLabels(shared).all(rule_id)]
    return marks, computation_basis(ctx, Labels(shared).all())


def add_routes(app: FastAPI, ctx: Context) -> None:
    def store() -> Labels:
        return Labels(ctx.paths(MARKETS[0]))  # shared.sqlite is common to every profile

    @app.get("/api/labels")
    def label_list(listing_id: str | None = None, basis: str = Query("qfq")):
        return [convert(ctx, x, basis, to_raw=False) for x in store().all(listing_id)]

    def to_raw(body: dict) -> dict:
        """The body's prices in exchange prices; a malformed body is a 422 with the reason."""
        try:
            return convert(ctx, body, body.get("basis", "qfq"), to_raw=True)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise HTTPException(422, f"标注写得不完整或格式不对：{e}") from None

    @app.post("/api/labels")
    def label_add(body: dict = Body(...)):
        try:
            return {"id": store().add(to_raw(body))}
        except LabelError as e:
            raise HTTPException(422, str(e)) from None

    @app.put("/api/labels/{label_id}")
    def label_update(label_id: str, body: dict = Body(...)):
        s = store()
        basis = body.get("basis", "qfq")
        try:
            old = s.get(label_id)
        except LabelError as e:
            raise HTTPException(404, str(e)) from None
        shown = {**convert(ctx, old, basis, to_raw=False), **body}
        try:
            s.update(label_id, to_raw(shown))
        except LabelError as e:
            raise HTTPException(422, str(e)) from None
        return {"id": label_id}

    @app.put("/api/labels/{label_id}/verdict")
    def label_verdict(label_id: str, verdict: str | None = Body(..., embed=True)):
        try:
            store().get(label_id)
        except LabelError as e:
            raise HTTPException(404, str(e)) from None
        try:
            store().set_verdict(label_id, verdict)
        except LabelError as e:
            raise HTTPException(422, str(e)) from None
        return {"id": label_id, "verdict": verdict}

    @app.delete("/api/labels/{label_id}")
    def label_delete(label_id: str):
        store().delete(label_id)
        return {"ok": True}

    @app.get("/api/hit_labels")
    def hit_label_list(rule_id: str):
        return HitLabels(ctx.paths(MARKETS[0])).all(rule_id)

    @app.put("/api/hit_labels")
    def hit_label_set(body: dict = Body(...)):
        """Y / N in the gallery: {rule_id, listing_id, signal_date, verdict: right | wrong | null}."""
        try:
            HitLabels(ctx.paths(MARKETS[0])).set(body["rule_id"], body["listing_id"], body["signal_date"], body.get("verdict"),
                                                 body.get("version"), body.get("hash"))
        except (LabelError, KeyError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        return {"ok": True}

    @app.post("/api/labels/agreement")
    def label_agreement(body: dict = Body(...)):
        """§5.10 agreement of the algorithm with your labels — in one market, or (none named) over all of them,
        as the P2 acceptance line counts the whole label set — under box settings `settings`."""
        market = body.get("market")
        if market is not None and market not in MARKETS:
            raise HTTPException(422, "market 要写 CN 或 JP（不写就是全部）")
        settings = body.get("settings") or {}
        given = body.get("thresholds") or {}
        if not isinstance(settings, dict) or not isinstance(given, dict):
            raise HTTPException(422, "settings 和 thresholds 要写成 {名字: 值}")
        bad = [k for k, v in given.items() if k not in THRESHOLDS or isinstance(v, bool) or not isinstance(v, (int, float))]
        if bad:
            raise HTTPException(422, f"thresholds 里 {'、'.join(map(str, bad))} 不对：可选 {'、'.join(THRESHOLDS)}，值要写数字")
        th = {**THRESHOLDS, **given}
        try:
            check_settings("BOX", settings)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        hfq, boxes, bars = [], [_empty_boxes()], [_empty_bars()]
        for m in [market] if market else MARKETS:
            mine = [x for x in store().all() if x["listing_id"].startswith(f"{m}:")]
            listings = sorted({x["listing_id"] for x in mine})
            st = ctx.store(m)
            snap = st.latest()
            if snap is None or not listings:  # no labels or no data in this market: nothing to compare
                continue
            b = l2_bars(st, m, ctx.paths(m).profile, listings, snap=snap)
            names = ["BOX"] + (["SLANT_BOX"] if any(x["category"] == "slant" for x in mine) else [])
            tables = compute_market(b, names, {"BOX": settings} if settings else None)
            boxes.append(tables["BOX"]["boxes"].select(BOX_COLS))
            if "SLANT_BOX" in tables:
                boxes.append(tables["SLANT_BOX"]["slant"].select(BOX_COLS))
            bars.append(b.select("listing_id", "date", "close"))
            # labels into the computation basis (backward-adjusted) and real dates
            hfq += computation_basis(ctx, mine)
        return clean(agreement(hfq, pl.concat(boxes, how="vertical_relaxed"), pl.concat(bars, how="vertical_relaxed"), th))


BOX_COLS = ["box_id", "listing_id", "scale", "status", "start", "known_at", "end", "ended_at", "top", "bottom"]


def _empty_boxes():
    return pl.DataFrame(schema={"box_id": pl.Utf8, "listing_id": pl.Utf8, "scale": pl.Utf8, "status": pl.Utf8, "start": pl.Date,
                                "known_at": pl.Date, "end": pl.Date, "ended_at": pl.Date, "top": pl.Float64, "bottom": pl.Float64})


def _empty_bars():
    return pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date, "close": pl.Float64})
