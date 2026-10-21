"""Rules (spec §5.2, §5.9): the built-in ones ship with the program and are read-only; the user's are
stored in shared.sqlite as versions. A version never changes: saving content that differs from the
latest version makes the next one, saving the same content again changes nothing."""
from datetime import datetime
from functools import cache
from pathlib import Path

import yaml

from wb.config import Paths
from wb.db import connect, tx
from wb.engine.l3.match import compile_errors
from wb.engine.l3.rule import Rule, RuleError, load_rule, rule_hash

BUILTIN = Path(__file__).resolve().parents[1] / "engine" / "l3" / "builtin"
TEMPLATES = BUILTIN / "templates"   # 内置模板库 (spec §6.6), read-only like the built-in screens


_PROBLEMS: dict[str, str | None] = {}


def problems(rule: Rule) -> str | None:
    """Why a saved rule cannot run (compile_errors, one per line), or None — cached by its content: the catalog
    lists every rule each time it is read."""
    h = rule_hash(rule)
    if h not in _PROBLEMS:
        _PROBLEMS[h] = "\n".join(compile_errors(rule)) or None
    return _PROBLEMS[h]


class RuleCannotRun(RuleError):
    """A saved rule that does not compile, asked to be enabled."""


class RuleExists(RuleError):
    """A new rule whose id is already taken."""


@cache
def templates() -> list[dict]:
    """The template cards: id, group (box 箱体 / general 通用), shape, basis, describe (a person's own words for
    it, also the AI's few-shot example, §6.1) and the slide examples."""
    return yaml.safe_load((TEMPLATES / "index.yaml").read_text(encoding="utf-8"))


def group_of(rule_id: str) -> str:
    """builtin (the built-in screens and the spec's example), box or general (templates)."""
    return next((c["group"] for c in templates() if c["id"] == rule_id), "builtin")


@cache
def builtin_rules() -> dict[str, tuple[Rule, str]]:
    out = {}
    for f in [*sorted(BUILTIN.glob("*.yaml")), *sorted(TEMPLATES.glob("tpl_*.yaml"))]:
        text = f.read_text(encoding="utf-8")
        rule = load_rule(text)
        out[rule.id] = (rule, text)
    return out


