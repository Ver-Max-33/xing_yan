"""Box labels (spec §5.10): your own drawing, on a chart cut at a date, of a box — edges, touches,
the extremes you left out and why, its scale, background and position — or of something that looks
like one but is not. Prices are exchange prices on their own days, as drawings are stored, so a
profile switch or a later ex-date never moves a label. Each sample label joins the tune or the test
group when it is made, 7:3 within its category, and stays there; a redraw (`redraw_of`) is your own
baseline, not a sample."""
import json
import math
import uuid
from datetime import date, datetime, timedelta

from wb.config import Paths
from wb.db import connect, tx

# the ten box classes and the not-a-box kinds of spec §5.10
CATEGORIES = {"down_base": "下横（下跌后的底部箱）", "up_relay": "上横（上涨中继箱）", "down_relay": "下跌中继",
              "high_flat": "高位横盘", "flat": "平箱", "curved": "曲箱", "slant": "斜箱", "break_return": "破箱回箱",
              "nested": "大箱套小箱（含金品）", "short_n": "突破后单底短 N 小箱"}
NOT_BOX = {"trend_pause": "单边趋势里的短暂停顿", "wide_chop": "宽幅乱震", "one_high_one_low": "只有一高一低的雏形",
           "other": "其他看着像、按定义不算的"}
SCALES = ("short", "mid", "long")
CONTEXTS = ("上横", "下横")
POSITIONS = ("低位", "中继", "高位")
VERDICTS = ("algo_wrong", "me_wrong", "both_ok")
_FIELDS = ("listing_id", "cutoff", "is_box", "category", "start", "end", "top", "bottom", "scale", "context", "position",
           "touches", "exclusions", "redraw_of", "note")


class LabelError(ValueError):
    pass


def _d(v) -> str:
    return v.isoformat() if isinstance(v, date) else date.fromisoformat(str(v)).isoformat()


