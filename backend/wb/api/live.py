"""盘中 (spec §8.5): each market's rows and session now, a stock's minutes of the day, and the live feed (SSE)."""
import asyncio
import json
from datetime import date, datetime

import polars as pl
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from wb.api.context import Context
from wb.jobs.live import Live, _read_one
from wb.listing import parse_id
from wb.markets import MARKETS, load_market
from wb.realtime.alerts import Alerts
from wb.realtime.micro import DAYS as MICRO_DAYS, PERIODS, micro_boxes
from wb.realtime.providers.yfinance_rt import MINUTES
from wb.realtime.session import minute_index, minute_span
from wb.realtime.stream import Broadcaster
from wb.realtime.today import turnover
from wb.validate.jobs import _json

class Mute(BaseModel):
    listing_id: str


KEEPALIVE = 15  # seconds between comment lines on a quiet stream (proxies and the page keep it open)


def add_routes(app: FastAPI, ctx: Context, live: Live, feed: Broadcaster) -> None:
    @app.get("/api/live")
    def live_now():
        return {"markets": [_json(live.state(m)) for m in MARKETS]}

    @app.get("/api/live/{market}/latent")
    def latent(market: str):
        """The 潜伏 stocks you do not watch, by the whole-market quote: seen, never judged (盯盘 follows yours only, 10-12)."""
        if market not in MARKETS:
            raise HTTPException(404, market)
        return _json(live.latent(market))

    @app.get("/api/live/{listing_id}/minutes")
    def minutes(listing_id: str, day: str | None = None, days: int = 1):
        """The stock's minutes of the last `days` kept days to `day` (default: the latest day kept), side by side:
        each minute with its own day's average price so far, its place on the clock (`i` = the day's
        position `d` × `total` + its minute in the day, lunch left out; `sessions` the hours), and the close before the
        last day."""
        market, _ = parse_id(listing_id)
        live.view(listing_id)   # its 分时 on the K-line page: polled first while you look (design 2026-10-12 §9)
        try:
            asked = date.fromisoformat(day) if day else None
        except ValueError:
            raise HTTPException(422, "日期要写成 YYYY-MM-DD") from None
        if not 1 <= days <= 20:
            raise HTTPException(422, "多日分时最多 20 天")
        sessions = load_market(market).sessions
        total = sum(minute_span(a, b) for a, b in sessions)
        folder = ctx.paths(market).market_dir(market) / "intraday"
        files = sorted(folder.glob("date=*.parquet")) if folder.exists() else []
        if asked is not None and not (folder / f"date={asked}.parquet").exists():
            chosen = []
        else:
            chosen = [f for f in files if asked is None or f.stem.removeprefix("date=") <= asked.isoformat()][-days:]
        parts = []
        for k, path in enumerate(chosen):
            rows = _read_one(path, listing_id).sort("at")
            if rows.height:
                parts.append(rows.with_columns(
                    pl.when(pl.col("volume").cum_sum() > 0)   # before any volume the average is the price
                    .then(turnover(rows).cum_sum() / pl.col("volume").cum_sum())
                    .otherwise(pl.col("close")).alias("vwap"),
                    (minute_index(sessions, pl.col("at")) + k * total).alias("i"), pl.lit(k).alias("d")))
        on = date.fromisoformat(chosen[-1].stem.removeprefix("date=")) if chosen else asked
        prev = None
        if on is not None and ctx.latest(market) is not None:
            closes = (ctx.store(market).scan("bars_1d").filter((pl.col("listing_id") == listing_id) & (pl.col("date") < on))
                      .select("date", "close").sort("date").collect())
            prev = closes["close"][-1] if closes.height else None
        rows = pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()
        return _json({"listing_id": listing_id, "day": on, "prev": prev,   # the box top: in the stock's row, on the page
                      "days": [f.stem.removeprefix("date=") for f in chosen] or ([asked.isoformat()] if asked else []),
                      "total": total, "sessions": [[a.strftime("%H:%M"), b.strftime("%H:%M")] for a, b in sessions],
                      "minutes": rows.drop("listing_id").to_dicts() if rows.height else []})

    @app.get("/api/live/alerts")
    def live_alerts():
        """Each market's alerts of its day (spec §8.5 预警中心) and the stocks muted for it (今天不再提醒)."""
        now, alerts, muted = datetime.now().astimezone(), [], []
        for m in MARKETS:
            day, kept = live.session(m, now).day, Alerts(ctx.paths(m))
            alerts += kept.day(day, m)
            muted += sorted(x for x in kept.muted(day) if x.startswith(f"{m}:"))
        return _json({"alerts": sorted(alerts, key=lambda a: a["at"]), "muted": muted})

    @app.post("/api/live/mute")
    def mute(body: Mute):
        """No alert on this stock for the rest of its market's day."""
        market, _ = parse_id(body.listing_id)
        Alerts(ctx.paths(market)).mute(live.session(market, datetime.now().astimezone()).day, body.listing_id)
        return {"muted": True}

    @app.get("/api/live/tachibana")
    def tachibana_status():
        """The prod JP source's session: configured, which address (demo or production), logged in today."""
        from wb.realtime.providers import tachibana
        return tachibana.status()

    @app.post("/api/live/tachibana/login")
    def tachibana_login():
        """Today's session, when you ask (during the transition: right after the phone authentication)."""
        from wb.realtime.providers import tachibana
        try:
            tachibana.Tachibana("JP").login()
        except (tachibana.TachibanaError, OSError, ValueError) as e:   # not configured, no key file, a refusal, cut off
            raise HTTPException(422, str(e)) from None
        return tachibana.status()

    @app.get("/api/live/{listing_id}/micro")
    def micro(listing_id: str, period: int = 1):
        """分时小箱 (spec §8.5): the stock's kept minutes as `period`-minute bars, the BOX boxes in them and their events,
        each break with its volume against the bars near that edge; the bar still forming marked; the waves with their
        volume against the wave before, and each bigger box's last small box as its 金品."""
        if period not in PERIODS:
            raise HTTPException(422, "分时小箱的周期只有 1、5、15 分钟")
        market, _ = parse_id(listing_id)
        now = datetime.now().astimezone()
        # the minute K-line's days (its 20, today's as polled), so both pages find the same boxes (review 10-13: every
        # day kept was read here, the first ones' boxes differed)
        minutes = live.minutes_of(listing_id, MICRO_DAYS, now)
        s = live.session(market, now)   # a bar still forming: the last one, on the day that trades
        out = micro_boxes(minutes, load_market(market).sessions, period,
                          trading=s.state == "trading" and not minutes.is_empty() and minutes["at"].max().date() == s.day,
                          market=market)
        keep = ("box_id", "scale", "status", "start", "known_at", "end", "ended_at", "end_reason", "top", "bottom", "shape")
        # each box as it stands now: its versions before (an edge redrawn as the box grew) are not drawn, nor their events
        now = out["boxes"].filter(pl.col("superseded_at").is_null()) if out["boxes"].height else out["boxes"]
        boxes = now.select([c for c in keep if c in now.columns]).to_dicts() if now.height else []
        ids = {b["box_id"] for b in boxes}
        events = out["events"].filter(pl.col("box_id").is_in(list(ids))).to_dicts() if out["events"].height else []
        return _json({"listing_id": listing_id, "period": period, "bars": out["bars"].to_dicts(), "boxes": boxes,
                      "events": events, "waves": out["waves"].to_dicts(),
                      "gold": out["gold"].to_dicts() if out["gold"].height else []})

    @app.get("/api/live/stream")
    async def stream(request: Request):
        return StreamingResponse(sse(live, feed, request.is_disconnected), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})


async def sse(live: Live, feed: Broadcaster, gone, keepalive: float = KEEPALIVE):
    """The feed of one page: the state now (`hello`), then every event published, a comment line while quiet;
    ends when `gone()` says the page left."""
    q = feed.subscribe()
    try:
        now = await asyncio.to_thread(lambda: [_json(live.state(m)) for m in MARKETS])
        yield f"event: hello\ndata: {json.dumps({'markets': now}, ensure_ascii=False, default=str)}\n\n"
        while not await gone():
            try:
                event = await asyncio.wait_for(q.get(), keepalive)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield f"event: {event['type']}\ndata: {json.dumps(_json(event), ensure_ascii=False, default=str)}\n\n"
    finally:
        feed.unsubscribe(q)
