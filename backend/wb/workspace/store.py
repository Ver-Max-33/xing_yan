"""Workspace persistence in shared.sqlite (spec §11): settings, watchlists, drawings, recent."""
import json
import re
import uuid
from datetime import datetime

from wb.config import Paths
from wb.db import connect, tx
from wb.datause import defaults
from wb.markets import MARKETS

LAYERS = ("auction", "near", "touch", "hold", "crash", "feed")   # 竞价、逼近、触线、持有 收盘前、大盘急跌、行情中断

DEFAULTS: dict[str, object] = {
    "profile.CN": "test",      # data profile per market (spec §4.3)
    "profile.JP": "test",
    "theme": "dark",           # dark | light | system
    "colors": "red_up",        # red_up (红涨绿跌, default) | green_up
    "ui.font": "system",       # the UI's font: the system's (苹方 / 微软雅黑 with SF / Segoe) | geist
    "ui.size": 16,             # the base font size in px the whole UI scales from (16: the components' own)
    "notify.live": True,   # 盘中提醒 to Feishu, from a realtime source only (spec §7.1, 1.34); the rest is the 开盘前简报
    # 开盘前简报 (spec §7.1, 1.34): per market on / off ("auto": on for a market on the formal profile — a test
    # profile's data is only a sample), when (the market's own clock), how much of each section
    "brief.CN": "auto", "brief.JP": "auto", "brief.time.CN": "08:50", "brief.time.JP": "08:40",
    "brief.per_rule": 3, "brief.near_pct": 3, "brief.near_max": 8, "brief.empty": True,
    # 盘中提醒 (spec §7.2): no volume-type alerts this many minutes after the open; 潜伏 box tops as price lines (of
    # stocks you do not watch: said in the app only); the layers of design 2026-10-10 §3, the approach's zone and the
    # market's fall (percent)
    "live.quiet_minutes": 15,
    **{f"live.{k}": True for k in LAYERS}, "live.zone": 1.0, "live.crash_pct": 2.0,
    # 市场看板 (design 2026-10-12 §8.2): a row of the whole market a minute while A shares trade (about 140 MB a day)
    "market.tape": True,
    # 分钟线提前取 (10-13): the whole market's 1-minute bars of the last 60 trading days fetched outside the session (about
    # 1.5–2 GB; a minute chart's indicators warm up on them), else a stock's when its chart opens
    "minutes.prefetch": True,
    # 个股研究 (spec §10.3): each market's new signals researched by AI after the daily scan, at most this many (0: none)
    "research.daily": 0,
    **defaults(),   # 数据使用开关 (spec §4.10): datause.<source>.<store | llm | notify | export>
}


CHOICES = {"profile": ("test", "prod"), "theme": ("dark", "light", "system"), "colors": ("red_up", "green_up"), "ui.font": ("system", "geist"),
           "notify": (True, False), "market.tape": (True, False), "minutes.prefetch": (True, False), "datause": (True, False), **{f"live.{k}": (True, False) for k in LAYERS},
           "brief.CN": ("auto", "on", "off"), "brief.JP": ("auto", "on", "off"), "brief.empty": (True, False)}
WHOLE = {"live.quiet_minutes": (0, 120), "research.daily": (0, 10), "ui.size": (14, 20),   # whole numbers in this range
         "brief.per_rule": (1, 10), "brief.near_pct": (1, 10), "brief.near_max": (1, 20)}
