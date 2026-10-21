"""The outbox (spec §7.1): a notice is queued once under its key, sent at least once, retried later when Feishu
fails (backing off), and given up only after many tries or when retrying cannot help (a wrong secret)."""
import json
from datetime import datetime, timedelta

from wb.config import Paths
from wb.db import connect
from wb.notify.feishu import Feishu, FeishuError, cards, chunks

MAX_ATTEMPTS = 12


def _iso(t: datetime) -> str:
    """The local wall clock without a zone, whatever zone the caller's time carries: one clock to compare."""
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t.isoformat(timespec="seconds")


class Outbox:
    def __init__(self, paths: Paths):
        self.conn = connect(paths.shared_db, "shared")

    def enqueue(self, kind: str, idem: str, payload: dict, market: str | None = None, day: str | None = None,
                now: datetime | None = None) -> bool:
        """Queue a notice; False when the same one was queued before (whenever)."""
        t = _iso(now or datetime.now())
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO outbox (idem, kind, market, day, payload, created_at, next_try_at) VALUES (?,?,?,?,?,?,?)",
            (idem, kind, market, day, json.dumps(payload, ensure_ascii=False, default=str), t, t))
        return cur.rowcount == 1

    def pending(self, now: datetime) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? AND next_try_at <= ? ORDER BY id",
            (MAX_ATTEMPTS, _iso(now)))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def mark_sent(self, ids: list[int], now: datetime) -> None:
        self.conn.executemany("UPDATE outbox SET sent_at=?, last_error=NULL WHERE id=?", [(_iso(now), i) for i in ids])

    def mark_failed(self, ids: list[int], error: str, now: datetime, retry: bool = True) -> None:
        for i in ids:
            n = self.conn.execute("SELECT attempts FROM outbox WHERE id=?", (i,)).fetchone()[0] + 1
            later = now + timedelta(minutes=min(60, 2 ** (n - 1)))   # 1, 2, 4 … 60 minutes
            self.conn.execute("UPDATE outbox SET attempts=?, last_error=?, next_try_at=? WHERE id=?",
                              (n if retry else MAX_ATTEMPTS, error, _iso(later), i))

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def stats(self) -> dict:
        q = lambda w: self.conn.execute(f"SELECT COUNT(*) FROM outbox WHERE {w}", (MAX_ATTEMPTS,)).fetchone()[0]  # noqa: E731
        last = self.conn.execute("SELECT last_error FROM outbox WHERE last_error IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return {"pending": q("sent_at IS NULL AND attempts < ?"), "given_up": q("sent_at IS NULL AND attempts >= ?"),
                "last_error": last[0] if last else None}


def drain(box: Outbox, bot: Feishu, now: datetime, title: str = "形研") -> int:
    """Send what is due: a whole card (开盘前简报) on its own — given up once it has expired (a briefing after the
    close) — and the other notices merged into as few cards as fit. Returns how many went out."""
    items = box.pending(now)
    sent = 0
    for it in [i for i in items if "card" in i["payload"]]:
        if it["payload"].get("expires") and _iso(now) > it["payload"]["expires"]:
            box.mark_failed([it["id"]], "过了收盘，简报不再发", now, retry=False)
            continue
        try:
            bot.send(it["payload"]["card"])
        except FeishuError as e:
            box.mark_failed([it["id"]], str(e), now, retry=e.retry)
            if e.retry:
                return sent
            continue
        box.mark_sent([it["id"]], now)
        sent += 1
    for group in chunks([{**i["payload"], "_id": i["id"], "kind": i["kind"]} for i in items if "card" not in i["payload"]]):
        ids = [g["_id"] for g in group]
        try:
            bot.send(cards(group, title)[0])
        except FeishuError as e:
            box.mark_failed(ids, str(e), now, retry=e.retry)
            if e.retry:  # rate limited or unreachable: the rest stays pending for the next round
                break
            continue
        box.mark_sent(ids, now)
        sent += len(ids)
    return sent
