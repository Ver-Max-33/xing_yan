"""The 选股 page (spec §7.1 结果分组、结果列): a rule's latest whole-market scan in a market — how far the listings
got, stage by stage (the funnel), and every listing that got anywhere, with its quote and the last 60 closes."""
import json
import re
from datetime import date, timedelta
from functools import lru_cache

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.db import connect
from wb.engine.l1.compiler import CompileError, Compiler
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l1.parser import ParseError
from wb.engine.l1.run import evaluate
from wb.engine.l3.rule import Rule, RuleError
from wb.engine.l3.run import run_rows
from wb.engine.l3.understand import as_text, names, understand
from wb.screening.scan import previous_run

SPARK = 60
RECENT = 5   # 近 N 日信号: signals in the rule's last N daily whole-market runs in the market
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
STATUS = ("signal", "watch", "unknown")


def latest_run(paths: Paths, rule_id: str, market: str) -> dict | None:
    return previous_run(paths, rule_id, market, date.max)


def recent_signals(paths: Paths, rule_id: str, market: str, before: date, days: int = RECENT - 1) -> dict[str, str]:
    """listing -> its latest signal day among the rule's last `days` daily runs before `before` (the latest run of
    each day: a correction's rescan replaces that day's earlier one)."""
    rows = connect(paths.app_db, "app").execute(
        "SELECT id, day FROM rule_run WHERE rule_id=? AND market=? AND mode='date' AND pool='null' AND day < ? "
        "ORDER BY day DESC, created_at DESC", (rule_id, market, before.isoformat())).fetchall()
    seen, out = set(), {}
    for r in rows:
        if r["day"] in seen:
            continue
        if len(seen) >= days:
            break
        seen.add(r["day"])
        try:
            signals = run_rows(paths, r["id"]).filter(pl.col("status") == "signal")["listing_id"]
        except (RuleError, FileNotFoundError):  # a run whose file is gone: that day is just not counted
            continue
        for lid in signals:
            out.setdefault(lid, r["day"])
    return out


def stage_labels(rule: Rule) -> list[dict]:
    """Each stage as the rule's reading heads it: a short label for the funnel and the whole heading."""
    lines = next((s["lines"] for s in as_text(understand(rule))["sections"] if s["title"] == "阶段"), [])
    heads = [x for x in lines if x[:1] in CIRCLED]
    named = names(rule)["stages"]   # the stage's name, as everywhere (its label, or what it is: 中箱, 金品, 信号)
    return [{"id": s.id, "label": named[s.id], "head": heads[i] if i < len(heads) else s.id} for i, s in enumerate(rule.stages)]


def _quotes(store: Store, ids: list[str], day: date) -> dict[str, dict]:
    snap = store.latest()
    bars = (store.scan("bars_1d", snap)
            .filter(pl.col("listing_id").is_in(ids) & pl.col("date").is_between(day - timedelta(days=130), day)
                    & pl.col("close").is_not_null())
            .sort("date").group_by("listing_id", maintain_order=True).tail(SPARK + 1).collect())
    out = {}
    for (lid,), g in bars.group_by("listing_id"):
        close, adj = g["close"].to_list(), g["adj_factor"].fill_null(1.0).to_list()
        pre = g["pre_close"][-1]
        base = pre if pre is not None else (close[-2] if len(close) > 1 else None)
        out[lid] = {"close": close[-1], "change": close[-1] / base - 1 if base else None, "amount": g["amount"][-1],
                    "spark": [c * a / adj[-1] for c, a in zip(close, adj, strict=True)][-SPARK:]}   # 前复权 to the last close
    return out


class Unreadable(ValueError):
    """附加条件 a market cannot read — not finished, no such function, not a condition, a column its data lacks — with the
    reason in the words of the formula checker."""


class NotYet(Unreadable):
    """附加条件 whose data has not come yet: a dataset the market's latest snapshot lacks (it comes with an update)."""


