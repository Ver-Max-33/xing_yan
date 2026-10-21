"""观察条目 (spec §11, §2.1): a stock you watch. Where it came from and at what close; its status (待研究、观察中、
持有中 — a mark you set by hand, no shares or broker —、暂不考虑、已关闭); why, what speaks for and against it and your notes, every version kept; fail lines
(a close crossing a price) and review dates, checked after each close and each fired once; how it has done since
it was added, against the market's benchmark — also once set aside, so the record is not only the successes; and
the record of what happened. Kept in shared.sqlite: the same in both data profiles."""
import json
from collections.abc import Callable
from datetime import date, datetime

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import build_frame
from wb.engine.l2.run import l2_bars
from wb.engine.l2.spec import compute
from wb.db import connect, tx
from wb.markets import MARKETS, load_market

_NO_BARS = pl.DataFrame(schema={"date": pl.Date, "close": pl.Float64, "hfq": pl.Float64})
STATUSES = {"research": "待研究", "watching": "观察中", "holding": "持有中", "passed": "暂不考虑", "closed": "已关闭"}
TEXTS = {"reason": "为什么关注", "for": "支持", "against": "反对 / 风险", "note": "我的笔记"}
ALERTING = ("research", "watching", "holding")  # 暂不考虑 and 已关闭 are still followed, but do not alert
# fail: a close crossing a price; review: a day to look again; manual: what no feed here can see (an announcement)
CONDITIONS = {"fail": "加失效条件", "review": "加复核", "manual": "加要自己对照的条件"}
HORIZON = 60   # bars a structure keeps defending after it formed or was left upward (BOX_DERIVED's fb_horizon)
BOX_TOL = 0.015
SCALES = {"mid": "中箱", "long": "大箱"}


def _scaled(bar: dict, factor: float | None) -> float:
    """The bar's close in the prices of the day a line was set (its factor then): a split since does not cross it."""
    return bar["hfq"] / factor if factor else bar["close"]


def fail_lines(tables: dict, bars: pl.DataFrame) -> list[dict]:
    """持有中's suggested fail lines (design 2026-10-10 §2) from one listing's structures (compute()'s tables) as its last
    bar knows them, in adjusted prices: a 金品's small-box bottom (known within HORIZON bars, not superseded), a 假底 whose
    line has not ended, a mid or long box's bottom × 0.985 (live, or left upward within HORIZON bars) — those under the
    last close that no close since has crossed, highest first (the highest is the one that acts, as in a trade plan's
    stops: usually the 金品, then the 假底, then the box). `bars`: date and adjusted close."""
    bars = bars.drop_nulls("close")   # a day without trading has no close
    if bars.is_empty():
        return []
    dates, closes = bars["date"].to_list(), bars["close"].to_list()
    recent = dates[max(0, len(dates) - HORIZON)]
    derived, out = tables.get("BOX_DERIVED", {}), []
    gold = derived.get("gold")
    for r in gold.filter(pl.col("superseded_at").is_null() & (pl.col("known_at") >= recent)).to_dicts() if gold is not None else []:
        out.append(("金品", f"{'变异' if r['variant'] else ''}{r['kind']}小箱底", r["small_bottom"], r["known_at"]))
    fb = derived.get("false_bottoms")
    for r in fb.filter(pl.col("end").is_null()).to_dicts() if fb is not None else []:
        out.append(("假底", f"假底（{r['source']}）", r["price"], r["known_at"]))
    boxes = tables.get("BOX", {}).get("boxes")
    live = pl.col("ended_at").is_null() | ((pl.col("end_reason") == "上破离箱") & (pl.col("ended_at") >= recent))
    for r in boxes.filter((pl.col("status") == "established") & pl.col("scale").is_in(list(SCALES))
                          & pl.col("superseded_at").is_null() & live).to_dicts() if boxes is not None else []:
        out.append(("箱底", f"{SCALES[r['scale']]}底 × {1 - BOX_TOL:g}", r["bottom"] * (1 - BOX_TOL), r["known_at"]))
    held = [x for x in out if x[2] is not None and x[2] < closes[-1]
            and all(c >= x[2] for d, c in zip(dates, closes, strict=True) if d > x[3])]
    order = {"金品": 0, "假底": 1, "箱底": 2}
    lines: list[dict] = []
    for source, label, price, since in sorted(held, key=lambda x: (-x[2], order[x[0]])):
        if not lines or abs(lines[-1]["price"] / price - 1) > 1e-9:   # a 金品's 假底 is its small box's bottom: said once
            lines.append({"source": source, "label": label, "price": price, "since": since})
    return lines


