"""观察条目 (spec §11, §2.1): a stock you watch, from where it came and at what close; its status (待研究、观察中、暂不考虑、
已关闭); why, what speaks for and against it and your notes, every version kept; fail lines and review dates checked
after each close, each fired once with a notice; how it has done since, against the market's benchmark — also once
set aside, so the record is not only the successes; and a record of what happened."""
import shutil
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from wb.data.store import Store
from wb.devdata import write
from wb.workspace.watch import Watch

NOW = datetime(2026, 10, 1, 17, 0)
LID = "CN:600519"


def close_on(paths, day=None) -> tuple[date, float]:
    bars = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == LID)
    if day is not None:
        bars = bars.filter(pl.col("date") <= day)
    row = bars.sort("date").row(-1, named=True)
    return row["date"], row["close"]


@pytest.fixture
def watch(paths):
    write(paths, 320)
    return Watch(lambda m: paths)


def test_an_entry_starts_from_where_it_came_at_that_close_and_keeps_a_record(paths, watch):
    e = watch.add(LID, source="规则「站上 20 日线」v1 2026-09-30 的信号", reason="箱体很干净", now=NOW)
    day, close = close_on(paths)
    assert (e["status"], e["added_day"], e["added_price"], e["reason"]) == ("watching", str(day), close, "箱体很干净")
    assert watch.add(LID, now=NOW)["added_day"] == str(day) and len(watch.entries()) == 1      # once per stock
    assert [r["text"] for r in watch.record(LID)] == ["加入观察：规则「站上 20 日线」v1 2026-09-30 的信号"]


def test_notes_keep_every_version_and_a_status_change_is_in_the_record(watch):
    watch.add(LID, now=NOW)
    watch.set_text(LID, "note", "第一版", now=NOW)
    watch.set_text(LID, "note", "第二版", now=NOW)
    assert watch.entry(LID)["note"] == "第二版" and [v["text"] for v in watch.versions(LID, "note")] == ["第二版", "第一版"]
    watch.set_status(LID, "passed", now=NOW)
    assert watch.entry(LID)["status"] == "passed" and watch.record(LID)[0]["text"] == "改为 暂不考虑"
    assert [e["listing_id"] for e in watch.entries("passed")] == [LID] and watch.entries("watching") == []
    with pytest.raises(ValueError, match="状态"):
        watch.set_status(LID, "sold", now=NOW)                                                 # no trading words


def test_a_stock_you_hold_is_marked_by_hand_and_its_fail_lines_are_checked(paths, watch):
    # 持有中 (design 2026-10-10 §7): a mark, no shares and no broker; the sell side's alerts are for these stocks
    watch.add(LID, now=NOW)
    watch.set_status(LID, "holding", now=NOW)
    assert watch.entry(LID)["status_name"] == "持有中" and watch.record(LID)[0]["text"] == "改为 持有中"
    _, close = close_on(paths)
    watch.add_condition(LID, "fail", text="收盘跌破假底", op="<", price=close + 1, now=NOW)
    assert [f["text"] for f in watch.check("CN", now=NOW)] == ["收盘跌破假底"]


def test_a_fail_line_fires_once_after_the_close_that_crosses_it(paths, watch):
    watch.add(LID, now=NOW)
    day, close = close_on(paths)
    watch.add_condition(LID, "fail", text="收盘跌回箱顶", op="<", price=close + 1, now=NOW)
    watch.add_condition(LID, "fail", text="收盘跌破 1 元", op="<", price=1.0, now=NOW)        # not crossed
    fired = watch.check("CN", now=NOW)
    assert [(f["listing_id"], f["text"], f["day"]) for f in fired] == [(LID, "收盘跌回箱顶", str(day))]
    assert watch.check("CN", now=NOW) == []                                                       # once
    assert watch.record(LID)[0]["text"].startswith("触发失效条件：收盘跌回箱顶（收")
    cond = {c["text"]: c for c in watch.entry(LID)["conditions"]}
    assert cond["收盘跌回箱顶"]["fired_day"] == str(day) and cond["收盘跌破 1 元"]["distance"] < 0