class Labels:
    def __init__(self, paths: Paths):
        self.conn = connect(paths.shared_db, "shared")

    # ---- checks ------------------------------------------------------------------------------
    def _check(self, x: dict, own_id: str | None = None) -> dict:
        errs = []
        try:
            for k in ("cutoff", "start", "end"):
                x[k] = _d(x[k])
        except (KeyError, ValueError, TypeError):
            raise LabelError("cutoff / start / end 要写成日期") from None
        x["is_box"] = bool(x.get("is_box", True))
        kinds = CATEGORIES if x["is_box"] else NOT_BOX
        if x.get("category") not in kinds:
            errs.append(f"category：{'是箱体' if x['is_box'] else '不是箱体'}的类别要选 {'、'.join(kinds)} 之一")
        if not x["start"] <= x["end"] <= x["cutoff"]:
            errs.append("起止要在截止日之前：start ≤ end ≤ 截止日（标注时看不到截止日之后的 K 线）")
        try:
            x["top"], x["bottom"] = float(x["top"]), float(x["bottom"])
        except (KeyError, TypeError, ValueError):
            errs.append("上沿、下沿要写价格")
        else:
            if not (math.isfinite(x["top"]) and math.isfinite(x["bottom"])):
                errs.append("上沿、下沿要写有限的价格")
            elif not x["top"] > x["bottom"] > 0:
                errs.append("上沿要高于下沿，价格要大于 0")
        for k, allowed in (("scale", SCALES), ("context", CONTEXTS), ("position", POSITIONS)):
            if x.get(k) not in (None, *allowed):
                errs.append(f"{k}：要选 {'、'.join(allowed)} 之一")
        try:
            x["touches"] = [{"date": _d(t["date"]), "side": t["side"], "price": float(t["price"])} for t in x.get("touches") or []]
            x["exclusions"] = [{"date": _d(e["date"]), "kind": str(e["kind"]), "price": float(e["price"]),
                                "reason": str(e.get("reason", ""))} for e in x.get("exclusions") or []]
        except (KeyError, TypeError, ValueError):
            errs.append("触点要写 {date, side, price}，排除项要写 {date, kind, price, reason}")
        else:
            marks = x["touches"] + x["exclusions"]
            if any(t["side"] not in ("top", "bottom") for t in x["touches"]):
                errs.append("触点的 side 要写 top 或 bottom")
            if any(not math.isfinite(m["price"]) for m in marks):
                errs.append("触点和排除项要写有限的价格")
            if errs == [] and any(m["date"] > x["cutoff"] for m in marks):
                errs.append("触点和排除项要在截止日之前（标注时看不到截止日之后的 K 线）")
        if own_id is not None and x.get("redraw_of") == own_id:
            errs.append("redraw_of：一个标注不能重画自己")
        elif x.get("redraw_of"):
            errs += self._redraw_errors(x["redraw_of"], own_id)
        if errs:
            raise LabelError("\n".join(errs))
        x["note"] = str(x.get("note") or "")
        return x

    def _redraw_errors(self, original: str, own_id: str | None) -> list[str]:
        """§5.10: each sample is redrawn once, at least a week after it was drawn, so the baseline is not
        drawn from memory; a redraw is not itself redrawn."""
        row = self.conn.execute("SELECT redraw_of, created_at FROM box_label WHERE id=?", (original,)).fetchone()
        if row is None:
            return [f"redraw_of：没有标注 {original}"]
        if row[0]:
            return ["redraw_of：重画的是样例，不能重画一个重画"]
        if self.conn.execute("SELECT 1 FROM box_label WHERE redraw_of=? AND id IS NOT ?", (original, own_id)).fetchone():
            return ["redraw_of：每个样例只重画一次"]
        if own_id is None and datetime.now() - datetime.fromisoformat(row[1]) < timedelta(days=7):
            return ["redraw_of：画完至少一周以后才能重画（免得凭记忆画）"]
        return []

    def _group(self, x: dict) -> str:
        """Tune or test, 7:3 within the category (all not-a-box labels are one stratum); a redraw
        takes its original's group. When a category's test slot falls due, it goes to this label if its
        market has no more test labels than the other one, else to the category's next label — so the
        test group holds both markets whatever order you label in."""
        if x.get("redraw_of"):
            return self.conn.execute("SELECT grp FROM box_label WHERE id=?", (x["redraw_of"],)).fetchone()[0]
        where = "category=?" if x["is_box"] else "is_box=0"
        args = (x["category"],) if x["is_box"] else ()
        rows = self.conn.execute(f"SELECT grp, COUNT(*) FROM box_label WHERE {where} AND redraw_of IS NULL GROUP BY grp",
                                 args).fetchall()
        n = {g: c for g, c in rows}
        test, total = n.get("test", 0), sum(n.values())
        if test >= round(0.3 * (total + 1)):
            return "tune"
        if test < round(0.3 * total):  # the slot was due at the category's previous label and passed on: take it
            return "test"
        market = x["listing_id"].split(":")[0]
        per = dict(self.conn.execute("SELECT substr(listing_id, 1, instr(listing_id, ':') - 1), COUNT(*) FROM box_label "
                                     "WHERE grp='test' AND redraw_of IS NULL GROUP BY 1").fetchall())
        others = [c for m, c in per.items() if m != market] or [0]
        return "test" if per.get(market, 0) <= min(others) else "tune"

    # ---- storage -----------------------------------------------------------------------------
    def add(self, data: dict) -> str:
        x = self._check({k: data.get(k) for k in _FIELDS})
        lid = uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        with tx(self.conn):
            grp = self._group(x)
            self.conn.execute(
                "INSERT INTO box_label (id, listing_id, cutoff, is_box, category, start, end, top, bottom, scale, context, "
                "position, touches, exclusions, grp, redraw_of, note, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid, x["listing_id"], x["cutoff"], int(x["is_box"]), x["category"], x["start"], x["end"], x["top"],
                 x["bottom"], x.get("scale"), x.get("context"), x.get("position"), json.dumps(x["touches"], ensure_ascii=False),
                 json.dumps(x["exclusions"], ensure_ascii=False), grp, x.get("redraw_of"), x["note"], now, now))
        return lid

    def update(self, label_id: str, data: dict) -> None:
        old = self.get(label_id)
        x = self._check({**{k: old[k] for k in _FIELDS}, **{k: v for k, v in data.items() if k in _FIELDS}}, own_id=label_id)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE box_label SET listing_id=?, cutoff=?, is_box=?, category=?, start=?, end=?, top=?, bottom=?, scale=?, "
                "context=?, position=?, touches=?, exclusions=?, redraw_of=?, note=?, updated_at=? WHERE id=?",
                (x["listing_id"], x["cutoff"], int(x["is_box"]), x["category"], x["start"], x["end"], x["top"], x["bottom"],
                 x.get("scale"), x.get("context"), x.get("position"), json.dumps(x["touches"], ensure_ascii=False),
                 json.dumps(x["exclusions"], ensure_ascii=False), x.get("redraw_of"), x["note"],
                 datetime.now().isoformat(timespec="seconds"), label_id))

    def set_verdict(self, label_id: str, verdict: str | None) -> None:
        if verdict not in (None, *VERDICTS):
            raise LabelError(f"verdict 要选 {'、'.join(VERDICTS)} 之一")
        self.get(label_id)
        with tx(self.conn):
            self.conn.execute("UPDATE box_label SET verdict=? WHERE id=?", (verdict, label_id))

    def delete(self, label_id: str) -> None:
        with tx(self.conn):
            self.conn.execute("DELETE FROM box_label WHERE id=?", (label_id,))

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["is_box"] = bool(d["is_box"])
        d["touches"], d["exclusions"] = json.loads(d["touches"]), json.loads(d["exclusions"])
        return d

    def get(self, label_id: str) -> dict:
        r = self.conn.execute("SELECT * FROM box_label WHERE id=?", (label_id,)).fetchone()
        if r is None:
            raise LabelError(f"没有标注 {label_id}")
        return self._row(r)

    def all(self, listing_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM box_label" + (" WHERE listing_id=?" if listing_id else "") + " ORDER BY created_at, rowid"
        return [self._row(r) for r in self.conn.execute(q, (listing_id,) if listing_id else ())]


class HitLabels:
    """Your right / wrong verdicts on a rule's hits (spec §6.3 Y/N), per rule id."""

    def __init__(self, paths: Paths):
        self.conn = connect(paths.shared_db, "shared")

    def set(self, rule_id: str, listing_id: str, signal_date, verdict: str | None, version: int | None = None,
            rule_hash: str | None = None) -> None:
        """`verdict` None clears it."""
        if verdict not in (None, "right", "wrong"):
            raise LabelError("verdict 要写 right 或 wrong（清除写 null）")
        if not (version is None or (isinstance(version, int) and not isinstance(version, bool))):
            raise LabelError("version 要写整数版本号")
        if not (rule_hash is None or isinstance(rule_hash, str)):
            raise LabelError("hash 要写文字")
        day = _d(signal_date)
        with tx(self.conn):
            if verdict is None:
                self.conn.execute("DELETE FROM hit_label WHERE rule_id=? AND listing_id=? AND signal_date=?",
                                  (rule_id, listing_id, day))
            else:
                self.conn.execute(
                    "INSERT INTO hit_label VALUES (?,?,?,?,?,?,?) ON CONFLICT (rule_id, listing_id, signal_date) DO UPDATE SET "
                    "verdict=excluded.verdict, rule_version=excluded.rule_version, rule_hash=excluded.rule_hash, "
                    "created_at=excluded.created_at",
                    (rule_id, listing_id, day, verdict, version, rule_hash, datetime.now().isoformat(timespec="seconds")))

    def all(self, rule_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT listing_id, signal_date, verdict, rule_version, rule_hash, created_at FROM hit_label "
                                 "WHERE rule_id=? ORDER BY signal_date, listing_id", (rule_id,))
        return [dict(r) for r in rows]
