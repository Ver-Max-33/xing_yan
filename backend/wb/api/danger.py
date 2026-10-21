"""危险提醒 (design 2026-10-15 §3, §6): the warnings of the stocks a page lists, and one stock's history for the chart."""
from datetime import date

import polars as pl
from fastapi import Body, FastAPI

from wb.api.context import Context
from wb.danger import NOT_TESTED, active, history, listed
from wb.danger_words import label
from wb.listing import parse_id

JP_WHY = "日股没有危险提醒：检验只用了沪深股票"


def add_routes(app: FastAPI, ctx: Context) -> None:
    @app.post("/api/danger")
    def warnings(body: dict = Body(...)):
        market = str(body.get("market") or "CN")
        ids = sorted({str(x) for x in (body.get("ids") or [])})[:5000]
        if market != "CN":
            return {"covered": False, "why": JP_WHY, "day": None, "warnings": {}, "unknown": {}, "uncovered": {}, "unlocks_from": None,
                    "history_from": None}
        day = date.fromisoformat(body["day"]) if body.get("day") else None
        store = ctx.store(market)
        snap = store.latest()
        res = ctx.misc_cache.get(("danger", str(store.base), snap.id if snap else None, day, tuple(ids)),
                                 lambda: active(store, ids, day, snap))
        return {"covered": True, "why": None, **res}

    @app.get("/api/danger/events")
    def events_of(listing_id: str):
        market, _ = parse_id(listing_id)
        if market != "CN":
            return {"covered": False, "why": JP_WHY, "events": []}
        store = ctx.store(market)
        snap = store.latest()
        if snap is None:
            return {"covered": True, "why": None, "events": []}
        if listing_id not in set(listed(store, snap)["listing_id"].to_list()):
            return {"covered": False, "why": NOT_TESTED, "events": []}
        ev = history(store, snap).filter(pl.col("listing_id") == listing_id).sort("date")
        return {"covered": True, "why": None,
                "events": [{"date": r["date"].isoformat(), "kind": r["kind"], "label": label(r["kind"], r["boards"])} for r in ev.iter_rows(named=True)]}