class Rules:
    def __init__(self, paths: Paths):
        self.conn = connect(paths.shared_db, "shared")

    def catalog(self) -> list[dict]:
        """Every rule with its latest version, its state, when it last changed (None for a built-in) and how
        many of its hits you marked right and wrong."""
        state = {r["rule_id"]: r for r in self.conn.execute("SELECT * FROM rule_state")}
        verdicts: dict[str, dict] = {}
        for r in self.conn.execute("SELECT rule_id, verdict, COUNT(*) AS n FROM hit_label GROUP BY rule_id, verdict"):
            verdicts.setdefault(r["rule_id"], {"right": 0, "wrong": 0})[r["verdict"]] = r["n"]

        def extra(rule_id: str, updated_at: str | None) -> dict:
            s = state.get(rule_id)
            return {"enabled": bool(s and s["enabled"]), "archived": bool(s and s["archived"]), "updated_at": updated_at,
                    "verdicts": verdicts.get(rule_id, {"right": 0, "wrong": 0})}

        out = [{"id": r.id, "name": r.name, "version": r.version, "builtin": True, "group": group_of(r.id), "markets": list(r.markets),
                "doc": r.doc, "error": None, "problem": None, **extra(r.id, None)} for r, _ in builtin_rules().values()]
        for row in self.conn.execute(
                "SELECT rule_id, MAX(version) AS version, MAX(created_at) AS updated_at FROM rule_version "
                "GROUP BY rule_id ORDER BY rule_id"):
            try:
                rule, _, version = self.get(row["rule_id"], row["version"])
            except RuleError as e:  # e.g. saved before a setting was renamed: listed with the reason, not fatal
                out.append({"id": row["rule_id"], "name": "（无法读取）", "version": row["version"], "builtin": False, "group": "mine",
                            "markets": [], "doc": "", "error": str(e), "problem": None, **extra(row["rule_id"], row["updated_at"])})
                continue
            out.append({"id": rule.id, "name": rule.name, "version": version, "builtin": False, "group": "mine",
                        "markets": list(rule.markets), "doc": rule.doc, "error": None, "problem": problems(rule),
                        **extra(rule.id, row["updated_at"])})
        return out

    def exists(self, rule_id: str) -> bool:
        return rule_id in builtin_rules() or self.conn.execute(
            "SELECT 1 FROM rule_version WHERE rule_id=? LIMIT 1", (rule_id,)).fetchone() is not None

    def set_state(self, rule_id: str, enabled: bool | None = None, archived: bool | None = None) -> None:
        """Enable a rule for the daily scan, or put it away; None leaves a setting as it is. (What the 开盘前简报 lists
        is its own: workspace/briefing.py.)"""
        if not self.exists(rule_id):
            raise RuleError(f"没有规则 {rule_id}")
        if enabled and (errors := problems(self.get(rule_id)[0])):   # saved before formulas were compiled on save
            raise RuleCannotRun(f"这条规则跑不了，改好再启用：{errors}")
        with tx(self.conn):
            old = self.conn.execute("SELECT enabled, archived FROM rule_state WHERE rule_id=?", (rule_id,)).fetchone()
            e = int(enabled) if enabled is not None else (old["enabled"] if old else 0)
            a = int(archived) if archived is not None else (old["archived"] if old else 0)
            self.conn.execute(
                "INSERT INTO rule_state (rule_id, enabled, archived, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (rule_id) DO UPDATE SET enabled=excluded.enabled, archived=excluded.archived, "
                "updated_at=excluded.updated_at", (rule_id, e, a, datetime.now().isoformat(timespec="seconds")))

    def get(self, rule_id: str, version: int | None = None) -> tuple[Rule, str, int]:
        """(rule, its text, version); the latest version when none is given."""
        if rule_id in builtin_rules():
            rule, text = builtin_rules()[rule_id]
            if version not in (None, rule.version):
                raise RuleError(f"内置规则 {rule_id} 只有版本 {rule.version}")
            return rule, text, rule.version
        q = "SELECT version, body FROM rule_version WHERE rule_id=?"
        row = (self.conn.execute(q + " AND version=?", (rule_id, version)).fetchone() if version is not None
               else self.conn.execute(q + " ORDER BY version DESC LIMIT 1", (rule_id,)).fetchone())
        if row is None:
            raise RuleError(f"没有规则 {rule_id}" + (f" 的版本 {version}" if version is not None else ""))
        return load_rule(row["body"]).model_copy(update={"version": row["version"]}), row["body"], row["version"]

    def raw(self, rule_id: str, version: int | None = None) -> tuple[str, int] | None:
        """A stored version's text as written, whether or not it still loads (None when there is none)."""
        q = "SELECT version, body FROM rule_version WHERE rule_id=?"
        row = (self.conn.execute(q + " AND version=?", (rule_id, version)).fetchone() if version is not None
               else self.conn.execute(q + " ORDER BY version DESC LIMIT 1", (rule_id,)).fetchone())
        return None if row is None else (row["body"], row["version"])

    def versions(self, rule_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT version, hash, created_at FROM rule_version WHERE rule_id=? ORDER BY version",
                                 (rule_id,))
        return [dict(r) for r in rows]

    def save(self, text: str, create: bool = False) -> tuple[str, int, bool]:
        """Check and store a rule. -> (id, version, whether a new version was made). `create`: this is a new
        rule, so an id that is already taken is refused rather than given a new version."""
        rule = load_rule(text)
        if rule.id in builtin_rules():
            raise RuleError(f"{rule.id} 是内置规则，不能改；复制后换一个 id 再保存")
        if errors := compile_errors(rule):   # a rule saved is a rule that runs (检查 says the same before)
            raise RuleError("\n".join(errors))
        if create and self.exists(rule.id):
            raise RuleExists(f"已经有规则 {rule.id}：新规则换一个 id，改这条规则请在它自己的页面里保存")
        h = rule_hash(rule)  # the `version:` written in the text is ignored: the store numbers versions
        with tx(self.conn):
            last = self.conn.execute("SELECT version, hash FROM rule_version WHERE rule_id=? ORDER BY version DESC LIMIT 1",
                                     (rule.id,)).fetchone()
            if last is not None and last["hash"] == h:
                return rule.id, last["version"], False
            version = (last["version"] if last else 0) + 1
            self.conn.execute("INSERT INTO rule_version VALUES (?, ?, ?, ?, ?)",
                              (rule.id, version, h, text, datetime.now().isoformat(timespec="seconds")))
        return rule.id, version, True
