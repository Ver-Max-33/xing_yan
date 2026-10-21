"""盘中提醒 (spec §7.2, §8.5 预警中心; design 2026-10-10): the day's intraday alerts, kept in the profile's app database —
never in the signal history. A line's alerts (竞价、逼近、触线 and 持有中's reminder before the close) come once a day per
line and layer; the market's fall once a day; a rule alert (a 潜伏 stock's next stage met on today's bar) once a day per
rule and stock, waiting for the close, which confirms it when the day's scan signalled that stock and revokes it
otherwise. A stock you mute is quiet until its next day. Feishu hears only the alerts marked for it (detail.notify: the
buying side of a stock 观察中 or 待研究, the selling side of one 持有中, the lines you drew on a stock you watch, the
market's fall, the feed's outage) — the rest, rule alerts and the close's verdicts among them, are in the app."""
import json
from datetime import date, datetime

from wb.config import Paths
from wb.db import connect

# ex: a price beyond the day's limits — an ex-rights day, not judged today; box and line: price alerts before 2026-10-10
KINDS = ("rule", "box", "line", "ex", "auction", "near", "touch", "hold", "market", "feed")
ONCE = ("rule", "ex", "auction", "near", "touch", "hold", "market")   # once a day per key and stock


def _price(v: float | None, market: str) -> str:
    return "" if v is None else f"{v:,.{2 if market == 'CN' else 1}f}".rstrip("0").rstrip(".")


class Alerts:
    def __init__(self, paths: Paths):
        self.conn = connect(paths.app_db, "app")

    def _row(self, r) -> dict:
        return {**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None}

    def add(self, *, day: date, market: str, listing_id: str, kind: str, key: str, at: datetime, price: float | None,
            title: str, text: str, detail: dict | None = None) -> dict | None:
        """The alert as kept, or None: the stock is muted today, or this (kind, key) already spoke about it today."""
        d = day.isoformat()
        if self.conn.execute("SELECT 1 FROM live_mute WHERE day=? AND listing_id=?", (d, listing_id)).fetchone():
            return None
        if kind in ONCE and self.conn.execute("SELECT 1 FROM live_alert WHERE day=? AND kind=? AND key=? AND listing_id=?",
                                                        (d, kind, key, listing_id)).fetchone():
            return None
        cur = self.conn.execute(
            "INSERT INTO live_alert (day, market, listing_id, kind, key, at, price, title, text, detail, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (d, market, listing_id, kind, key, at.isoformat(timespec="seconds"), price, title, text,
             json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None, "pending" if kind == "rule" else "none"))
        return self._row(self.conn.execute("SELECT * FROM live_alert WHERE id=?", (cur.lastrowid,)).fetchone())

    def day(self, day: date, market: str | None = None) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM live_alert WHERE day=? AND (? IS NULL OR market=?) ORDER BY at, id",
                                 (day.isoformat(), market, market))
        return [self._row(r) for r in rows]

    def mute(self, day: date, listing_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO live_mute VALUES (?, ?)", (day.isoformat(), listing_id))

    def muted(self, day: date) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT listing_id FROM live_mute WHERE day=?", (day.isoformat(),))}

    def resolve(self, market: str, day: date, signals: set[tuple[str, str]], now: datetime | None = None,
                rule: str | None = None) -> list[dict]:
        """The day's pending rule alerts in `market` (of `rule` only, when given), judged by its scan: (rule id,
        listing) in `signals` → confirmed, otherwise revoked."""
        out = []
        at = (now or datetime.now()).isoformat(timespec="seconds")
        for r in self.conn.execute("SELECT * FROM live_alert WHERE day=? AND market=? AND status='pending' AND (? IS NULL OR key=?) "
                                   "ORDER BY at, id", (day.isoformat(), market, rule, rule)).fetchall():
            status = "confirmed" if (r["key"], r["listing_id"]) in signals else "revoked"
            self.conn.execute("UPDATE live_alert SET status=?, resolved_at=? WHERE id=?", (status, at, r["id"]))
            out.append({**self._row(r), "status": status, "resolved_at": at})
        return out


def _facts(d: dict) -> list[str]:
    """What the moment looks like, for the card (design 2026-10-10 §3)."""
    out = []
    if d.get("move2") is not None:
        out.append(f"2 分钟 {d['move2'] * 100:+.1f}%")
    if d.get("vol3") is not None:
        out.append(f"近 3 分钟量 {d['vol3']:.1f} 倍")
    if d.get("vol_ratio") is not None:
        out.append(f"量比 {d['vol_ratio']:.1f}")
    if d.get("vwap_gap") is not None:
        out.append(f"均价线{'上' if d['vwap_gap'] >= 0 else '下'} {abs(d['vwap_gap']) * 100:.1f} 点")
    if d.get("to_limit") is not None:
        out.append(f"离{'涨停' if d.get('side') == 'up' else '跌停'} {d['to_limit'] * 100:.1f} 点")
    return out + list(d.get("tags") or []) + ([d["history"]] if d.get("history") else [])


def notices(alerts: list[dict], quiet: set[str] = frozenset()) -> list[dict]:
    """Outbox notices (kind `live`) of the alerts marked for Feishu: the time, the price and what happened, then the
    moment's facts; a market in `quiet` (its source may not put its numbers into notifications, §4.10) has its words
    without a number."""
    out = []
    for a in alerts:
        d = a["detail"] or {}
        if not d.get("notify"):
            continue
        if a["market"] in quiet:
            line = f"{a['at'][11:16]} · {d.get('what') or a['text']}"
        else:
            line = " · ".join([a["at"][11:16], *([_price(a["price"], a["market"])] if a["price"] is not None else []), a["text"], *_facts(d)])
        out.append({"kind": "live", "idem": f"live|{a['id']}", "market": a["market"], "day": a["day"],
                    "payload": {"title": a["title"], "line": line}})
    return out
