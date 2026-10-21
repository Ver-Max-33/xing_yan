"""指标库 (design 2026-10-10): indicators as data — a name, where it is drawn, its parameters, reference levels and a
通达信-style formula (wb.engine.l1.script). The built-in ones ship with the program (builtin/*.yaml, read-only); yours
are kept in shared.sqlite as versions, as rules are (a version never changes; saving the same text makes none)."""
import hashlib
import json
import re
import time
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from wb.config import Paths
from wb.db import connect, tx
from wb.engine.l1.script import NAME, compile_script
from wb.engine.l3.rule import _zh, load_text

BUILTIN = Path(__file__).with_name("builtin")
GROUPS = {"common": "常用指标", "setup": "组合设置", "ict": "ICT 概念", "mine": "我的指标"}
Market = Literal["CN", "JP"]


class IndicatorError(ValueError):
    pass


class IndicatorExists(IndicatorError):
    """A new indicator whose id is already taken."""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IndParam(_M):
    id: str
    value: float
    range: tuple[float, float] | None = None
    step: float | None = None

    @field_validator("id")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.fullmatch(NAME, v):
            raise ValueError("参数名用字母、数字、下划线或中文，不以数字开头")
        return v


class Indicator(_M):
    id: str = ""                                   # yours: given when first saved
    name: str = Field(min_length=1, max_length=40)
    doc: str = ""
    group: Literal["common", "setup", "ict", "mine"] = "mine"
    pane: Literal["auto", "main", "sub"] = "auto"  # auto: over the candles when every drawn thing is a price
    params: list[IndParam] = []
    levels: list[float] = []                       # reference lines of a sub pane (RSI's 20 / 50 / 80)
    markets: list[Market] = ["CN", "JP"]
    source: str = ""                               # where it is rewritten from
    formula: str = Field(min_length=1)
    version: int = 1

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if v and not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", v):
            raise ValueError("id 用小写字母开头，只含小写字母、数字、下划线")
        return v

    def param_values(self, override: dict | None = None) -> dict[str, float]:
        return {p.id: (override or {}).get(p.id, p.value) for p in self.params}


def load_indicator(text: str, check: bool = True) -> Indicator:
    """YAML (or JSON) text -> a checked Indicator; `check`: its formula must compile in each of its markets (what is
    wrong, line by line). Raises IndicatorError with every problem."""
    try:
        data = load_text(text)
    except yaml.YAMLError as e:
        raise IndicatorError(f"不是合法的 YAML / JSON：{e}") from None
    if not isinstance(data, dict):
        raise IndicatorError("指标要写成 key: value 的形式")
    try:
        ind = Indicator.model_validate(data)
    except ValidationError as e:
        raise IndicatorError(_zh(e)) from None
    if len({p.id.upper() for p in ind.params}) != len(ind.params):
        raise IndicatorError("params：参数名重复（大小写不分）")
    if not ind.markets:
        raise IndicatorError("markets：至少一个市场")
    if check and (problems := formula_problems(ind)):
        raise IndicatorError("\n".join(problems))
    return ind


def formula_problems(ind: Indicator) -> list[str]:
    """Why the formula cannot be drawn, in words — a problem only some of its markets have (their own fields) names
    them; empty when it can be drawn."""
    seen: dict[str, list[str]] = {}
    for m in ind.markets:
        c = compile_script(ind.formula, m, ind.param_values())
        lines = [f"第 {e['line']} 行：{e['message']}" if e["line"] else e["message"] for e in c.errors]
        lines += [f"{why}：{'、'.join(names)}" for why, names in c.unsupported.items()]
        for x in lines:
            seen.setdefault(x, []).append(m)
    names = {"CN": "A 股", "JP": "日股"}
    return [x if len(ms) == len(ind.markets) else f"{'、'.join(names[m] for m in ms)}：{x}" for x, ms in seen.items()]


def indicator_hash(ind: Indicator) -> str:
    body = json.dumps(ind.model_dump(mode="json", exclude={"version"}), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@cache
def builtin_indicators() -> dict[str, tuple[Indicator, str]]:
    out = {}
    for f in sorted(BUILTIN.glob("*.yaml")):
        text = f.read_text(encoding="utf-8")
        ind = load_indicator(text, check=False)   # checked by the tests, not on every start
        out[ind.id] = (ind, text)
    return out


def dump(ind: Indicator) -> str:
    """An indicator as the YAML it is kept in (the editor's text): the formula a literal block."""
    data = ind.model_dump(mode="json", exclude={"version"}, exclude_defaults=True)
    data["name"], data["formula"] = ind.name, ind.formula

    class _Dumper(yaml.SafeDumper):
        pass

    def _str(d, s):
        return d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)
    _Dumper.add_representer(str, _str)
    order = ["id", "name", "doc", "group", "pane", "params", "levels", "markets", "source", "formula"]
    return yaml.dump({k: data[k] for k in order if k in data}, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=120)