# what a condition's data is called when it has not come yet
DATA_NAMES = {"float_holders": "前十大流通股东", "moneyflow": "资金流向", "valuation": "估值", "financials": "财务",
              "industry": "行业", "status_intervals": "ST 记录", "index_bars_1d": "指数日线"}


def for_market(formula: str, market: str) -> str | None:
    """附加条件 written for each market (A 股流通市值, 日股总市值: J-Quants gives no free float) as `CN: …; JP: …`: the
    market's own part, or None when it names other markets only (not cut there); a formula naming no market is every
    market's. Raises Unreadable for a part without its market, or a market named twice."""
    parts = [p.strip() for p in re.split(r"[;；]", formula) if p.strip()]
    named = [re.fullmatch(r"(CN|JP)\s*[:：]\s*(.+)", p, re.S) for p in parts]
    if not any(named):
        return formula
    if not all(named):
        raise Unreadable("按市场分开写时，每一段都以市场开头，例如 CN: CIRC_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000")
    mine = [m[2].strip() for m in named if m[1] == market]
    if len(mine) > 1:
        raise Unreadable(f"{market} 写了不止一次")
    return mine[0] if mine else None


# the 选股 page's ready 附加条件 by their words (frontend lib/jobs.ts WHERE_PRESETS); any other is named by its formula
WHERE_NAMES = {"MA(BAR_LEN, 20) <= 0.03": "近期 K 线短小", "CN: CIRC_MV / 1e8 <= 100; JP: TOTAL_MV / 1e8 <= 1000": "小盘股",
               "CN: FOREIGN_HOLDERS >= 2": "多个外资持股"}


def _compiled(store: Store, market: str, formula: str):
    # a market with no data yet cannot say which columns its data has: the formula is read, the columns are not judged
    snap = store.latest()
    comp = Compiler(market, available=available_columns(store) if snap is not None else None)
    try:
        comp.add("hit", formula, want="cond")
    except (CompileError, ParseError) as e:
        raise _not_yet(store, market, formula, snap) or Unreadable(str(e)) from None
    return comp.program()


def _not_yet(store: Store, market: str, formula: str, snap) -> NotYet | None:
    """A formula that reads right but needs a dataset the snapshot lacks and this market's sources give (an update brings
    it): said as data not come yet. A dataset no source of the market gives stays 在当前数据档里没有."""
    from wb.data.capability import load_caps, route
    from wb.db import connect
    from wb.engine.l1.fields import FIELDS
    from wb.engine.l1.frame import OPTIONAL
    from wb.notify.notices import MARKET_NAME
    comp = Compiler(market)   # the formula alone, every column taken as there
    try:
        comp.add("hit", formula, want="cond")
    except (CompileError, ParseError):
        return None
    caps, name = load_caps(connect(store.paths.app_db, "app"), market), {f.column: f.name for f in FIELDS.values()}
    lacking: dict[str, list[str]] = {}
    for col in sorted(comp.program().columns):
        ds = {"chips": "valuation"}.get(OPTIONAL.get(col), OPTIONAL.get(col))   # 筹码 is worked out of the turnover
        if ds is None or (snap is not None and snap.manifest.get(ds)):
            continue
        if route(store.paths.profile, market, ds, caps) is None:
            return None
        lacking.setdefault(ds, []).append(name.get(col, col))
    if not lacking:
        return None
    return NotYet(f"{MARKET_NAME[market]}还没有取到{'、'.join(DATA_NAMES.get(ds, ds) for ds in lacking)}的数据"
                  f"（{'、'.join(n for ns in lacking.values() for n in ns)} 要用），下一次更新数据时会取（数据页可以点「立即更新」）")


def check(paths: Paths, market: str, formula: str) -> None:
    """Raises Unreadable when `market` cannot read the 附加条件 (its own part; one written for the other market only is
    not its) — compiled, not run; a market with no data yet is held to the formula only."""
    own = for_market(formula, market)
    if own is not None:
        _compiled(Store(paths, market), market, own)


