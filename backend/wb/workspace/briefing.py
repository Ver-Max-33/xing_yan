"""开盘前简报's items (spec §7.1, 2026-10-10): the 选股 views you put into the briefing — a rule, its 附加条件 (formulas
all held on the scan's day; `CN: …; JP: …` gives each market its own) and maybe 同时满足另一条规则 (its 命中, or 命中或
潜伏) — each listed (its stocks, the first few) or counted, in your order. Nothing until you put one in. Kept in
shared.sqlite, as the rules are."""
import json
from collections.abc import Callable
from datetime import datetime

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect, tx
from wb.engine.l3.rule import RuleError
from wb.markets import MARKETS
from wb.screening.screen import Unreadable, check
from wb.workspace.rules import Rules

MODES = {"list": "列出股票", "count": "只说数量"}
ALSO = {"signal": ("signal",), "watch": ("signal", "watch")}   # 同时满足另一条规则: its 命中 / 命中或潜伏


def tidy(wheres) -> list[str]:
    """附加条件 as the 选股 page keeps them: trimmed, once each, in their order."""
    return list(dict.fromkeys(w.strip() for w in wheres or [] if w and w.strip()))


class BriefItems:
    def __init__(self, paths_of: Callable[[str], Paths]):
        self.paths_of = paths_of
        self.conn = connect(paths_of(MARKETS[0]).shared_db, "shared")

    def list(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM brief_item ORDER BY sort, id")
        return [{"id": r["id"], "rule_id": r["rule_id"], "wheres": json.loads(r["wheres"]), "also": r["also_rule"],
                 "also_status": r["also_status"], "mode": r["mode"]} for r in rows]

    def add(self, rule_id: str, wheres=(), also: str | None = None, also_status: str = "signal", mode: str = "list",
            now: datetime | None = None) -> int:
        """Put a 选股 view into the briefing, last. Refused (ValueError, the reason in words) when the briefing could not
        use it: no such rule, one not enabled (not scanned every day), a 附加条件 none of the rule's markets with data can
        read (as the 选股 page says it: one that cannot is told so in its own entry of the briefing; a market without
        data yet is held to the formula alone and counts only when no market has data), the same view already in."""
        rules = Rules(self.paths_of(MARKETS[0]))
        if mode not in MODES:
            raise ValueError(f"简报里只能{'或'.join(MODES.values())}")
        try:
            rule = rules.get(rule_id)[0]
            other = rules.get(also)[0] if also else None
        except RuleError as e:
            raise ValueError(str(e)) from None
        if other is not None and also_status not in ALSO:
            raise ValueError("同时满足另一条规则只能是命中（signal）或命中或潜伏（watch）")
        on = {r["id"] for r in rules.catalog() if r["enabled"]}
        for r in (rule, other):
            if r is not None and r.id not in on:
                raise ValueError(f"「{r.name}」没有启用，不在每日扫描里：先在规则库里启用它")
        wheres = tidy(wheres)
        for w in wheres:
            said: dict[str, str | None] = {}
            for m in rule.markets:
                try:
                    check(self.paths_of(m), m, w)
                    said[m] = None
                except Unreadable as e:
                    said[m] = str(e)
            judged = [m for m in rule.markets if Store(self.paths_of(m), m).latest() is not None] or list(rule.markets)
            if all(said[m] for m in judged):
                why = [f"{m}：{said[m]}" if len(rule.markets) > 1 else said[m] for m in judged]
                raise ValueError(f"附加条件读不了（{w}）：{'；'.join(why)}")
        key = (rule.id, sorted(wheres), other.id if other else None, also_status if other else None)
        if any((i["rule_id"], sorted(i["wheres"]), i["also"], i["also_status"]) == key for i in self.list()):
            raise ValueError("这个选股视图已经在简报里")
        with tx(self.conn):
            sort = self.conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM brief_item").fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO brief_item (rule_id, wheres, also_rule, also_status, mode, sort, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule.id, json.dumps(wheres, ensure_ascii=False), other.id if other else None, also_status if other else None,
                 mode, sort, (now or datetime.now()).isoformat(timespec="seconds")))
        return int(cur.lastrowid)

    def _one(self, item_id: int) -> None:
        if not self.conn.execute("SELECT 1 FROM brief_item WHERE id=?", (item_id,)).fetchone():
            raise KeyError(f"简报里没有第 {item_id} 项")

    def set_mode(self, item_id: int, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"简报里只能{'或'.join(MODES.values())}")
        self._one(item_id)
        self.conn.execute("UPDATE brief_item SET mode=? WHERE id=?", (mode, item_id))

    def move(self, item_id: int, step: int) -> None:
        """One place up (-1) or down (+1); the first and the last stay where they are."""
        self._one(item_id)
        ids = [i["id"] for i in self.list()]
        k = ids.index(item_id)
        j = max(0, min(len(ids) - 1, k + (1 if step > 0 else -1)))
        ids[k], ids[j] = ids[j], ids[k]
        with tx(self.conn):
            for n, i in enumerate(ids):
                self.conn.execute("UPDATE brief_item SET sort=? WHERE id=?", (n, i))

    def remove(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM brief_item WHERE id=?", (item_id,))
