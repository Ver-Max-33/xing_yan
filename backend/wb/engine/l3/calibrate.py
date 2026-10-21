"""网格校准 (spec §6.5): a grid over the parameters marked `tune: true` (at most 3, each over its range by its
step, at most 500 combinations), scored on what you marked — the hits you said were right should stay, the
ones you said were wrong should go, and, when a tuned parameter sets a structure, the boxes should agree
with your hand-drawn samples (§5.10). Marks fall into a tune and a test group once and for all (7:3, by the
mark itself); the grid is judged on the tune group, every candidate reports both groups and its hit count
on the sample. The result is the Pareto front — never applied by itself — and, for each sample the current
settings disagree with, the nearest setting under which it agrees (which parameter explains it)."""
import hashlib
import itertools
import json
import math
import re
from bisect import bisect_left
from datetime import date, timedelta
from typing import Callable

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import available_columns
from wb.engine.l1.run import evaluate
from wb.engine.l2.agreement import agreement
from wb.engine.l2.run import compute_market, l2_bars
from wb.engine.l3.match import Matcher
from wb.engine.l3.preview import _latest, gallery, sample
from wb.engine.l3.rule import Rule, RuleError
from wb.engine.l3.run import frame_for, market_turn, one_market_frame, primitives
from wb.engine.l3.understand import MARKET

MAX_PARAMS, MAX_COMBOS = 3, 500
NEAR = 3          # bars: a mark is found when the rule signals within 3 bars of it (a run's first day can move)
FEW = 10          # fewer marks than this: the result is likely to fit them and nothing else
FRONT = 10        # candidates listed
BOX_COLS = ["box_id", "listing_id", "scale", "status", "start", "known_at", "end", "ended_at", "top", "bottom"]


def axes(rule: Rule) -> dict[str, list]:
    """Each tuned parameter's grid values: its range by its step (a switch: off and on)."""
    tune = [p for p in rule.params if p.tune]
    if not tune:
        raise RuleError("没有要校准的参数：给参数写上 range、step 和 tune: true")
    if len(tune) > MAX_PARAMS:
        raise RuleError(f"最多 3 个参数一起校准，这条规则标了 {len(tune)} 个（{'、'.join(p.id for p in tune)}）：把其余的 tune 改成 false")
    out: dict[str, list] = {}
    for p in tune:
        if isinstance(p.value, bool):
            out[p.id] = [False, True]
            continue
        if not isinstance(p.value, (int, float)) or not p.range or not p.step:
            raise RuleError(f"参数 {p.id} 要写数字的 range 和 step 才能校准")
        lo, hi, step = float(p.range[0]), float(p.range[1]), float(p.step)
        n = int(math.floor((hi - lo) / step + 1e-9)) + 1
        vals = [round(lo + k * step, 10) for k in range(n)]
        out[p.id] = [int(round(v)) for v in vals] if rule.whole(p.id) else vals
    total = math.prod(len(v) for v in out.values())
    if total > MAX_COMBOS:
        widest = max(out, key=lambda k: len(out[k]))
        raise RuleError(f"{total} 种组合，超过 500：把参数 {widest} 的步长调大（它现在有 {len(out[widest])} 个取值）")
    return out


def group(listing_id: str, day) -> str:
    """A mark's group, fixed by the mark itself (7:3)."""
    return "tune" if int(hashlib.sha1(f"{listing_id}|{day}".encode()).hexdigest(), 16) % 10 < 7 else "test"


def _in_structure(rule: Rule, pid: str) -> bool:
    """Whether parameter `pid` sets a structure (`with:` of a structure stage) — by its whole name ($tol is not $tol_box)."""
    pat = re.compile(re.escape(f"${pid}") + r"(?!\w)")
    return any(s.kind == "structure" and pat.search(json.dumps(s.with_, ensure_ascii=False)) for s in rule.stages)


def _found(marks: list[dict], idx: dict, signals: pl.DataFrame) -> list[bool]:
    """Whether the rule signals within NEAR bars of each mark."""
    by: dict[str, list[int]] = {}
    for lid, i in signals.select("listing_id", "i").iter_rows():
        by.setdefault(lid, []).append(i)
    for v in by.values():
        v.sort()
    out = []
    for m in marks:
        i0 = idx.get((m["listing_id"], m["signal_date"]))
        xs = by.get(m["listing_id"], [])
        k = bisect_left(xs, i0 - NEAR) if i0 is not None else len(xs)
        out.append(k < len(xs) and xs[k] <= i0 + NEAR)
    return out