def test_a_review_date_comes_due_once(paths, watch):
    watch.add(LID, now=NOW)
    day, _ = close_on(paths)
    watch.add_condition(LID, "review", text="决算发表后复核", due=str(day), now=NOW)
    assert [f["text"] for f in watch.check("CN", now=NOW)] == ["决算发表后复核"] and watch.check("CN", now=NOW) == []


def test_how_it_has_done_since_against_the_benchmark_also_when_set_aside(paths, watch):
    first, _ = close_on(paths, date(2026, 8, 3))
    watch.add(LID, now=NOW, day=first)
    watch.set_status(LID, "passed", now=NOW)
    perf = watch.entry(LID)["performance"]
    bars = Store(paths, "CN").read("bars_1d").filter(pl.col("listing_id") == LID).sort("date")
    hfq = dict(zip(bars["date"], bars["close"] * bars["adj_factor"].fill_null(1.0), strict=True))
    idx = Store(paths, "CN").read("index_bars_1d").filter(pl.col("index_id") == "CN:IDX:000300")
    ic = dict(zip(idx["date"], idx["close"], strict=True))
    last = max(hfq)
    assert perf["benchmark"] == "CN:IDX:000300" and perf["since"] == pytest.approx(hfq[last] / hfq[first] - 1)
    assert perf["excess"] == pytest.approx(perf["since"] - (ic[last] / ic[first] - 1))


def test_a_condition_only_you_can_check_is_kept_and_never_fires(watch):
    watch.add(LID, now=NOW)
    watch.add_condition(LID, "manual", text="停产延长公告", now=NOW)                             # no feed for it
    assert watch.check("CN", now=NOW) == []
    c = watch.entry(LID)["conditions"][0]
    assert (c["kind"], c["text"], c.get("distance"), c["fired_day"]) == ("manual", "停产延长公告", None, None)
    assert watch.record(LID)[0]["text"] == "加要自己对照的条件：停产延长公告"


def test_stocks_already_in_groups_become_watch_entries(tmp_path, monkeypatch):
    from wb import db
    mig = tmp_path / "m" / "shared"
    mig.mkdir(parents=True)
    for f in sorted((db.MIGRATIONS / "shared").glob("*.sql")):
        if f.name < "010":
            shutil.copy(f, mig)
    monkeypatch.setattr(db, "MIGRATIONS", mig.parent)
    conn = db.connect(tmp_path / "shared.sqlite", "shared")
    conn.execute("INSERT INTO watch_item VALUES (1, 'CN:600519', '', '2026-09-30T10:00:00')")
    conn.execute("INSERT INTO watch_item VALUES (2, 'CN:600519', '', '2026-09-29T21:00:00')")
    shutil.copy(next((Path(db.__file__).with_name("migrations") / "shared").glob("010_*.sql")), mig)
    db._migrate(conn, "shared")
    rows = [dict(r) for r in conn.execute("SELECT listing_id, status, source, added_day FROM watch_entry")]
    assert rows == [{"listing_id": LID, "status": "watching", "source": "自选分组", "added_day": "2026-09-29"}]
    assert [r[0] for r in conn.execute("SELECT text FROM watch_record")] == ["加入观察：自选分组"]


def test_how_it_has_done_carries_a_missing_factor_forward_as_the_chart_does(paths, watch):
    # Tushare's factor call is optional: a day without it is not a 1:1 price (the rest of the history has 1.3)
    st = Store(paths, "CN")
    bars = st.read("bars_1d")
    last = bars["date"].max()
    st.publish({"bars_1d": bars.filter((pl.col("listing_id") == LID) & (pl.col("date") == last))
                .with_columns(pl.lit(None, pl.Float64).alias("adj_factor"))}, as_of=last, note="factor missing")
    first, _ = close_on(paths, date(2026, 8, 3))
    watch.add(LID, now=NOW, day=first)
    own = st.read("bars_1d").filter(pl.col("listing_id") == LID).sort("date")
    a, b = own.filter(pl.col("date") == first).row(0, named=True), own.row(-1, named=True)
    factor = own.filter(pl.col("adj_factor").is_not_null())["adj_factor"][-1]
    assert watch.entry(LID)["performance"]["since"] == pytest.approx(b["close"] * factor / (a["close"] * a["adj_factor"]) - 1)


