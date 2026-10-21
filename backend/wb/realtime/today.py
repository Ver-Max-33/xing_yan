"""盘中评估 (spec §7.2): today's temporary daily bar from the minutes so far — volume and turnover scaled to the whole
day (按全天成交节奏折算) — goes after the stock's stored bars, and the rule runs on it as the scan would if the day closed now,
formulas and structures alike (prefix consistency, §5.8, keeps the past as it was). Nothing here is ever recorded as
a signal: the scan on the close is the record. A price the day's limits do not allow means an ex-rights day whose
factor has not come: such a stock is not judged."""
from datetime import date

import polars as pl

from wb.config import Paths
from wb.data.store import Store
from wb.engine.l1.frame import available_columns, build_frame
from wb.engine.l2.run import chunks, compute, l2_bars, node_hash
from wb.engine.l3.explain import render
from wb.engine.l3.match import Matcher
from wb.engine.l3.rule import Rule
from wb.engine.l3.run import primitives
from wb.listing import parse_id
from wb.markets import cn_limit_pct, limit_width, load_market
from wb.realtime.clock import share
from wb.registry import PRIMITIVES


def turnover(minutes: pl.DataFrame) -> pl.Expr:
    """Each minute's turnover: its own when the source gives it (TickFlow), else its close × its volume."""
    approx = pl.col("close") * pl.col("volume")
    return pl.coalesce("amount", approx) if "amount" in minutes.columns else approx


def temp_bars(minutes: pl.DataFrame, day: date, total: int, covered: int, clock: tuple[float, ...] | None = None) -> pl.DataFrame:
    """listing_id, date, open (the first minute's), high, low, close (the last), volume and amount scaled to the day:
    ÷ the share of the day usually done by now on the market's volume clock, or × total ÷ covered without one."""
    scale = 1 / share(clock, covered, total)
    return (minutes.sort("at").group_by("listing_id", maintain_order=True)
            .agg(pl.col("open").first(), pl.col("high").max(), pl.col("low").min(), pl.col("close").last(),
                 (pl.col("volume").sum() * scale).alias("volume"), (turnover(minutes).sum() * scale).alias("amount"))
            .with_columns(pl.lit(day).alias("date")))


def snapshot_bars(snap: pl.DataFrame, day: date, total: int, covered: int, clock: tuple[float, ...] | None = None) -> pl.DataFrame:
    """A snapshot source's day so far as today's bar: its open, high and low, the price now, and volume and turnover
    since the open scaled to the day as `temp_bars` does (a missing open, high or low is the price now); its 昨收, the
    exchange's reference (an ex-date's, day_factors)."""
    scale = 1 / share(clock, covered, total)
    return snap.select("listing_id", *[pl.coalesce(c, "last").alias(c) for c in ("open", "high", "low")], pl.col("last").alias("close"),
                       (pl.col("volume") * scale).alias("volume"), (pl.col("amount") * scale).alias("amount"), pl.lit(day).alias("date"),
                       *(["pre_close"] if "pre_close" in snap.columns else []))


def day_factors(bars: pl.DataFrame, market: str) -> pl.DataFrame:
    """Today's 复权 factors on today's temporary bars, an ex-date's not yet published (review 10-13: carried from the day
    before, a 送转 / dividend day gapped the K-line and stood the lines off in today's prices). From each listing's last
    stored bar — `_close` (the exchange's close), `_adj`, `_share` — and the day's corporate action — `_ratio` (shares
    after per share before), `_cash` (cash per share; A shares' factor counts it, Japan's only splits) — or the quote's
    昨收 (`pre_close`, the exchange's reference price) where it moved. adj_factor and share_factor: today's, none where
    unchanged (carried forward as before); pre_close: the reference."""
    quote = pl.col("pre_close") if "pre_close" in bars.columns else pl.lit(None, pl.Float64)
    event = pl.col("_ratio").is_not_null()
    cash = pl.col("_cash").fill_null(0.0) if market == "CN" else pl.lit(0.0)
    moved = (quote - pl.col("_close")).abs() > 0.005   # beyond a 分's rounding: the exchange reset the price
    pre = pl.when(quote.is_not_null() & (event | moved)).then(quote).when(event).then((pl.col("_close") - cash) / pl.col("_ratio"))
    return bars.with_columns(pre.alias("_pre")).with_columns(
        (pl.col("_adj") * pl.col("_close") / pl.col("_pre")).alias("adj_factor"),
        pl.when(event).then(pl.col("_share").fill_null(1.0) * pl.col("_ratio")).alias("share_factor"),
        pl.coalesce(quote, pl.col("_pre")).alias("pre_close")).drop("_pre")


