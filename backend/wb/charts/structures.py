"""Structures of one listing for the chart (spec §8.2 overlays, §6.3 single-stock debugging and replay).

The tables are the L2 primitives' own output, so the chart shows exactly what the rules read. Prices
are converted from the computation basis (backward-adjusted) to the display basis on their own day:
one constant for qfq and hfq, the day's factor for raw prices — so a box across an ex-date is drawn as
two segments and a slanted line bends there. Every row keeps its known_at (boxes also ended_at and
superseded_at): the replay shows only what was known on its day (spec §5.8 rule 5)."""
from bisect import bisect_left, bisect_right
from datetime import date

import polars as pl

PRICE_EVENTS = {"spike_up", "spike_down", "touch_top", "touch_bottom"}  # the others carry a ratio or nothing
_BOX = ("scale", "status", "start", "known_at", "end", "ended_at", "end_reason", "width", "length", "shape", "context",
        "position", "excluded_before", "superseded_at")


def factors(daily: pl.DataFrame, basis: str) -> tuple[list[date], list[float]]:
    """Trading dates and, per date, display price ÷ backward-adjusted price."""
    d = daily.sort("date")
    adjusted = d["adj_factor"].null_count() < d.height
    adj = pl.col("adj_factor").fill_null(strategy="forward").fill_null(1.0)
    if not adjusted or basis == "hfq":
        f = pl.lit(1.0)
    elif basis == "raw":
        f = 1.0 / adj
    else:
        f = pl.lit(1.0 / d.filter(pl.col("adj_factor").is_not_null())["adj_factor"][-1])
    out = d.select("date", f.cast(pl.Float64).alias("f"))
    return out["date"].to_list(), out["f"].to_list()


def display_per_raw(daily: pl.DataFrame, basis: str) -> tuple[list[date], list[float]]:
    """Trading dates and, per date, display price ÷ exchange price (the chart's `scale`)."""
    d = daily.sort("date")
    adjusted = d["adj_factor"].null_count() < d.height
    adj = pl.col("adj_factor").fill_null(strategy="forward").fill_null(1.0)
    if basis == "raw" or not adjusted:
        s = pl.lit(1.0)
    elif basis == "hfq":
        s = adj
    else:
        s = adj / d.filter(pl.col("adj_factor").is_not_null())["adj_factor"][-1]
    out = d.select("date", s.cast(pl.Float64).alias("s"))
    return out["date"].to_list(), out["s"].to_list()


class _Basis:
    def __init__(self, daily: pl.DataFrame, basis: str, traded: list[date] | None):
        self.dates, self.f = factors(daily, basis)
        self.changes = [i for i in range(1, len(self.f)) if self.f[i] != self.f[i - 1]]
        self.traded = traded if traded is not None else self.dates
        self.tpos = {d: i for i, d in enumerate(self.traded)}

    def at(self, d: date | None) -> float:
        """The factor on day d (the last known one before it if d has no row)."""
        if d is None or not self.dates:
            return 1.0
        i = bisect_right(self.dates, d) - 1
        return self.f[max(i, 0)]

    def price(self, d: date | None, p: float | None) -> float | None:
        return None if p is None else p * self.at(d)

    def pieces(self, start: date, end: date | None) -> list[tuple[date, date | None]]:
        """[start, end] cut where the factor changes (end None: still open). The cuts fall between traded
        bars: a change on a day without trading starts the next piece on the next bar."""
        a = bisect_left(self.dates, start)
        b = bisect_right(self.dates, end) - 1 if end is not None else len(self.dates) - 1
        cuts = self.changes[bisect_right(self.changes, a):bisect_right(self.changes, b)]
        out, s = [], start
        for c in cuts:
            k = bisect_left(self.traded, self.dates[c])                  # the first bar at the new factor
            last = self.traded[k - 1] if k > 0 else self.dates[c - 1]
            if last >= s:                                                # two changes in one halt: one cut
                out.append((s, last))
            s = self.traded[k] if k < len(self.traded) else self.dates[c]
        out.append((s, end))
        return out


def _rows(t: pl.DataFrame | None) -> list[dict]:
    return [] if t is None or t.is_empty() else t.to_dicts()