class Indicators:
    def __init__(self, paths: Paths):
        self.conn = connect(paths.shared_db, "shared")

    def catalog(self) -> list[dict]:
        """Every indicator — the built-in ones, then yours — with its latest version and whether it is put away."""
        state = {r["indicator_id"]: r for r in self.conn.execute("SELECT * FROM indicator_state")}

        def row(ind: Indicator, builtin: bool, version: int, updated_at: str | None, error: str | None = None) -> dict:
            s = state.get(ind.id)
            return {"id": ind.id, "name": ind.name, "doc": ind.doc, "group": ind.group, "pane": ind.pane,
                    "markets": list(ind.markets), "builtin": builtin, "version": version, "updated_at": updated_at,
                    "archived": bool(s and s["archived"]), "params": [p.model_dump() for p in ind.params],
                    "levels": list(ind.levels), "source": ind.source, "error": error}
        out = [row(ind, True, ind.version, None) for ind, _ in builtin_indicators().values()]
        for r in self.conn.execute("SELECT indicator_id, MAX(version) AS version, MAX(created_at) AS updated_at "
                                   "FROM indicator_version GROUP BY indicator_id ORDER BY MIN(created_at)"):
            try:
                ind, _, version = self.get(r["indicator_id"], r["version"])
                out.append(row(ind, False, version, r["updated_at"]))
            except IndicatorError as e:   # saved before a check got stricter: listed with why, not fatal
                out.append({"id": r["indicator_id"], "name": "（无法读取）", "doc": "", "group": "mine", "pane": "auto",
                            "markets": [], "builtin": False, "version": r["version"], "updated_at": r["updated_at"],
                            "archived": bool(state.get(r["indicator_id"]) and state[r["indicator_id"]]["archived"]),
                            "params": [], "levels": [], "source": "", "error": str(e)})
        return out

    def exists(self, iid: str) -> bool:
        return iid in builtin_indicators() or self.conn.execute(
            "SELECT 1 FROM indicator_version WHERE indicator_id=? LIMIT 1", (iid,)).fetchone() is not None

    def get(self, iid: str, version: int | None = None) -> tuple[Indicator, str, int]:
        """(indicator, its text, version); the latest when none is given."""
        if iid in builtin_indicators():
            ind, text = builtin_indicators()[iid]
            return ind, text, ind.version
        q = "SELECT version, body FROM indicator_version WHERE indicator_id=?"
        r = (self.conn.execute(q + " AND version=?", (iid, version)).fetchone() if version is not None
             else self.conn.execute(q + " ORDER BY version DESC LIMIT 1", (iid,)).fetchone())
        if r is None:
            raise IndicatorError(f"没有指标 {iid}" + (f" 的版本 {version}" if version is not None else ""))
        ind = load_indicator(r["body"], check=False).model_copy(update={"version": r["version"], "id": iid})
        return ind, r["body"], r["version"]

    def save(self, text: str, create: bool = False) -> tuple[str, int, bool]:
        """Check and keep an indicator -> (id, version, whether a new version was made). A new one without an id is
        given one; `create` refuses an id already taken (a copy keeps its own)."""
        ind = load_indicator(text)
        if ind.id in builtin_indicators():
            raise IndicatorError(f"{ind.id} 是内置指标，不能改；复制一份再改")
        if ind.group != "mine":
            ind = ind.model_copy(update={"group": "mine"})
        if not ind.id:
            ind = ind.model_copy(update={"id": f"u{int(time.time() * 1000):x}"})
        elif create and self.exists(ind.id):
            raise IndicatorExists(f"已经有指标 {ind.id}")
        body, h = dump(ind), indicator_hash(ind)
        with tx(self.conn):
            last = self.conn.execute("SELECT version, hash FROM indicator_version WHERE indicator_id=? ORDER BY version DESC LIMIT 1",
                                     (ind.id,)).fetchone()
            if last is not None and last["hash"] == h:
                return ind.id, last["version"], False
            version = (last["version"] if last else 0) + 1
            self.conn.execute("INSERT INTO indicator_version VALUES (?, ?, ?, ?, ?)",
                              (ind.id, version, h, body, datetime.now().isoformat(timespec="seconds")))
        return ind.id, version, True

    def set_state(self, iid: str, archived: bool) -> None:
        if not self.exists(iid):
            raise IndicatorError(f"没有指标 {iid}")
        with tx(self.conn):
            self.conn.execute("INSERT INTO indicator_state (indicator_id, archived, updated_at) VALUES (?, ?, ?) "
                              "ON CONFLICT (indicator_id) DO UPDATE SET archived=excluded.archived, updated_at=excluded.updated_at",
                              (iid, int(archived), datetime.now().isoformat(timespec="seconds")))