@lru_cache(maxsize=16)   # ponytail: whole-market reads (~5 s, a few hundred MB on A shares); a new snapshot is a new key
def _holds(paths: Paths, market: str, snapshot: str, day: date, formula: str) -> tuple[frozenset[str], int]:
    store = Store(paths, market)
    prog = _compiled(store, market, formula)
    calendar = store.read("calendar").filter(pl.col("is_open"))["date"].to_list()
    # every bar up to the day: a recursive indicator (EMA, MACD) reads them all, as in a rule
    out = evaluate(build_frame(store, market, paths.profile, prog.columns, end=day), prog, calendar).filter(pl.col("date") == day)
    return frozenset(out.filter(pl.col("hit"))["listing_id"].to_list()), int(out["hit"].null_count())


def holds(paths: Paths, market: str, formula: str, day: date) -> tuple[set[str], int]:
    """附加条件 (spec §7.1): the listings `formula` — a condition over the bars, written as in a rule (例如
    MA(BAR_LEN, 20) <= 0.03, 近期 K 线短小) — holds for on `day` in the market's latest snapshot, and how many it could
    not be read for (too few bars: left out). Raises Unreadable."""
    snap = Store(paths, market).latest()
    hit, unknown = _holds(paths, market, snap.id if snap else "", day, formula)
    return set(hit), unknown


def found_by(paths: Paths, run: dict, statuses: tuple[str, ...]) -> set[str]:
    """The listings a run found in `statuses` (命中: signal; 命中或潜伏: signal and watch) — all of them, not the first rows."""
    rows = run_rows(paths, run["id"])
    return set(rows.filter(pl.col("status").is_in(list(statuses)))["listing_id"].to_list())


def screen(paths: Paths, market: str, run: dict, stages: int, limit: int = 1000,
           only: set[str] | None = None) -> tuple[list[int] | None, list[dict], int]:
    """(the funnel: all, then how many reached each of the `stages`, None for a run recorded before depths were;
    the listings that reached any stage, best first, at most `limit`; how many there are in all). `only`: the listings
    another rule found too (同时满足另一条规则) — the funnel and the rows are theirs."""
    rows = run_rows(paths, run["id"])
    if only is not None:
        rows = rows.filter(pl.col("listing_id").is_in(list(only)))
    has_depth = "depth" in rows.columns
    funnel = [rows.height] + [int((rows["depth"] >= i).sum()) for i in range(stages)] if has_depth else None
    recent = recent_signals(paths, run["rule_id"], market, date.fromisoformat(run["day"]))
    keep = (pl.col("status").is_in(STATUS) | (pl.col("depth") >= 0 if has_depth else pl.lit(False))
            | pl.col("listing_id").is_in(list(recent)))
    kept = rows.filter(keep)
    shown = kept.head(limit)
    store = Store(paths, market)
    from wb.market.industry import at   # one scheme: 申万 for A shares, 33 業種 for 日股 (redesign §6.3)
    industry = {r[0]: (r[1], r[2]) for r in at(store, store.latest(), market, date.fromisoformat(run["day"])).iter_rows()}
    tags: dict[str, list[str]] = {}
    for lid, tag in connect(paths.shared_db, "shared").execute("SELECT listing_id, tag FROM listing_tag ORDER BY tag"):
        tags.setdefault(lid, []).append(tag)
    quotes = _quotes(store, shown["listing_id"].to_list(), date.fromisoformat(run["day"])) if shown.height else {}
    out = []
    for r in shown.iter_rows(named=True):
        l1, l2 = industry.get(r["listing_id"], (None, None))
        out.append({"listing_id": r["listing_id"], "market": market, "name": r.get("name"), "industry": l1, "industry2": l2,
                    "tags": tags.get(r["listing_id"], []),
                    "status": r["status"], "depth": r.get("depth"), "signal_date": r.get("signal_date"), "repeat": r.get("repeat"),
                    "recent_signal": recent.get(r["listing_id"]),
                    "detail": json.loads(r["detail"]) if isinstance(r.get("detail"), str) else None,
                    **{k: v for k, v in r.items() if k.startswith("rank")}, **quotes.get(r["listing_id"], {})})
    return funnel, out, kept.height