def _with_market_columns(part: pl.DataFrame, whole: pl.DataFrame, prog, calendar: list[date], pool: list[str]) -> pl.DataFrame:
    """`part` (the marked listings' rows) with the cross-sectional columns `prog` reads, computed on the `whole` market
    — again only when a setting changes one of them (a box setting does not change a rank): columns are named by
    their formula, and the Matcher does not compute a column its frame has."""
    if prog.xs_cols <= set(part.columns):
        return part
    full = evaluate(whole, prog, calendar, keep=True).filter(pl.col("listing_id").is_in(pool))
    return part.hstack(full.select(c for c in full.columns if c.startswith("l1_") and c not in part.columns))


def _score_marks(paths: Paths, rule: Rule, market: str, points: list[dict], marks: list[dict], years: int,
                 done: list = (), step: Callable | None = None) -> list[list[bool]]:
    """found[point][mark] for one market's marks: the frame is built once, the rule matched per point. `done`: the
    points scored before (a calibration continued); `step(row)`: told after each next one."""
    out = list(done)
    if len(out) >= len(points):
        return out
    store, snap = _latest(paths, market)
    lids = sorted({m["listing_id"] for m in marks})
    available = available_columns(store, snap)
    calendar = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    names = primitives(rule)
    bars = l2_bars(store, market, paths.profile, lids, snap=snap) if names else None
    tables: dict[str, dict] = {}
    idx: dict | None = None     # (listing, day) -> bar number: the same frame for every point
    with market_turn(Matcher.cross_sectional(rule, market, available)):
        frame, pool = frame_for(store, market, paths.profile, rule, available, lids, snap)
        end = frame["date"].max() if frame.height else date.today()
        # the last `years`, and back to your oldest mark: a mark made on an older gallery still counts
        start = min(end - timedelta(days=365 * years), min(m["signal_date"] for m in marks) - timedelta(days=30))
        if pool is not None:    # the whole market, for a cross-sectional formula: matched on the marked listings' rows
            frame = frame.sort("listing_id", "date")
            part = frame.filter(pl.col("listing_id").is_in(pool))
        for point in points[len(out):]:
            r = rule.with_params(point)
            pp = r.primitive_params()
            key = json.dumps(pp, sort_keys=True, default=str)
            if names and key not in tables:
                tables[key] = compute_market(bars, names, pp)
            if pool is not None:
                part = _with_market_columns(part, frame, Matcher.program(r, market, available), calendar, pool)
            m = Matcher(r, market, part if pool is not None else frame, tables.get(key, {}), calendar, available)
            bars_i = m.frame.select("listing_id", "date", "i")
            if idx is None:
                idx = {(lid, d): i for lid, d, i in bars_i.iter_rows()}
            sig = m.signals(start, end).select("listing_id", pl.col("signal_date").alias("date")).join(
                bars_i, on=["listing_id", "date"], how="left")
            out.append(_found(marks, idx, sig))
            if step:
                step(out[-1])
    return out


def _score_labels(paths: Paths, rule: Rule, market: str, points: list[dict], labels: list[dict],
                  done: list = (), step: Callable | None = None) -> list[dict[str, bool]]:
    """agrees[point][label id] for one market's samples (labels in the computation basis); `done` and `step` as for
    _score_marks."""
    out = list(done)
    if len(out) >= len(points):
        return out
    store, snap = _latest(paths, market)
    lids = sorted({x["listing_id"] for x in labels})
    bars = l2_bars(store, market, paths.profile, lids, snap=snap)
    closes = bars.select("listing_id", "date", "close")
    names = ["BOX"] + (["SLANT_BOX"] if any(x["category"] == "slant" for x in labels) else [])
    seen: dict[str, dict[str, bool]] = {}
    for point in points[len(out):]:
        pp = rule.with_params(point).primitive_params()
        settings = {n: pp[n] for n in names if pp.get(n)}
        key = json.dumps(settings, sort_keys=True, default=str)
        if key not in seen:
            t = compute_market(bars, names, settings or None)
            boxes = pl.concat([t["BOX"]["boxes"].select(BOX_COLS)]
                              + ([t["SLANT_BOX"]["slant"].select(BOX_COLS)] if "SLANT_BOX" in t else []), how="vertical_relaxed")
            seen[key] = {row["id"]: bool(row["consistent"]) for row in agreement(labels, boxes, closes)["labels"]}
        out.append(seen[key])
        if step:
            step(out[-1])
    return out


