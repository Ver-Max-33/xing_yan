"""盯盘的线 (spec §7.2, design 2026-10-10 §2): the lines a price is watched against, in today's prices — the ones you
drew with 提醒 ticked (a horizontal line at its price, a trend line or ray carried along to today's bar, straight in
adjusted prices as the chart draws it by default), a 潜伏 box's break line, a watched stock's own box; none that today's
limits cannot reach."""

import polars as pl
import pytest

from tests.test_live_service import closes
from wb.data.store import Store
from wb.devdata import write
from wb.engine.l2.run import l2_bars
from wb.engine.l2.spec import compute
from wb.markets import limit_width, load_market
from wb.realtime.lines import drawn_lines, own_boxes, reachable
from wb.workspace.store import Workspace


def test_a_line_drawn_before_a_split_is_watched_in_todays_prices(paths):
    write(paths, 320)
    c = closes(paths, "JP:7203")                                                # Toyota splits 1:5 at bar 200
    before = c.row(100, named=True)
    ws = Workspace(paths)
    hid = ws.save_drawing("JP:7203", "horizontalStraightLine", [{"date": str(before["date"]), "price": before["raw_close"]}], alert=True)
    ws.save_drawing("JP:7203", "horizontalStraightLine", [{"date": str(before["date"]), "price": 1.0}])   # 提醒 not ticked
    lines = drawn_lines(paths, "JP", ["JP:7203"])
    assert [x["key"] for x in lines] == [hid]
    assert lines[0]["price"] == pytest.approx(before["close"] * c["raw_close"][-1] / c["close"][-1])   # a fifth of it


def test_a_trend_line_is_carried_to_todays_bar(paths):
    write(paths, 320)
    c = closes(paths, "JP:6758")
    a, b = str(c["date"][300]), str(c["date"][310])
    Workspace(paths).save_drawing("JP:6758", "segment", [{"date": a, "price": 100.0}, {"date": b, "price": 110.0}], alert=True)
    assert drawn_lines(paths, "JP", ["JP:6758"])[0]["price"] == pytest.approx(100.0 + (c.height - 300))   # today: bar c.height


def test_a_watched_stock_off_the_latent_lists_is_watched_against_its_own_box(paths):
    # design 2026-10-10 §2: the mid or long box it is in at its last close (established, neither ended nor superseded)
    # whose top is the nearest above that close — the top itself is watched; its break line (top × 1.015) is kept with
    # it — in adjusted prices, as the scan's. A top the close is already over is passed (600519 with 320 days: 0.06%)
    write(paths, 320)
    found = 0
    for market, ids in (("JP", ["JP:7203", "JP:6758", "JP:130A"]), ("CN", ["CN:600519", "CN:000001"])):
        got = own_boxes(paths, market, ids)
        store = Store(paths, market)
        bars = l2_bars(store, market, "test", ids, snap=store.latest())
        boxes = compute(bars, ["BOX"])["BOX"]["boxes"]
        for lid in ids:
            last = bars.filter(pl.col("listing_id") == lid).drop_nulls("close")["close"][-1]
            live = boxes.filter((pl.col("listing_id") == lid) & (pl.col("status") == "established") & pl.col("scale").is_in(["mid", "long"])
                                & pl.col("ended_at").is_null() & pl.col("superseded_at").is_null())
            above = sorted(t for t in live["top"] if t > last)
            assert (got[lid]["top"] if lid in got else None) == pytest.approx(above[0]) if above else lid not in got
            if above:
                assert got[lid]["line"] == pytest.approx(above[0] * 1.015)
            found += bool(above)
    assert found                                                                 # the demo market has some


def test_a_line_todays_limits_cannot_reach_is_not_watched():
    assert reachable("CN:600001", 11.0, 10.0) and not reachable("CN:600001", 11.02, 10.0)
    assert reachable("CN:300001", 11.9, 10.0)                                      # ChiNext: 20%
    assert reachable("CN:600001", 9.0, 10.0) and not reachable("CN:600001", 8.98, 10.0)
    w = limit_width(load_market("JP"), 2800.0)
    assert reachable("JP:7203", 2800.0 + w, 2800.0) and not reachable("JP:7203", 2800.0 + w + 1, 2800.0)


def test_a_ray_drawn_leftward_does_not_reach_today(paths):
    # a ray goes from its first point through its second: pointing into the past it never comes to today's bar
    write(paths, 320)
    c = closes(paths, "JP:6758")
    ws = Workspace(paths)
    ws.save_drawing("JP:6758", "rayLine", [{"date": str(c["date"][310]), "price": 110.0}, {"date": str(c["date"][300]), "price": 100.0}], alert=True)
    ws.save_drawing("JP:6758", "rayLine", [{"date": str(c["date"][300]), "price": 100.0}, {"date": str(c["date"][310]), "price": 110.0}], alert=True)
    assert [round(x["price"], 6) for x in drawn_lines(paths, "JP", ["JP:6758"])] == [100.0 + (c.height - 300)]
