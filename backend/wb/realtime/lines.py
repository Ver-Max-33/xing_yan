"""盯盘的线 (spec §7.2, design 2026-10-10 §2): what a price is watched against — the lines you drew with 提醒 ticked (a
horizontal line at its price; a trend line or ray carried along to today's bar, straight in adjusted prices as the chart
draws it by default), a 潜伏 box's top (from the scan's break line), a watched stock's own box's top (the mid or long box
it is in): the top itself, where the alert is set, not the break line over it (2026-10-10, ledger); a
line today's limits cannot reach is not watched today."""
import logging
from bisect import bisect_right
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import build_frame
from wb.engine.l2.run import l2_bars
from wb.engine.l2.spec import compute
from wb.listing import parse_id
from wb.markets import cn_limit_pct, limit_width, load_market
from wb.workspace.store import Workspace

log = logging.getLogger("wb.live")
TOL = 0.015   # a box's break line, its top × 1.015: where the scan's rules call it a breakout (the box templates' tol)
SCALES = {"mid": "中箱", "long": "大箱"}
LINE_KINDS = ("horizontalStraightLine", "segment", "rayLine")   # 水平线, 趋势线, 射线 (the chart's overlay names)


def local(stamp: str, tz: ZoneInfo) -> datetime:
    """A time this app stored (this computer's local time) as the exchange's clock reads it."""
    return datetime.fromisoformat(stamp).astimezone(tz).replace(tzinfo=None)


def drawn_lines(paths: Paths, market: str, listing_ids: list[str], day: date | None = None) -> list[dict]:
    """key (the drawing's id), listing_id, kind, price today, text, since (when it was drawn, exchange time, if that was
    on `day`): your lines with 提醒 ticked. A ray drawn leftward never comes to today: not watched."""
    ws = Workspace(paths)
    drawn = [(lid, d) for lid in listing_ids for d in ws.drawings(lid) if d["alert"] and d["kind"] in LINE_KINDS and d["points"]]
    store = Store(paths, market)
    if not drawn or store.latest() is None:
        return []
    bars = (build_frame(store, market, paths.profile, {"close", "raw_close"}, sorted({lid for lid, _ in drawn}), snap=store.latest())
            .drop_nulls(["close", "raw_close"]).sort("listing_id", "date"))
    tz = ZoneInfo(load_market(market).timezone)
    out = []
    for lid, d in drawn:
        b = bars.filter(pl.col("listing_id") == lid)
        if b.is_empty():
            continue
        days, factor = b["date"].to_list(), (b["close"] / b["raw_close"]).to_list()
        try:
            pts = []
            for p in d["points"][:2]:   # a point's bar: the last one on or before its date (the first for an earlier date)
                i = max(bisect_right(days, date.fromisoformat(p["date"])) - 1, 0)
                pts.append((i, p["price"] * factor[i]))
            drawn_at = local(d["created_at"], tz)
        except (ValueError, TypeError, KeyError):   # a drawing that cannot be read is not watched; the others are
            log.warning("drawing %s of %s cannot be read as a price line", d.get("id"), lid)
            continue
        (i1, h1), (i2, h2) = pts[0], pts[-1]
        if d["kind"] == "rayLine" and i2 < i1:
            continue
        h = h1 if d["kind"] == "horizontalStraightLine" or i1 == i2 else h1 + (h2 - h1) * (len(days) - i1) / (i2 - i1)
        out.append({"key": d["id"], "listing_id": lid, "kind": d["kind"], "price": h / factor[-1], "text": d["styles"].get("text"),
                    "since": drawn_at if day is not None and drawn_at.date() == day else None})
    return out


def own_boxes(paths: Paths, market: str, ids: list[str]) -> dict[str, dict]:
    """Each stock's mid or long box it is in at its last close (established, neither ended nor superseded) whose top is
    the nearest above that close: top and line (its break line, top × 1.015; adjusted prices, as a scan's), scale (中箱 /
    大箱), start, length (bars) and width — the structures as the chart has them. A top the close is already over (inside
    the band, short of the break line) is passed: the box over it, if any, is the one."""
    store = Store(paths, market)
    snap = store.latest()
    if snap is None or not ids:
        return {}
    bars = l2_bars(store, market, paths.profile, ids, snap=snap)
    if bars.is_empty():
        return {}
    last = bars.drop_nulls("close").sort("date").group_by("listing_id").agg(pl.col("close").last().alias("last"))
    boxes = compute(bars, ["BOX"])["BOX"]["boxes"]
    live = (boxes.filter((pl.col("status") == "established") & pl.col("scale").is_in(list(SCALES))
                         & pl.col("ended_at").is_null() & pl.col("superseded_at").is_null())
            .join(last, on="listing_id").filter(pl.col("top") > pl.col("last")).sort("top").unique("listing_id", keep="first"))
    return {r["listing_id"]: {"top": r["top"], "line": r["top"] * (1 + TOL), "scale": SCALES[r["scale"]], "start": r["start"],
                              "length": r["length"], "width": r["width"]} for r in live.iter_rows(named=True)}


def day_limit(listing_id: str, prev: float) -> float:
    """How far today's limits let the price go either way from the last close `prev` (a limit price is rounded to the
    tick; an ST stock's 5% is not in the code: its board's)."""
    market, code = parse_id(listing_id)
    return limit_width(load_market(market), prev) if market == "JP" else prev * cn_limit_pct(code) + 0.005


def reachable(listing_id: str, price: float, prev: float) -> bool:
    """A price today's limits let the stock reach from its last close `prev`."""
    return abs(price - prev) <= day_limit(listing_id, prev) + 1e-9