def _now(now: datetime | None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


class Watch:
    def __init__(self, paths_of: Callable[[str], Paths]):
        self.paths_of = paths_of
        self.conn = connect(paths_of(MARKETS[0]).shared_db, "shared")

    # ---- prices -------------------------------------------------------------------------------------------
    def _bars(self, lid: str) -> pl.DataFrame:
        return self._bars_of(lid.split(":", 1)[0], [lid]).get(lid, _NO_BARS)

    def _bars_of(self, market: str, lids: list[str]) -> dict[str, pl.DataFrame]:
        """date, the exchange's close and the adjusted close of each listing, read as formulas read them (a missing
        factor carried forward, a bad row unknown): one read of the market's bars for all of them."""
        paths = self.paths_of(market)
        store = Store(paths, market)
        snap = store.latest()
        if snap is None or not lids:
            return {}
        bars = (build_frame(store, market, paths.profile, {"close", "raw_close"}, lids, snap=snap)
                .select("listing_id", "date", pl.col("raw_close").alias("close"), pl.col("close").alias("hfq"))
                .drop_nulls(["close", "hfq"]))
        return {k[0]: g.drop("listing_id") for k, g in bars.group_by("listing_id", maintain_order=True)}

    def _benchmark(self, market: str) -> tuple[str, dict[date, float]]:
        paths = self.paths_of(market)
        iid = load_market(market).benchmark[paths.profile]
        store = Store(paths, market)
        if store.latest() is None:
            return iid, {}
        idx = store.scan("index_bars_1d").filter(pl.col("index_id") == iid).select("date", "close").collect()
        return iid, dict(zip(idx["date"], idx["close"], strict=True))

    # ---- entries ------------------------------------------------------------------------------------------
    def add(self, lid: str, source: str = "", reason: str = "", now: datetime | None = None, day: date | None = None,
            status: str = "watching") -> dict:
        """Watch `lid` from its close on `day` (or its latest close). Once per stock: adding it again keeps it, and
        one that was closed is watched again."""
        row = self.conn.execute("SELECT status FROM watch_entry WHERE listing_id=?", (lid,)).fetchone()
        if row:
            if row["status"] == "closed":
                self.set_status(lid, status, now=now, why="重新观察")
            return self.entry(lid)
        bars = self._bars(lid)
        if day is not None:
            bars = bars.filter(pl.col("date") <= day)
        with tx(self.conn):
            self._insert(lid, bars, source, self._status(status), now)
        if reason.strip():
            self.set_text(lid, "reason", reason, now=now)
        return self.entry(lid)

    def add_many(self, lids: list[str], source: str = "", now: datetime | None = None) -> int:
        """Watch each of `lids` from its latest close, as `add` does one — the bars read once a market (选股's 全选 adds
        hundreds at once). One watched already stays as it is, one closed is watched again. -> how many are new."""
        lids = list(dict.fromkeys(lids))
        if not lids:
            return 0
        known = dict(self.conn.execute(
            f"SELECT listing_id, status FROM watch_entry WHERE listing_id IN ({','.join('?' * len(lids))})", lids).fetchall())
        for lid in lids:
            if known.get(lid) == "closed":
                self.set_status(lid, "watching", now=now, why="重新观察")
        fresh = [lid for lid in lids if lid not in known]
        bars: dict[str, pl.DataFrame] = {}
        for market in dict.fromkeys(lid.split(":", 1)[0] for lid in fresh):
            bars |= self._bars_of(market, [lid for lid in fresh if lid.split(":", 1)[0] == market])
        with tx(self.conn):
            for lid in fresh:
                self._insert(lid, bars.get(lid, _NO_BARS), source, "watching", now)
        return len(fresh)

    def _insert(self, lid: str, bars: pl.DataFrame, source: str, status: str, now: datetime | None) -> None:
        """A new entry from the last of `bars` (its trading day and the exchange's close), and its record."""
        last = bars.row(-1, named=True) if bars.height else None
        self.conn.execute("INSERT INTO watch_entry VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (lid, status, source, _now(now), str(last["date"]) if last else None,
                           last["close"] if last else None, _now(now)))
        self._record(lid, "加入观察" + (f"：{source}" if source else ""), now)

    def delete(self, lids: list[str]) -> int:
        """删除 (spec §11): each stock's entry and all it had — its texts with every version, conditions, the record and
        its groups — gone, unlike 已关闭, which keeps the record. -> how many entries there were."""
        lids = list(dict.fromkeys(lids))
        if not lids:
            return 0
        q = ",".join("?" * len(lids))
        with tx(self.conn):
            n = self.conn.execute(f"DELETE FROM watch_entry WHERE listing_id IN ({q})", lids).rowcount
            for table in ("watch_text", "watch_condition", "watch_record", "watch_item"):
                self.conn.execute(f"DELETE FROM {table} WHERE listing_id IN ({q})", lids)
        return n

    @staticmethod
    def _status(status: str) -> str:
        if status not in STATUSES:
            raise ValueError(f"状态只能是 {'、'.join(STATUSES.values())}")
        return status

    def set_status(self, lid: str, status: str, now: datetime | None = None, why: str | None = None) -> None:
        self._status(status)
        with tx(self.conn):
            if self.conn.execute("UPDATE watch_entry SET status=?, updated_at=? WHERE listing_id=?",
                                 (status, _now(now), lid)).rowcount == 0:
                raise KeyError(f"{lid} 不在观察里")
            self._record(lid, why or f"改为 {STATUSES[status]}", now)

    def close(self, lid: str, now: datetime | None = None) -> None:
        """Out of every group: no longer watched, its record kept."""
        row = self.conn.execute("SELECT status FROM watch_entry WHERE listing_id=?", (lid,)).fetchone()
        if row and row["status"] != "closed":
            self.set_status(lid, "closed", now=now, why="移出自选分组，关闭")

    def set_text(self, lid: str, kind: str, text: str, now: datetime | None = None) -> None:
        """A new version of the text (the same text again is not a new version)."""
        if kind not in TEXTS:
            raise ValueError(f"只能写 {'、'.join(TEXTS.values())}")
        if self._text(lid, kind) != text:
            self.conn.execute("INSERT INTO watch_text (listing_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                              (lid, kind, text, _now(now)))

    def _text(self, lid: str, kind: str) -> str:
        row = self.conn.execute("SELECT text FROM watch_text WHERE listing_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                                (lid, kind)).fetchone()
        return row[0] if row else ""

    def versions(self, lid: str, kind: str) -> list[dict]:
        rows = self.conn.execute("SELECT text, created_at FROM watch_text WHERE listing_id=? AND kind=? ORDER BY id DESC",
                                 (lid, kind))
        return [dict(r) for r in rows]

    # ---- conditions -----------------------------------------------------------------------------------------
    def add_condition(self, lid: str, kind: str, text: str, op: str | None = None, price: float | None = None,
                      due: str | None = None, now: datetime | None = None) -> int:
        if kind == "fail":
            try:
                price = float(price)
            except (TypeError, ValueError):
                raise ValueError("失效价位要写数字") from None
            if op not in ("<", ">") or not price > 0:
                raise ValueError("失效条件要写收盘价在哪个价位之下（<）或之上（>）")
        if kind == "review":
            try:
                due = date.fromisoformat(str(due)).isoformat()
            except ValueError:
                raise ValueError("复核要写日期，例如 2026-11-05") from None
        if kind not in CONDITIONS or not text.strip():
            raise ValueError("条件要写说明，种类是 fail（失效线）、review（复核日）或 manual（自己对照）")
        bars = self._bars(lid) if kind == "fail" else _NO_BARS
        factor = bars["hfq"][-1] / bars["close"][-1] if bars.height else None   # the line is in this day's prices
        with tx(self.conn):
            cur = self.conn.execute("INSERT INTO watch_condition (listing_id, kind, text, op, price, due, created_at, factor) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (lid, kind, text.strip(), op, price, due, _now(now), factor))
            self._record(lid, f"{CONDITIONS[kind]}：{text.strip()}", now)
        return int(cur.lastrowid)

    def fail_lines(self, lid: str) -> list[dict]:
        """The fail lines its structures suggest (fail_lines), in the prices of its latest close, with their distance
        from it: the one you pick is saved as a condition (收盘 < price)."""
        market = lid.split(":", 1)[0]
        paths, raw = self.paths_of(market), self._bars(lid)
        store = Store(paths, market)
        snap = store.latest()
        if snap is None or raw.is_empty():
            return []
        bars = l2_bars(store, market, paths.profile, [lid], snap=snap)
        factor = raw["hfq"][-1] / raw["close"][-1]
        out = []
        for x in fail_lines(compute(bars, ["BOX_DERIVED"]), bars):   # the chart's structures (rules' structures_of)
            price = round(x["price"] / factor, 2)
            out.append({**x, "price": price, "since": str(x["since"]), "distance": price / raw["close"][-1] - 1})
        return out

    def remove_condition(self, lid: str, cid: int, now: datetime | None = None) -> None:
        with tx(self.conn):
            row = self.conn.execute("SELECT text FROM watch_condition WHERE id=? AND listing_id=?", (cid, lid)).fetchone()
            if row:
                self.conn.execute("DELETE FROM watch_condition WHERE id=?", (cid,))
                self._record(lid, f"去掉：{row[0]}", now)

    def check(self, market: str, now: datetime | None = None) -> list[dict]:
        """After a close: the fail lines that close crossed and the review dates it reached, for the stocks being
        watched or researched; each fires once."""
        fired = []
        rows = self.conn.execute(
            "SELECT c.* FROM watch_condition c JOIN watch_entry e ON e.listing_id = c.listing_id "
            f"WHERE c.fired_day IS NULL AND c.kind != 'manual' AND e.status IN ({', '.join('?' * len(ALERTING))}) "
            "AND c.listing_id LIKE ?", (*ALERTING, f"{market}:%")).fetchall()
        closes = self._bars_of(market, sorted({c["listing_id"] for c in rows if c["kind"] == "fail"}))
        today = (now or datetime.now()).date()
        for c in rows:
            if c["kind"] == "review":  # a day on the calendar: due whether or not the stock traded
                if str(today) < c["due"]:
                    continue
                day, close = today, None
            else:
                bars = closes.get(c["listing_id"], _NO_BARS)
                if bars.is_empty():
                    continue
                last = bars.row(-1, named=True)
                day, close = last["date"], _scaled(last, c["factor"])
                if not ((c["op"] == "<" and close < c["price"]) or (c["op"] == ">" and close > c["price"])):
                    continue
            with tx(self.conn):
                self.conn.execute("UPDATE watch_condition SET fired_day=?, fired_value=? WHERE id=?", (str(day), close, c["id"]))
                self._record(c["listing_id"], f"触发失效条件：{c['text']}（收 {close:g}）" if c["kind"] == "fail"
                             else f"到了复核的日子：{c['text']}", now)
            fired.append({"id": c["id"], "listing_id": c["listing_id"], "kind": c["kind"], "text": c["text"],
                          "day": str(day), "close": close})
        return fired

    # ---- reading --------------------------------------------------------------------------------------------
    def statuses(self, market: str) -> dict[str, str]:
        return dict(self.conn.execute("SELECT listing_id, status FROM watch_entry WHERE listing_id LIKE ?", (f"{market}:%",)).fetchall())

    def selling_lines(self, market: str) -> list[dict]:
        """持有中's fail lines not yet fired that a falling price crosses (收盘 < price): id, listing_id, text, price (the
        exchange's on the day it was set), factor (adjusted ÷ exchange price then; None: set before the stock had bars),
        created_at (this computer's time)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT c.id, c.listing_id, c.text, c.price, c.factor, c.created_at FROM watch_condition c JOIN watch_entry e ON e.listing_id = c.listing_id "
            "WHERE e.status = 'holding' AND c.kind = 'fail' AND c.op = '<' AND c.fired_day IS NULL AND c.listing_id LIKE ? ORDER BY c.id",
            (f"{market}:%",))]

    def _record(self, lid: str, text: str, now: datetime | None) -> None:
        self.conn.execute("INSERT INTO watch_record (listing_id, text, at) VALUES (?, ?, ?)", (lid, text, _now(now)))

    def record(self, lid: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT text, at FROM watch_record WHERE listing_id=? ORDER BY id DESC", (lid,))]

    def performance(self, e: dict, bars: pl.DataFrame | None = None, bench: tuple | None = None) -> dict | None:
        """Since the close it was added at, against the market's benchmark over the same days (adjusted prices)."""
        if not e["added_day"]:
            return None
        bars = self._bars(e["listing_id"]) if bars is None else bars
        if bars.is_empty():
            return None
        start = date.fromisoformat(e["added_day"])
        then = bars.filter(pl.col("date") <= start)
        if then.is_empty():
            return None
        a, b = then.row(-1, named=True), bars.row(-1, named=True)
        since = b["hfq"] / a["hfq"] - 1
        iid, idx = bench or self._benchmark(e["listing_id"].split(":", 1)[0])
        bench = idx[b["date"]] / idx[a["date"]] - 1 if a["date"] in idx and b["date"] in idx else None
        return {"since": since, "benchmark": iid, "bench": bench, "excess": since - bench if bench is not None else None,
                "day": str(b["date"]), "close": b["close"]}

    def entry(self, lid: str, bars: pl.DataFrame | None = None, bench: tuple | None = None) -> dict:
        row = self.conn.execute("SELECT * FROM watch_entry WHERE listing_id=?", (lid,)).fetchone()
        if row is None:
            raise KeyError(f"{lid} 不在观察里")
        e = dict(row)
        bars = self._bars(lid) if bars is None else bars
        last = bars.row(-1, named=True) if bars.height else None
        conds = []
        for c in self.conn.execute("SELECT * FROM watch_condition WHERE listing_id=? ORDER BY id", (lid,)):
            c = dict(c)
            if c["kind"] == "fail" and last:
                c["distance"] = c["price"] / _scaled(last, c["factor"]) - 1   # how far the line is from the latest close
            if c["kind"] == "review":
                c["days_left"] = (date.fromisoformat(c["due"]) - date.today()).days
            conds.append(c)
        return {**e, "status_name": STATUSES[e["status"]], **{k: self._text(lid, k) for k in TEXTS},
                "conditions": conds, "lines": self._lines(lid, bars), "performance": self.performance(e, bars, bench),
                "record": self.record(lid)}

    def _lines(self, lid: str, bars: pl.DataFrame) -> list[dict]:
        """The lines drawn on the stock's chart that alert when a price crosses them (养箱's trigger): a level's
        price and its distance from the latest close (in today's prices: a split since does not move it), a sloped
        line's points."""
        last = bars.row(-1, named=True) if bars.height else None
        out = []
        for d in self.conn.execute("SELECT id, kind, points FROM drawing WHERE listing_id=? AND alert=1 ORDER BY created_at, rowid", (lid,)):
            pts = json.loads(d["points"])
            price = pts[0]["price"] if d["kind"] == "horizontalStraightLine" and pts else None
            distance = None
            if price is not None and last:
                then = bars.filter(pl.col("date") <= date.fromisoformat(pts[0]["date"]))
                factor = (then["hfq"][-1] / then["close"][-1]) if then.height else None
                distance = price / _scaled(last, factor) - 1   # the latest close in the line's prices
            out.append({"id": d["id"], "kind": d["kind"], "points": pts, "price": price, "distance": distance})
        return out

    def entries(self, status: str | None = None) -> list[dict]:
        q = "SELECT listing_id FROM watch_entry" + (" WHERE status=?" if status else "") + " ORDER BY added_at DESC, rowid DESC"
        lids = [r[0] for r in self.conn.execute(q, (self._status(status),) if status else ())]
        markets = sorted({lid.split(":", 1)[0] for lid in lids})
        bars = {m: self._bars_of(m, [x for x in lids if x.startswith(f"{m}:")]) for m in markets}   # one read a market
        bench = {m: self._benchmark(m) for m in markets}
        return [self.entry(lid, bars[m].get(lid, _NO_BARS), bench[m]) for lid in lids for m in [lid.split(":", 1)[0]]]