def day_basis(store: Store, day: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    """What day_factors reads, for every listing: its last stored bar before `day` (`_close`, `_adj`, `_share`) and the
    day's corporate actions (`_ratio`, `_cash`) — read once a day, joined to each poll's bars."""
    bars = store.scan("bars_1d").filter((pl.col("date") < day) & pl.col("close").is_not_null())
    share = "share_factor" in bars.collect_schema().names()
    last = bars.group_by("listing_id").agg(
        pl.col("close").sort_by("date").last().alias("_close"),
        pl.col("adj_factor").sort_by("date").drop_nulls().last().alias("_adj"),
        (pl.col("share_factor").sort_by("date").drop_nulls().last() if share else pl.lit(None, pl.Float64)).alias("_share")).collect()
    acts = (store.scan("corp_actions").filter(pl.col("ex_date") == day).group_by("listing_id")
            .agg(pl.col("ratio").fill_null(1.0).product().alias("_ratio"), pl.col("cash_div").fill_null(0.0).sum().alias("_cash"))
            .collect())
    return last, acts


def with_day_factors(bars: pl.DataFrame, market: str, basis: tuple[pl.DataFrame, pl.DataFrame]) -> pl.DataFrame:
    """`bars` (today's temporary bars) with today's factors and reference price (day_factors) from `basis` (day_basis)."""
    last, acts = basis
    out = day_factors(bars.join(last, on="listing_id", how="left").join(acts, on="listing_id", how="left"), market)
    return out.drop("_close", "_adj", "_share", "_ratio", "_cash")


def ex_rights(paths: Paths, market: str, bars: pl.DataFrame) -> set[str]:
    """The stocks whose price now is beyond what the day's limits allow from their last close."""
    store = Store(paths, market)
    if store.latest() is None or bars.is_empty():
        return set()
    prev = (store.scan("bars_1d").filter(pl.col("listing_id").is_in(bars["listing_id"].implode()) & (pl.col("date") < bars["date"].min())
                                          & pl.col("close").is_not_null())
            .group_by("listing_id").agg(pl.col("close").sort_by("date").last().alias("prev")).collect())
    m, out = load_market(market), set()
    for lid, close, p in bars.join(prev, on="listing_id").select("listing_id", "close", "prev").iter_rows():
        width = limit_width(m, p) if market == "JP" else p * cn_limit_pct(parse_id(lid)[1]) + 0.01   # a limit price is rounded
        if abs(close - p) > width + 1e-6:
            out.add(lid)
    return out


def _keyed(tables: dict[str, pl.DataFrame]) -> dict[str, tuple[pl.DataFrame, bool]]:
    """A primitive's tables each with its listing: a box's touches and events are given their box's (joined once a
    poll; True: the column is not the table's own)."""
    boxes = [df.select("box_id", "listing_id") for df in tables.values() if {"box_id", "listing_id"} <= set(df.columns)]
    out = {}
    for t, df in tables.items():
        if "listing_id" in df.columns:
            out[t] = (df, False)
        else:
            out[t] = (df.join(pl.concat(boxes), on="box_id", how="left", maintain_order="left"), True)
            if out[t][0].height != df.height:
                raise ValueError(f"{t}: a box id twice")
    return out


def _mine(keyed: dict[str, tuple[pl.DataFrame, bool]], ids: pl.Series) -> dict[str, pl.DataFrame]:
    """A primitive's tables on some of their stocks: the rows of theirs. A filter keeps the order — what the primitive
    computes on those stocks alone (it works listing by listing)."""
    return {t: (df.filter(pl.col("listing_id").is_in(ids)).drop("listing_id") if joined else df.filter(pl.col("listing_id").is_in(ids)))
            for t, (df, joined) in keyed.items()}


class Shared:
    """A poll's structures for the rules it judges: each primitive computed once, on today's bars of the stocks of all
    the rules that read it with the same settings (one node hash, spec §5.7) — not once a rule — each rule given its own
    stocks' rows. Inside box.resumed the bars before today are scanned once a day."""

    def __init__(self, paths: Paths, market: str, bars: pl.DataFrame, rules: list[tuple[Rule, list[str]]]):
        """`bars`: today's bars; `rules`: each rule the poll judges, with its stocks."""
        self.paths, self.market, self.bars = paths, market, bars
        self._ids: dict[str, set[str]] = {}   # node -> the stocks of the rules reading it
        for rule, ids in rules:
            try:   # one that cannot say what it reads is refused on its own (tables): never the others' trouble
                params = rule.primitive_params()
                for n in primitives(rule):
                    self._ids.setdefault(node_hash(n, params), set()).update(ids)
            except Exception:  # noqa: BLE001
                continue
        self._l2: pl.DataFrame | None = None
        self._done: dict[str, dict[str, tuple[pl.DataFrame, bool]]] = {}

    def tables(self, names: list[str], params: dict, ids: list[str]) -> dict[str, dict[str, pl.DataFrame]]:
        if not all(set(ids) <= self._ids.get(node_hash(n, params), set()) for n in names):
            raise ValueError("Shared: a rule this poll did not say it judges")
        mine = pl.Series(ids, dtype=pl.Utf8).implode()
        return {n: _mine(self._run(n, params), mine) for n in names}

    def _run(self, name: str, params: dict) -> dict[str, tuple[pl.DataFrame, bool]]:
        """spec.compute's run, each node once: what it requires with the same settings (on the same stocks), then itself."""
        key = node_hash(name, params)
        if key not in self._done:
            if self._l2 is None:
                b = self.bars.filter(pl.col("listing_id").is_in(list(set().union(*self._ids.values()))))
                store = Store(self.paths, self.market)
                self._l2 = l2_bars(store, self.market, self.paths.profile, b["listing_id"].to_list(), snap=store.latest(), extra=b)
            bars = self._l2.filter(pl.col("listing_id").is_in(pl.Series(sorted(self._ids[key]), dtype=pl.Utf8).implode()))
            p = PRIMITIVES[name]
            deps = {r: self._run(r, params) for r in p.requires}
            parts = []   # a few dozen stocks at a time, as the scan's compute_market: a stock's boxes are paired with its others
            for s, n in chunks(bars.group_by("listing_id", maintain_order=True).len()["len"].to_list(), 1):
                part = bars.slice(s, n)
                mine = part["listing_id"].unique().implode()
                parts.append(p.compute(part, params.get(name), {r: _mine(d, mine) for r, d in deps.items()}))
            tables = ({t: pl.concat([x[t] for x in parts], how="diagonal_relaxed") for t in parts[0]} if parts else
                      p.compute(bars, params.get(name), {r: _mine(d, pl.Series([], dtype=pl.Utf8).implode()) for r, d in deps.items()}))
            self._done[key] = _keyed(tables)
        return self._done[key]


def evaluate_today(paths: Paths, rule: Rule, market: str, bars: pl.DataFrame, shared: Shared | None = None) -> dict[str, dict]:
    """Each stock's status on today's bar (signal / watch / unknown / fail) and, for a signal, its explanation and that
    explanation in words (the scan's, §5.6) — what alerts. `shared`: the poll's structures, computed once for every rule
    reading them. A cross-sectional rule reads the whole market's day, which the intraday source does not have: not
    judged ({})."""
    store = Store(paths, market)
    snap = store.latest()
    if snap is None or bars.is_empty():
        return {}
    available = available_columns(store, snap)
    prog = Matcher.program(rule, market, available)
    if prog.xs:
        return {}
    ids, day = bars["listing_id"].to_list(), bars["date"].min()
    names, params = primitives(rule), rule.primitive_params()
    tables = ((shared.tables(names, params, ids) if shared is not None else
               compute(l2_bars(store, market, paths.profile, ids, snap=snap, extra=bars), names, params)) if names else {})
    frame = build_frame(store, market, paths.profile, prog.columns, ids, snap=snap, extra=bars)
    calendar = sorted(set(store.read("calendar", snap).filter(pl.col("is_open"))["date"].to_list()) | {day})
    m = Matcher(rule, market, frame, tables, calendar, available, None)
    out = {}
    for r in m.at(day).iter_rows(named=True):
        out[r["listing_id"]] = {"status": r["status"]}
        if r["status"] == "signal":   # one by one, each explanation reads the listing's rows: only what alerts
            detail = m.detail(r["listing_id"], day)
            out[r["listing_id"]] |= {"detail": detail, "lines": render(detail)}
    return out
