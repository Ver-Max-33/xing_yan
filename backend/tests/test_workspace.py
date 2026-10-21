import pytest

from wb.workspace.store import Workspace


def test_settings_defaults_and_validation(paths):
    ws = Workspace(paths)
    assert ws.profile("CN") == "test" and ws.settings()["colors"] == "red_up"
    ws.set_setting("profile.CN", "prod")
    assert Workspace(paths).profile("CN") == "prod"
    with pytest.raises(ValueError):
        ws.set_setting("profile.JP", "staging")
    with pytest.raises(KeyError):
        ws.set_setting("nope", 1)


def test_watchlists(paths):
    ws = Workspace(paths)
    assert [g["name"] for g in ws.groups()] == ["自选", "养箱"]
    gid = ws.groups()[0]["id"]
    ws.add_item(gid, "JP:7203")
    ws.add_item(gid, "JP:7203")  # idempotent
    ws.add_item(gid, "CN:600519", "茅台")
    assert [i["listing_id"] for i in ws.items(gid)] == ["JP:7203", "CN:600519"]
    assert ws.groups()[0]["n"] == 2 and ws.groups_of("JP:7203") == [gid]
    new = ws.add_group("候选")
    ws.add_item(new, "JP:130A")
    ws.delete_group(new)  # cascades to its items
    assert ws.groups_of("JP:130A") == []


def test_drawings_keep_raw_anchors(paths):
    ws = Workspace(paths)
    did = ws.save_drawing("JP:7203", "segment", [{"date": "2026-09-01", "price": 2600.0},
                                                 {"date": "2026-09-30", "price": 2840.5}], {"color": "#fff"}, alert=True)
    got = ws.drawings("JP:7203")
    assert got[0]["id"] == did and got[0]["points"][1]["price"] == 2840.5 and got[0]["alert"] is True
    ws.save_drawing("JP:7203", "segment", [{"date": "2026-09-01", "price": 2601.0}], drawing_id=did)
    assert len(ws.drawings("JP:7203")) == 1 and ws.drawings("JP:7203")[0]["points"][0]["price"] == 2601.0
    with pytest.raises(ValueError):
        ws.save_drawing("JP:7203", "segment", [{"date": 20260901, "price": 1}])
    ws.delete_drawing(did)
    assert ws.drawings("JP:7203") == []


def test_recent_is_bounded_and_ordered(paths):
    ws = Workspace(paths)
    for lid in ["JP:1301", "JP:7203", "JP:1301"]:
        ws.touch_recent(lid, keep=5)
    assert ws.recent() == ["JP:1301", "JP:7203"]


def test_recent_keeps_its_order_when_the_clock_reads_the_same(paths, monkeypatch):
    # Windows' clock gave two views one time (2026-10-10, Windows test run): the latest view still comes first
    from datetime import datetime as real

    import wb.workspace.store as store

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 10, 10, 9, 30)
    monkeypatch.setattr(store, "datetime", Frozen)
    ws = Workspace(paths)
    for lid in ["JP:1301", "JP:7203", "JP:9984", "JP:1301"]:
        ws.touch_recent(lid, keep=2)
    assert ws.recent() == ["JP:1301", "JP:9984"]


def test_intraday_alert_settings(paths):
    # spec §7.2: no volume-type alerts in the first minutes after the open (15, configurable), box tops as price lines
    ws = Workspace(paths)
    s = ws.settings()
    assert (s["live.quiet_minutes"], s["notify.live"]) == (15, True)
    ws.set_setting("live.quiet_minutes", 30)
    ws.set_setting("notify.live", False)
    assert (ws.settings()["live.quiet_minutes"], ws.settings()["notify.live"]) == (30, False)
    with pytest.raises(KeyError):   # gone with 盯盘 watching your stocks only (10-12): a 潜伏 stock you do not watch is not followed
        ws.set_setting("live.box_alerts", False)
    for bad in (-1, 121, 7.5, True, "15"):
        with pytest.raises(ValueError):
            ws.set_setting("live.quiet_minutes", bad)
    with pytest.raises(ValueError):
        ws.set_setting("notify.live", "yes")


def test_each_live_layer_can_be_turned_off_and_the_zone_and_the_markets_fall_are_percents(paths):
    # design 2026-10-10 §8: 竞价、逼近、触线、持有 收盘前、大盘急跌、行情中断 on by default; 1% and 2%
    ws = Workspace(paths)
    s = ws.settings()
    assert all(s[f"live.{k}"] is True for k in ("auction", "near", "touch", "hold", "crash", "feed"))
    assert (s["live.zone"], s["live.crash_pct"]) == (1.0, 2.0)
    ws.set_setting("live.near", False)
    ws.set_setting("live.zone", 0.5)
    ws.set_setting("live.crash_pct", 3)
    assert (ws.settings()["live.near"], ws.settings()["live.zone"], ws.settings()["live.crash_pct"]) == (False, 0.5, 3)
    for key, bad in (("live.zone", 0.1), ("live.zone", True), ("live.crash_pct", 6), ("live.crash_pct", "2"), ("live.touch", 1)):
        with pytest.raises(ValueError):
            ws.set_setting(key, bad)


def test_each_data_source_has_its_four_uses_and_j_quants_is_not_exported_by_default(paths):
    # spec §4.10 数据使用开关: 本地存储 / 发送给 LLM / 出现在通知中 / 导出 per data source; J-Quants' off for 导出
    from wb.datause import SOURCES, USES, allowed, source_of
    s = Workspace(paths).settings()
    assert allowed(s, "jquants", "export") is False and allowed(s, "jquants", "llm") and allowed(s, "tushare", "export")
    assert all(f"datause.{x}.{u}" in s for x in SOURCES for u in USES)
    Workspace(paths).set_setting("datause.tushare.notify", False)
    assert allowed(Workspace(paths).settings(), "tushare", "notify") is False
    with pytest.raises(ValueError):
        Workspace(paths).set_setting("datause.tushare.notify", 0)
    assert (source_of("JP", "prod"), source_of("JP", "test"), source_of("CN", "prod")) == ("jquants", "yfinance", "tushare")


def test_the_font_and_its_size_are_yours_to_set(paths):
    # 设置 → 外观: the system's font or Geist; the base size the whole UI scales from (16 = shadcn's own)
    ws = Workspace(paths)
    assert (ws.settings()["ui.font"], ws.settings()["ui.size"]) == ("system", 16)
    ws.set_setting("ui.font", "geist")
    ws.set_setting("ui.size", 17)
    assert (ws.settings()["ui.font"], ws.settings()["ui.size"]) == ("geist", 17)
    for key, bad in (("ui.font", "comic"), ("ui.size", 30), ("ui.size", 16.5), ("ui.size", True)):
        with pytest.raises(ValueError):
            ws.set_setting(key, bad)
