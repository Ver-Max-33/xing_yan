"""盘中提醒 (spec §7.2, §8.5 预警中心): each alert of the day is kept with what was seen; a rule alert — a 潜伏 stock's
next stage met on today's bar — comes once a day per rule and stock and waits for the close, which confirms it when
the day's scan signalled it and revokes it otherwise; a price alert needs no confirmation; a stock you muted is quiet
until the next day. Feishu hears only the alerts marked for it (design 2026-10-10 §4): not rule alerts, not verdicts."""
from datetime import date, datetime, timedelta

from wb.realtime.alerts import Alerts, notices

DAY = date(2026, 10, 2)


def add(a, listing_id="JP:7203", kind="rule", key="tpl_x", at=(10, 31), day=DAY, **kw):
    return a.add(day=day, market=listing_id.split(":")[0], listing_id=listing_id, kind=kind, key=key,
                 at=datetime(day.year, day.month, day.day, *at), price=2861.0, title="トヨタ自動車 7203 · 日股",
                 text=kw.pop("text", "上横上 v3 · 盘中满足突破条件"), **kw)


def test_a_rule_alert_comes_once_a_day_and_waits_for_the_close(paths):
    a = Alerts(paths)
    first = add(a)
    assert first["status"] == "pending" and add(a, at=(10, 33)) is None
    assert add(a, listing_id="JP:6758")["status"] == "pending"                     # another stock, the same rule
    assert [x["listing_id"] for x in a.day(DAY)] == ["JP:7203", "JP:6758"]


def test_a_price_alert_needs_no_confirmation_and_a_muted_stock_is_quiet_for_the_day(paths):
    a = Alerts(paths)
    assert add(a, kind="line", key="d1", text="触及你画的线 2,860")["status"] == "none"
    assert add(a, kind="line", key="d1", at=(11, 2)) is not None                  # crossings are rationed upstream
    a.mute(DAY, "JP:7203")
    assert add(a, kind="box", key="tpl_x") is None and a.muted(DAY) == {"JP:7203"}
    assert add(a, listing_id="JP:6758", kind="ex", key="ex")["status"] == "none"            # an ex-rights day: said once
    assert add(a, listing_id="JP:6758", kind="ex", key="ex", at=(11, 0)) is None
    assert add(a, kind="box", key="tpl_x", day=DAY + timedelta(days=1)) is not None


def test_the_close_confirms_what_the_days_scan_signalled_and_revokes_the_rest(paths):
    a = Alerts(paths)
    add(a), add(a, listing_id="JP:6758"), add(a, kind="line", key="d1")
    out = a.resolve("JP", DAY, {("tpl_x", "JP:7203")})
    assert {x["listing_id"]: x["status"] for x in out} == {"JP:7203": "confirmed", "JP:6758": "revoked"}
    assert a.resolve("JP", DAY, set()) == [] and a.day(DAY)[2]["status"] == "none"   # once; price alerts stay as they were


def test_feishu_hears_only_the_alerts_marked_for_it_and_a_quiet_source_without_its_numbers(paths):
    a = Alerts(paths)
    rule = add(a)                                                                  # a rule alert: in the app only
    near = add(a, kind="near", key="box:JP:7203", at=(10, 32), text="可能突破，注意看：离箱顶 2,900 还差 0.8%",
               detail={"notify": True, "what": "可能突破，注意看：箱顶", "side": "up", "move2": 0.012, "vol3": 3.46,
                       "vol_ratio": 2.1, "vwap_gap": 0.018, "to_limit": 0.062, "tags": ["大盘弱"], "history": "同类提醒约六成当天碰线"})
    app = add(a, listing_id="JP:6758", kind="touch", key="box:JP:6758", text="已到箱顶 3,300", detail={"notify": False})
    out = notices([rule, near, app])
    assert [n["idem"] for n in out] == [f"live|{near['id']}"] and out[0]["day"] == "2026-10-02"
    assert out[0]["payload"]["line"] == ("10:32 · 2,861 · 可能突破，注意看：离箱顶 2,900 还差 0.8% · 2 分钟 +1.2% · 近 3 分钟量 3.5 倍 · "
                                         "量比 2.1 · 均价线上 1.8 点 · 离涨停 6.2 点 · 大盘弱 · 同类提醒约六成当天碰线")
    assert notices([near], quiet={"JP"})[0]["payload"]["line"] == "10:32 · 可能突破，注意看：箱顶"   # §4.10: no number
    assert add(a, kind="near", key="box:JP:7203", at=(10, 40)) is None                 # a line's layer: once a day