def test_a_review_day_comes_due_for_a_stock_that_does_not_trade(paths, watch):
    watch.add("CN:688999", now=NOW)                                                   # no bars at all
    watch.add_condition("CN:688999", "review", text="复核", due="2026-10-01", now=NOW)
    fired = watch.check("CN", now=datetime(2026, 10, 2, 17, 0))
    assert [(f["text"], f["day"], f["close"]) for f in fired] == [("复核", "2026-10-02", None)]


def test_a_condition_with_a_bad_date_or_price_is_refused_and_nothing_is_kept(watch):
    watch.add(LID, now=NOW)
    for kw in ({"kind": "review", "due": "11/05"}, {"kind": "fail", "op": "<", "price": "abc"}):
        with pytest.raises(ValueError):
            watch.add_condition(LID, text="x", now=NOW, **kw)
    assert watch.entry(LID)["conditions"] == [] and len(watch.entries()) == 1


def test_a_fail_line_keeps_to_the_prices_of_the_day_it_was_set_across_a_split(paths, watch):
    # a 2:1 split halves the exchange's close: a line under the old close must not fire, its distance stays the same
    st = Store(paths, "CN")
    last = st.read("bars_1d").filter(pl.col("listing_id") == LID).sort("date").tail(1)
    watch.add(LID, now=NOW)
    watch.add_condition(LID, "fail", text="跌回箱顶", op="<", price=last["close"][0] * 0.9, now=NOW)
    halves = ("open", "high", "low", "close", "pre_close", "up_limit", "down_limit")
    st.publish({"bars_1d": last.with_columns(pl.col(*halves) / 2, pl.col("adj_factor") * 2)}, as_of=last["date"][0], note="split")
    assert watch.check("CN", now=NOW) == []                                           # the close is still above the line
    assert watch.entry(LID)["conditions"][0]["distance"] == pytest.approx(-0.1)


def test_an_entry_shows_the_lines_that_alert_on_its_stock(paths, watch):
    # 养箱: the trigger is a price line set to alert; the entry shows those lines next to its fail lines
    from wb.workspace.store import Workspace
    ws = Workspace(paths)
    watch.add(LID, now=NOW)
    day, close = close_on(paths)
    top = ws.save_drawing(LID, "horizontalStraightLine", [{"date": str(day), "price": round(close * 1.05, 2)}], alert=True)
    ws.save_drawing(LID, "horizontalStraightLine", [{"date": str(day), "price": round(close * 0.9, 2)}])          # no alert
    ray = ws.save_drawing(LID, "rayLine", [{"date": "2026-06-01", "price": 1500.0}, {"date": str(day), "price": 1600.0}], alert=True)
    lines = watch.entry(LID)["lines"]
    assert [x["id"] for x in lines] == [top, ray]
    assert lines[0]["price"] == round(close * 1.05, 2) and lines[0]["distance"] == pytest.approx(round(close * 1.05, 2) / close - 1, abs=1e-6)
    assert lines[1]["price"] is None and lines[1]["points"][1]["price"] == 1600.0                     # a sloped line: its points


def test_an_alert_lines_distance_keeps_to_the_prices_of_the_day_it_was_drawn_across_a_split(paths, watch):
    # a 2:1 split the day after the line was drawn halves the exchange's close: the line's distance stays where it was
    from wb.workspace.store import Workspace
    st = Store(paths, "CN")
    two = st.read("bars_1d").filter(pl.col("listing_id") == LID).sort("date").tail(2)
    last = two.tail(1)
    watch.add(LID, now=NOW)
    Workspace(paths).save_drawing(LID, "horizontalStraightLine", [{"date": str(two["date"][0]), "price": round(last["close"][0] * 1.05, 2)}],
                                  alert=True)
    before = watch.entry(LID)["lines"][0]["distance"]
    halves = ("open", "high", "low", "close", "pre_close", "up_limit", "down_limit")
    st.publish({"bars_1d": last.with_columns(pl.col(*halves) / 2, pl.col("adj_factor") * 2)}, as_of=last["date"][0], note="split")
    assert before == pytest.approx(0.05, abs=1e-3) and watch.entry(LID)["lines"][0]["distance"] == pytest.approx(before)