def _share(xs: list[bool]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _dominates(a: tuple, b: tuple) -> bool:
    return all(x >= y for x, y in zip(a, b, strict=True)) and any(x > y for x, y in zip(a, b, strict=True))


def prepare(paths_of: Callable[[str], Paths], rule: Rule, marks: list[dict], labels: list[dict] | None = None) -> tuple:
    """What a calibration runs on — the grid, the markets with data (a market the rule names without a snapshot yet
    is left out and named in the result), your marks and, when a tuned parameter sets a box, your samples there — or
    a RuleError saying why it cannot run (said before a calibration is queued)."""
    ax = axes(rule)
    markets = [m for m in rule.markets if Store(paths_of(m), m).latest() is not None]
    if not markets:
        raise RuleError(f"{'、'.join(MARKET[m] for m in rule.markets)}还没有数据：先在「数据」页取数据")
    labels = [x for x in labels or [] if not x.get("redraw_of")]
    marks = [m for m in marks if m["verdict"] in ("right", "wrong") and m["listing_id"].split(":")[0] in markets]
    structural = sorted(k for k in ax if _in_structure(rule, k))
    use_labels = [x for x in labels if x["listing_id"].split(":")[0] in markets] if structural else []
    if not marks and not use_labels:
        raise RuleError("还没有可以校准的标注：先在「样例图集」里按 Y / N 标对错"
                        + ("，或在「箱体标注」里画样例" if structural else "（这条规则校准的参数不改箱体，箱体样例用不上）"))
    return ax, markets, marks, structural, use_labels


def calibrate(paths_of: Callable[[str], Paths], rule: Rule, marks: list[dict], labels: list[dict] | None = None,
              years: int = 2, sample_n: int = 300, seed: int = 0, progress: Callable[[str], None] | None = None,
              keep=None) -> dict:
    """`marks`: listing_id, signal_date, verdict (right / wrong) — the gallery's Y/N on this rule; `labels`:
    your box samples in the computation basis (as agreement() takes them). `paths_of(market)` is that
    market's data profile. `progress(text)` is told after each step — a parameter set scored, a candidate's hits
    counted — and may stop the calibration there by raising; `keep` (rows / add, as validate.engine.Keep) holds the
    finished steps, read back by a calibration continued later (on another snapshot: done again)."""
    ax, markets, marks, structural, use_labels = prepare(paths_of, rule, marks, labels)
    names = list(ax)
    skipped = [m for m in rule.markets if m not in markets]

    def steps(part: str, market: str, what: str, unit: str, total: int, about) -> tuple[list, Callable]:
        """A part done step by step: the steps it finished before, and what to call after each next one."""
        snap = Store(paths_of(market), market).latest()
        digest = hashlib.sha1(json.dumps(about, sort_keys=True, default=str).encode()).hexdigest()[:12]
        key = (part, market, snap.id, digest)   # what the steps were computed on: other marks or data, done again
        done = keep.rows(key)[:total] if keep else []
        count = [len(done)]

        def step(row) -> None:
            if keep:
                keep.add(key, row)
            count[0] += 1
            if progress:
                progress(f"{MARKET[market]} · {what}：第 {count[0]} / {total} {unit}")
        if progress and count[0] < total:
            progress(f"{MARKET[market]} · {what}：{f'从第 {count[0] + 1} {unit}接着算' if count[0] else '准备数据'}")
        return done, step
    current = {k: rule.param(k) for k in names}
    points = [dict(zip(names, vals, strict=True)) for vals in itertools.product(*ax.values())]
    combos = len(points)
    if current not in points:
        points.append(current)
    cur = points.index(current)

    found = [[False] * len(marks) for _ in points]
    agrees: list[dict[str, bool]] = [{} for _ in points]
    for market in markets:
        mine = [i for i, m in enumerate(marks) if m["listing_id"].startswith(f"{market}:")]
        if mine:
            ms = [marks[i] for i in mine]
            done, step = steps("marks", market, "你的标注", "组参数", len(points), [points, ms, years])
            for k, row in enumerate(_score_marks(paths_of(market), rule, market, points, ms, years, done, step)):
                for i, ok in zip(mine, row, strict=True):
                    found[k][i] = ok
        lbs = [x for x in use_labels if x["listing_id"].startswith(f"{market}:")]
        if lbs:
            done, step = steps("labels", market, "箱体样例", "组参数", len(points), [points, lbs])
            for k, row in enumerate(_score_labels(paths_of(market), rule, market, points, lbs, done, step)):
                agrees[k].update(row)

    grp = [group(m["listing_id"], m["signal_date"]) for m in marks]

    def scores(k: int, g: str) -> dict:
        right = [found[k][i] for i, m in enumerate(marks) if m["verdict"] == "right" and grp[i] == g]
        wrong = [not found[k][i] for i, m in enumerate(marks) if m["verdict"] == "wrong" and grp[i] == g]
        boxes = [agrees[k].get(x["id"], False) for x in use_labels if x["grp"] == g]
        return {"right": _share(right), "wrong": _share(wrong), "boxes": _share(boxes)}

    def vector(k: int) -> tuple:
        return tuple(v for v in scores(k, "tune").values() if v is not None)

    def distance(p: dict) -> tuple:
        """Fewest parameters changed, then the smallest change (in steps of each range)."""
        changed = [k for k in names if p[k] != current[k]]
        span = {k: (max(ax[k]) - min(ax[k])) or 1 for k in names if not isinstance(current[k], bool)}
        return len(changed), sum(abs(p[k] - current[k]) / span[k] for k in changed if k in span)

    # the Pareto front on the tune group; of points that score the same, the one closest to what you have
    best: dict[tuple, int] = {}
    for k in range(len(points)):
        v = vector(k)
        if v not in best or distance(points[k]) < distance(points[best[v]]):
            best[v] = k
    front = [k for v, k in best.items() if not any(_dominates(w, v) for w in best if w != v)]
    front.sort(key=lambda k: (-sum(vector(k)), distance(points[k])))
    front = front[:FRONT]

    # each candidate's hits on the sample, market by market: a cross-sectional rule's whole market is built once
    counted = dict.fromkeys([cur, *front], 0)
    for m in markets:
        done, step = steps("hits", m, "样本命中", "组", len(counted), [[points[k] for k in counted], sample_n, seed, years])
        for k, n in zip(counted, done, strict=False):
            counted[k] += n
        todo = list(counted)[len(done):]
        if todo:
            pool = sample(paths_of(m), m, sample_n // len(rule.markets), seed)
            with one_market_frame():
                for k in todo:
                    n = gallery(paths_of(m), rule.with_params(points[k]), m, pool, years).height
                    counted[k] += n
                    step(n)

    def entry(k: int) -> dict:
        return {"values": points[k], "tune": scores(k, "tune"), "test": scores(k, "test"), "hits": counted[k]}

    explained = []
    for x in use_labels:
        if agrees[cur].get(x["id"], False):
            continue
        ok = [k for k in range(len(points)) if agrees[k].get(x["id"], False)]
        k = min(ok, key=lambda k: distance(points[k])) if ok else None
        explained.append({"label": x["id"], "values": points[k] if k is not None else None})

    n = len(marks) + len(use_labels)
    right, wrong = sum(m["verdict"] == "right" for m in marks), sum(m["verdict"] == "wrong" for m in marks)
    in_test = grp.count("test") + sum(x["grp"] == "test" for x in use_labels)
    # with marks of one kind only, the score can only rise one way (no wrong marks: a looser setting loses nothing), and a
    # test group of one or two marks says nothing — the groups are fixed by the marks themselves, so few marks split unevenly
    warnings = [f"只有 {n} 个标注，校准结果很可能只是贴合了它们；多标一些再校准" if n < FEW else None,
                "只标了对的、没有标错的：放宽的设置不会多出标错的命中，校准只会往放宽（命中更多）的方向走；也标一些错的再校准"
                if right and not wrong else None,
                "只标了错的、没有标对的：收紧的设置不会丢掉标对的命中，校准只会往收紧的方向走；也标一些对的再校准"
                if wrong and not right else None,
                f"检验组只有 {in_test} 个标注（按标注本身固定分组，少的时候分得不均），检验那一列说明不了什么" if n and in_test < 3 else None]
    return {
        "params": names, "axes": ax, "combos": combos, "structural": structural, "skipped": skipped,
        "current": entry(cur), "candidates": [entry(k) for k in front], "explained": explained,
        "samples": {"right": sum(m["verdict"] == "right" for m in marks), "wrong": sum(m["verdict"] == "wrong" for m in marks),
                    "boxes": len(use_labels), "tune": grp.count("tune") + sum(x["grp"] == "tune" for x in use_labels),
                    "test": grp.count("test") + sum(x["grp"] == "test" for x in use_labels),
                    # what each half's shares are of (67% of 3 marks is not 67% of 30)
                    "by_group": {g: {"right": sum(m["verdict"] == "right" and grp[i] == g for i, m in enumerate(marks)),
                                     "wrong": sum(m["verdict"] == "wrong" and grp[i] == g for i, m in enumerate(marks)),
                                     "boxes": sum(x["grp"] == g for x in use_labels)} for g in ("tune", "test")}},
        "warning": "；".join(w for w in warnings if w) or None,
    }