def _boxes(tables: dict, b: _Basis) -> list[dict]:
    """Each version of a box with the touches and events of all its versions (each known from its own day)."""
    box = tables.get("BOX", {})
    rows = _rows(box.get("boxes"))
    line = {r["box_id"]: r.get("lineage") or r["box_id"] for r in rows}
    first: dict = {}   # the day each box was first drawn: its versions' earliest known_at
    for r in rows:
        k = line[r["box_id"]]
        first[k] = min(first.get(k, r["known_at"]), r["known_at"])
    touches, events = {}, {}
    for r in _rows(box.get("touches")):
        touches.setdefault(line.get(r["box_id"], r["box_id"]), []).append(
            {"date": r["date"], "side": r["side"], "price": b.price(r["date"], r["price"]), "known_at": r["known_at"]})
    for r in _rows(box.get("events")):
        v = b.price(r["date"], r["value"]) if r["event"] in PRICE_EVENTS else r["value"]
        events.setdefault(line.get(r["box_id"], r["box_id"]), []).append(
            {"date": r["date"], "event": r["event"], "value": v, "known_at": r["known_at"]})
    out = []
    for r in rows:
        segs = [{"from": s, "to": e, "top": b.price(s, r["top"]), "bottom": b.price(s, r["bottom"])} for s, e in b.pieces(r["start"], r["end"])]
        out.append({"id": r["box_id"], **{k: r.get(k) for k in _BOX}, "first_known": first[line[r["box_id"]]], "segments": segs,
                    "touches": touches.get(line[r["box_id"]], []), "events": events.get(line[r["box_id"]], [])})
    return out


def _line(b: _Basis, a: date, pa: float, z: date, pz: float, start: date, end: date | None) -> list[dict]:
    """A slanted edge (linear in price over traded bars, as the primitive draws it) from start to end,
    as display-basis points: a point at every cut, so it stays exact across ex-dates."""
    ia, iz = b.tpos.get(a), b.tpos.get(z)
    if ia is None or iz is None or ia == iz:
        return []
    out = []
    for s, e in b.pieces(start, end):
        for d in (s, e if e is not None else b.traded[-1]):
            i = b.tpos.get(d)
            if i is not None:
                out.append({"date": d, "price": (pa + (pz - pa) * (i - ia) / (iz - ia)) * b.at(s)})
    return out


def _slant(tables: dict, b: _Basis) -> list[dict]:
    out = []
    for r in _rows(tables.get("SLANT_BOX", {}).get("slant")):
        out.append({"id": r["box_id"], **{k: r.get(k) for k in ("status", "start", "known_at", "end", "ended_at", "end_reason", "width")},
                    "top": _line(b, r["top_a"], r["top_a_price"], r["top_b"], r["top_b_price"], r["start"], r["end"]),
                    "bottom": _line(b, r["bottom_a"], r["bottom_a_price"], r["bottom_b"], r["bottom_b_price"], r["start"], r["end"])})
    return out


def _false_bottoms(tables: dict, b: _Basis) -> list[dict]:
    """假底 rows with the old box's edges for the chart's two faint lines. ponytail: one factor for the three lines, the
    break day's — a raw-price line across an ex-date is off by the dividend; cut them into pieces like boxes if it shows."""
    edges = {r["box_id"]: (r["top"], r["bottom"]) for r in _rows(tables.get("BOX", {}).get("boxes"))}
    out = []
    for r in _rows(tables.get("BOX_DERIVED", {}).get("false_bottoms")):
        top, bottom = edges.get(r["box_id"], (None, None))
        k = r["known_at"]
        out.append({"box_id": r["box_id"], "source": r["source"], "date": r["date"], "price": b.price(k, r["price"]),
                    "known_at": k, "end": r["end"], "end_reason": r["end_reason"],
                    "box_top": b.price(k, top), "box_bottom": b.price(k, bottom)})
    return out


def overlay(tables: dict, daily: pl.DataFrame, basis: str, traded: list[date] | None = None) -> dict:
    """`tables`: compute()'s output for one listing; `daily`: its raw daily rows (date, adj_factor);
    `traded`: the bars the primitives counted (default: every daily row)."""
    b = _Basis(daily, basis, traded)
    derived = tables.get("BOX_DERIVED", {})
    return {
        "boxes": _boxes(tables, b),
        "slant": _slant(tables, b),
        "pivots": [{"date": r["date"], "kind": r["kind"], "price": b.price(r["date"], r["price"]), "known_at": r["known_at"]}
                   for r in _rows(tables.get("PIVOTS", {}).get("pivots"))],
        "swings": [{"start": r["start"], "end": r["end"], "direction": r["direction"], "change": r["change"],
                    "start_price": b.price(r["start"], r["start_price"]), "end_price": b.price(r["end"], r["end_price"]),
                    "known_at": r["known_at"]} for r in _rows(tables.get("SWING", {}).get("swings"))],
        "gold": [{**{k: r[k] for k in ("small_box_id", "big_box_id", "kind", "variant", "known_at")}, "superseded_at": r.get("superseded_at")}
                 for r in _rows(derived.get("gold"))],
        "false_bottoms": _false_bottoms(tables, b),
    }
