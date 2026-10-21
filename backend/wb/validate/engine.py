"""The signals of a rule and its variants over a whole market (spec §9.5 计算量, §5.7), a chunk of listings at a
time: one chunk's frame and structures are built once for every variant — a variant without a condition, or with
another threshold, is judged on the same evaluated frame (`Matcher.variant`), another formula or structure setting
gets its own. Memory stays near a chunk's (~1–2 GB on CN) instead of a ten-year range over the whole market (~10 GB);
a cross-sectional rule reads the whole market in one piece. Listings that were delisted later are included (§9.4)."""
import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import available_columns, build_frame, not_stocks
from wb.engine.l2.run import compute_market, l2_bars, structures
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import Plan, Rule, RuleError
from wb.engine.l3.run import primitives
from wb.validate.cross import cross_signals

CHUNK = 800  # listings a pass


class Keep:
    """A validation run's finished chunks, on disk (暂停 and 继续, 1.32): each variant's signals of a chunk, keyed by the
    run's k-th signal pass, the market, the snapshot and the chunk — a run continued later reads them back instead of
    computing them again (a new snapshot meanwhile: computed again). Set for a run through KEEP."""

    def __init__(self, folder: Path):
        self.folder, self.calls = folder, 0

    def _name(self, key: tuple) -> str:
        return "-".join(map(str, key))

    def get(self, key: tuple, variants: int) -> list[pl.DataFrame] | None:
        if not (self.folder / f"{self._name(key)}.ok").exists():   # written last: a chunk cut off half saved is not one
            return None
        return [pl.read_parquet(self.folder / f"{self._name(key)}-{i}.parquet") for i in range(variants)]

    def put(self, key: tuple, frames: list[pl.DataFrame]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        for i, df in enumerate(frames):
            df.write_parquet(self.folder / f"{self._name(key)}-{i}.parquet")
        (self.folder / f"{self._name(key)}.ok").touch()

    # a part done step by step (a calibration's parameter sets, 1.33): one JSON line a finished step
    def rows(self, key: tuple) -> list:
        path = self.folder / f"{self._name(key)}.jsonl"
        if not path.exists():
            return []
        done, _, cut = path.read_text(encoding="utf-8").rpartition("\n")
        if cut:   # a line cut off half written (the app quit): not one, and gone before the next is added
            path.write_text(done + "\n" if done else "", encoding="utf-8")
        return [json.loads(x) for x in done.split("\n")] if done else []

    def add(self, key: tuple, value) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.folder / f"{self._name(key)}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(value) + "\n")


KEEP: ContextVar[Keep | None] = ContextVar("validation_keep", default=None)


@dataclass(frozen=True)
class Variant:
    rule: Rule
    skip: frozenset = field(default_factory=frozenset)  # (stage, condition id) left out


def _key(rule: Rule, market: str, available: set[str]) -> tuple:
    """Variants with the same key share an evaluated frame: the same structures and the same formula columns."""
    prog = Matcher.program(rule, market, available)  # a $param inside a formula is part of its expression
    return (json.dumps(rule.primitive_params(), sort_keys=True, default=str),
            tuple(sorted((c, str(e)) for step in prog.steps for c, e in step.items())), tuple(str(t) for t in prog.tf))


def _slice(tables: dict, chunk: list[str]) -> dict:
    """A primitive's tables for these listings: by listing_id, and a table keyed by its parent (a box's touches by
    box_id) by the parent rows kept; a table nothing ties to a listing is kept whole."""
    out = {}
    for p, ts in tables.items():
        kept = {t: df.filter(pl.col("listing_id").is_in(chunk)) for t, df in ts.items() if "listing_id" in df.columns}
        for t, df in ts.items():
            if t in kept:
                continue
            key = next((c for c in df.columns if c.endswith("_id") and any(c in k.columns for k in kept.values())), None)
            kept[t] = df if key is None else df.join(
                pl.concat([k.select(key) for k in kept.values() if key in k.columns]).unique(), on=key, how="semi")
        out[p] = {t: kept[t] for t in ts}
    return out


def _with_lines(sig: pl.DataFrame, rule: Rule, tables: dict) -> pl.DataFrame:
    """The signals with their box line — the first BOX stage's bottom × (1 − tol), in the adjusted prices the boxes are
    in, judged on the close like BOX's break_down (组合回测's 跌破箱体下沿止损) — null for a rule without a BOX stage."""
    stage = next((s.id for s in rule.stages if s.kind == "structure" and s.use == "BOX"), None)
    boxes = tables.get("BOX", {}).get("boxes")
    if stage is None or boxes is None or boxes.is_empty():
        return sig.with_columns(pl.lit(None, pl.Float64).alias("stop"))
    tol = rule.primitive_params().get("BOX", {}).get("tol", 0.015)
    return sig.join(boxes.select(pl.col("box_id").alias(stage), (pl.col("bottom") * (1 - tol)).alias("stop")), on=stage, how="left")


def _watch_key(rule: Rule, skip: frozenset) -> str:
    """What a rule's 潜伏 list reads — its stages up to the watch stage and the conditions of them it keeps, its ranking
    (the plan's lines), pool and pressure, the parameters these read: variants alike in it wait on the same list (a
    check-up's breakout-volume variants: a cross does not read the breakout bar)."""
    ids = [s.id for s in rule.stages]
    k = ids.index(rule.watch_stage)
    text = json.dumps({"stages": [s.model_dump(mode="json", by_alias=True, exclude_none=True) for s in rule.stages[: k + 1]],
                       "next": rule.stages[k + 1].on if k + 1 < len(ids) else None,
                       "ranking": [r.model_dump(mode="json") for r in rule.ranking], "universe": rule.universe.model_dump(mode="json"),
                       "pressure": rule.pressure.model_dump(mode="json")}, ensure_ascii=False, sort_keys=True)
    used = sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", text)))
    return json.dumps([text, sorted(x for x in skip if x[0] in ids[: k + 1]), [(p, rule.param(p)) for p in used]],
                      ensure_ascii=False, default=str)


def _crosses(m: Matcher, v: Variant, frame: pl.DataFrame, plan: Plan, calendar: list[date], before: date, start: date,
             end: date, replays: dict) -> pl.DataFrame:
    """A variant's crosses on this chunk: its 潜伏 rows from the evening before `start` (one replay for the variants
    that wait on the same list, `replays`) less the days its own rule signals, crossed on [start, end], each with
    whether its rule signalled that close."""
    bars = frame.select("listing_id", "date", pl.col("raw_open").alias("open"), pl.col("raw_high").alias("high"),
                        pl.col("raw_close").alias("close"),
                        (pl.col("close") / pl.col("raw_close")).forward_fill().over("listing_id").alias("adj"))   # as plan_trades
    key = _watch_key(v.rule, v.skip)
    if key not in replays:
        replays[key] = m.watches(before, end, as_if_break=True, all_days=True)
    days = m._passing(before, end)
    days = (days.select("listing_id", pl.col("signal_date").alias("date")).unique() if not days.is_empty()
            else pl.DataFrame(schema={"listing_id": pl.Utf8, "date": pl.Date}))
    w = replays[key].join(days, on=["listing_id", "date"], how="anti")
    x = cross_signals(w, bars, plan, calendar).filter(pl.col("signal_date").is_between(start, end))
    sig = days.select("listing_id", pl.col("date").alias("signal_date"), pl.lit(True).alias("signal"))
    return x.join(sig, on=["listing_id", "signal_date"], how="left").with_columns(pl.col("signal").fill_null(False))


def market_signals(paths: Paths, market: str, variants: list[Variant], start: date, end: date,
                   progress: Callable[[int, int], None] | None = None, lines: bool = False,
                   cross: Plan | None = None) -> list[pl.DataFrame]:
    """Each variant's signals in [start, end]: listing_id, signal_date (one per structure instance, as a range run);
    `lines`: also the structures they stand on, the box line (`stop`) and the rule's ranking values (rank0, …).
    `cross` (a plan buying by cross, design 2026-10-11 §6): each variant's crosses instead — its 潜伏 rows (the 假底 as
    if broken the next day) bought on the next day's bars where the high first reaches the line (cross_signals), the
    ranking read the evening before, with `signal` (the rule's signal that close) — a cross day in [start, end]."""
    store = Store(paths, market)
    snap = store.latest()
    if snap is None:
        raise RuleError(f"{market} 还没有数据快照")
    available = available_columns(store, snap)
    calendar = store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()
    listed = store.scan("bars_1d", snap).select("listing_id").unique()
    if (funds := not_stocks(store, market, snap)) is not None:   # the stock pool, as a whole-market frame reads it
        listed = listed.join(funds, on="listing_id", how="anti")
    lids = sorted(listed.collect()["listing_id"].to_list())
    xs = any(Matcher.cross_sectional(v.rule, market, available) for v in variants)
    chunks = [lids] if xs else [lids[i:i + CHUNK] for i in range(0, len(lids), CHUNK)]
    columns = set().union(*(Matcher.columns(v.rule, market, available) for v in variants))
    if cross is not None:   # the cross day's bars, raw
        columns |= {"raw_open", "raw_high", "raw_close"}
        before = max((d for d in calendar if d < start), default=start)   # a cross on `start` is the evening before's
    keys = [_key(v.rule, market, available) for v in variants]
    own = json.dumps(variants[0].rule.primitive_params(), sort_keys=True, default=str)
    names = sorted(set().union(*(primitives(v.rule) for v in variants)))
    whole = structures(paths, market, names, variants[0].rule.primitive_params(), snap=snap) if names else {}  # cached
    out: list[list[pl.DataFrame]] = [[] for _ in variants]
    groups: dict[tuple, list[int]] = {}
    for i, key in enumerate(keys):
        groups.setdefault(key, []).append(i)
    keep = KEEP.get()
    if keep is not None:
        keep.calls += 1
    for n, chunk in enumerate(chunks, 1):
        kept_as = (keep.calls, market, snap.id, len(chunks), n) if keep is not None else None
        if kept_as is not None and (kept := keep.get(kept_as, len(variants))) is not None:   # done before a pause
            for i, df in enumerate(kept):
                out[i].append(df)
            if progress:
                progress(n, len(chunks))
            continue
        frame = build_frame(store, market, paths.profile, columns, None if xs else chunk, snap=snap)
        bars = None
        for key, idxs in groups.items():  # one evaluated frame (and its structures) alive at a time
            rule = variants[idxs[0]].rule
            if not names:
                tables = {}
            elif key[0] == own:  # the rule's own settings come from the market's cache, others are computed here
                tables = whole if xs else _slice(whole, chunk)
            else:
                bars = bars if bars is not None else l2_bars(store, market, paths.profile, chunk, snap=snap)
                tables = compute_market(bars, names, rule.primitive_params())
            m = Matcher(rule, market, frame, tables, calendar, available, chunk if xs else None)
            replays: dict = {}
            for i in idxs:
                v = m.variant(variants[i].rule, variants[i].skip)
                if cross is not None:
                    sig = _crosses(v, variants[i], frame, cross, calendar, before, start, end, replays)
                else:
                    sig = v.signals(start, end, ranks=lines)
                keep_all = lines or cross is not None   # a cross keeps its price and how it went, lines or not
                out[i].append(_with_lines(sig, variants[i].rule, tables) if keep_all else sig.select("listing_id", "signal_date"))
            del m, tables
        del frame, bars
        if kept_as is not None:
            keep.put(kept_as, [parts[-1] for parts in out])   # each variant got this chunk's signals just now
        if progress:   # where a pause or a cancel stops the run: after a chunk, with nothing of it held
            progress(n, len(chunks))
    return [pl.concat(parts) if parts else pl.DataFrame(schema={"listing_id": pl.Utf8, "signal_date": pl.Date}) for parts in out]