NUMBERS = {"live.zone": (0.2, 3.0), "live.crash_pct": (0.5, 5.0)}   # a number in this range
TIMES = ("brief.time.CN", "brief.time.JP")   # a time of day, HH:MM


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Workspace:
    def __init__(self, paths: Paths):
        self.conn = connect(paths.shared_db, "shared")

    # ---- settings ------------------------------------------------------------------------
    def settings(self) -> dict[str, object]:
        stored = {r["key"]: json.loads(r["value"]) for r in self.conn.execute("SELECT key, value FROM setting")}
        return {**DEFAULTS, **stored}

    def set_setting(self, key: str, value: object) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting {key!r}")
        allowed = CHOICES.get(key, CHOICES.get(key.split(".")[0]))
        if allowed and (type(value) is not type(allowed[0]) or value not in allowed):   # 1 is not True
            raise ValueError(f"{key} must be one of {', '.join(map(str, allowed))}")
        if key in WHOLE and not (type(value) is int and WHOLE[key][0] <= value <= WHOLE[key][1]):
            raise ValueError(f"{key} must be a whole number from {WHOLE[key][0]} to {WHOLE[key][1]}")
        if key in NUMBERS and not (type(value) in (int, float) and NUMBERS[key][0] <= value <= NUMBERS[key][1]):
            raise ValueError(f"{key} must be a number from {NUMBERS[key][0]} to {NUMBERS[key][1]}")
        if key in TIMES and not (isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value)):
            raise ValueError(f"{key} must be a time like 08:50")
        self.conn.execute("INSERT OR REPLACE INTO setting VALUES (?, ?)", (key, json.dumps(value)))

    def profile(self, market: str) -> str:
        if market not in MARKETS:
            raise ValueError(market)
        return str(self.settings()[f"profile.{market}"])

    # ---- watchlists ----------------------------------------------------------------------
    def groups(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT g.id, g.name, g.sort, COUNT(i.listing_id) AS n FROM watch_group g "
            "LEFT JOIN watch_item i ON i.group_id = g.id GROUP BY g.id ORDER BY g.sort, g.id")
        return [dict(r) for r in rows]

    def add_group(self, name: str) -> int:
        sort = self.conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM watch_group").fetchone()[0]
        cur = self.conn.execute("INSERT INTO watch_group (name, sort, created_at) VALUES (?, ?, ?)", (name, sort, _now()))
        return int(cur.lastrowid)

    def delete_group(self, group_id: int) -> None:
        self.conn.execute("DELETE FROM watch_group WHERE id=?", (group_id,))

    def items(self, group_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT listing_id, note, added_at FROM watch_item WHERE group_id=? ORDER BY rowid", (group_id,))
        return [dict(r) for r in rows]

    def add_item(self, group_id: int, listing_id: str, note: str = "") -> None:
        self.conn.execute("INSERT OR IGNORE INTO watch_item VALUES (?, ?, ?, ?)", (group_id, listing_id, note, _now()))

    def remove_item(self, group_id: int, listing_id: str) -> None:
        self.conn.execute("DELETE FROM watch_item WHERE group_id=? AND listing_id=?", (group_id, listing_id))

    def groups_of(self, listing_id: str) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT group_id FROM watch_item WHERE listing_id=?", (listing_id,))]

    # ---- drawings ------------------------------------------------------------------------
    def drawings(self, listing_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM drawing WHERE listing_id=? ORDER BY created_at, rowid", (listing_id,))
        return [{**dict(r), "points": json.loads(r["points"]), "styles": json.loads(r["styles"]),
                 "alert": bool(r["alert"])} for r in rows]

    def alerted(self, market: str) -> list[str]:
        """The stocks of `market` with a line you drew and ticked 提醒 on: 盯盘 follows them (design 2026-10-10 §2)."""
        rows = self.conn.execute("SELECT DISTINCT listing_id FROM drawing WHERE alert=1 AND listing_id LIKE ? ORDER BY listing_id",
                                 (f"{market}:%",))
        return [r["listing_id"] for r in rows]

    def save_drawing(self, listing_id: str, kind: str, points: list[dict], styles: dict | None = None,
                     alert: bool = False, drawing_id: str | None = None) -> str:
        for p in points:
            if not isinstance(p.get("date"), str) or not isinstance(p.get("price"), int | float):
                raise ValueError("each point needs a date string and a raw price")
        drawing_id = drawing_id or uuid.uuid4().hex
        now = _now()
        with tx(self.conn):
            exists = self.conn.execute("SELECT created_at FROM drawing WHERE id=?", (drawing_id,)).fetchone()
            self.conn.execute(
                "INSERT OR REPLACE INTO drawing VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (drawing_id, listing_id, kind, json.dumps(points), json.dumps(styles or {}), int(alert),
                 exists["created_at"] if exists else now, now))
        return drawing_id

    def delete_drawing(self, drawing_id: str) -> None:
        self.conn.execute("DELETE FROM drawing WHERE id=?", (drawing_id,))

    # ---- recently viewed -----------------------------------------------------------------
    def touch_recent(self, listing_id: str, keep: int = 50) -> None:
        with tx(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO recent VALUES (?, ?)", (listing_id, datetime.now().isoformat()))
            # a view written later has the higher rowid: the order when the clock reads the same (Windows' did)
            self.conn.execute("DELETE FROM recent WHERE listing_id NOT IN "
                              "(SELECT listing_id FROM recent ORDER BY viewed_at DESC, rowid DESC LIMIT ?)", (keep,))

    # ---- 题材 tags (spec §7.3) -------------------------------------------------------------------
    def set_tags(self, listing_id: str, tags: list[str]) -> list[str]:
        """The listing's tags, replaced: trimmed, empty ones and repeats dropped."""
        clean = sorted({t.strip() for t in tags if t.strip()})
        with tx(self.conn):
            self.conn.execute("DELETE FROM listing_tag WHERE listing_id=?", (listing_id,))
            self.conn.executemany("INSERT INTO listing_tag VALUES (?, ?, ?)", [(listing_id, t, _now()) for t in clean])
        return clean

    def tags_of(self, listing_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT tag FROM listing_tag WHERE listing_id=? ORDER BY tag", (listing_id,))]

    def all_tags(self) -> list[dict]:
        """Every tag in use with how many listings carry it, the most used first."""
        rows = self.conn.execute("SELECT tag, COUNT(*) AS n FROM listing_tag GROUP BY tag ORDER BY n DESC, tag")
        return [dict(r) for r in rows]

    def recent(self, limit: int = 20) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT listing_id FROM recent ORDER BY viewed_at DESC, rowid DESC LIMIT ?",
                                                (limit,))]
