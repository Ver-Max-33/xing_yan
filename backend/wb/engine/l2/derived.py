"""Structures derived from boxes (spec §5.4 由箱体推出的结构). Nothing re-reads the bars for its shape;
each is known when the latest structure it depends on is known.

  NEST      箱中箱: a box of a smaller scale inside a larger one, in time and price.
  GOLD      金品: the small box C at the end of the big box P (at the big box's end or just after its break; a 曲箱 that
            flattens out at its end becomes one too) — P's last small box: one is
            `superseded_at` when the next small box in P becomes known. Classified by where C sits (the position
            decides the name, whatever a chart's caption calls it):
              天品  C forms after P broke up and stays above P's old top;
              上品  C inside P with its top within tol of P's top;
              下品  C inside P with its top more than tol below P's top; the inner distance
                    (P top − C top) / C top is kept. The two 10% readings stay
                    template choices, not part of the class (spec §5.4, §6.6).
            A rising slant small box makes it 变异 (slant boxes join this table when present).
  RELATION  successive boxes A then B of one level (the big machine's, the 中箱 inside 大箱, 小箱): 邻 (an edge within
            tol of the other's opposite edge), 中 (one price range inside the other), 交 (partial overlap), 隔 (a gap), plus the
            寻箱线 between them: direction, change and bars from A's end to B's start.
  FALSE_BOTTOM 假底: on a mid or long box's first upward break that has one, its
            current 上品 / 下品's bottom, else C — the low of the pullback after its last top touch, above its bottom
            band and above the bottom touch before (a pullback that reaches the bottom leaves none). A line forward until lost, a new box of
            the scale, or a horizon.

A box's end, end reason and length are known only from its `ended_at`, so nothing here conditions
on them before that (spec §5.8). The self-joins carry only the columns they use: a full-width join of
every box with every box of the same listing took ~1.9 GB per worker chunk.
"""
from bisect import bisect_left
from collections import defaultdict

import numpy as np
import polars as pl

SCALE_RANK = {"slant": 0, "short": 0, "mid": 1, "long": 2}  # a slant box plays the small box (变异金品)
# a 天品's small box may take in this many bars before the break: its window can reach the last bars at the top
# (002765 in 2017-08 and 600425 in 2017-01: one bar); one that began earlier formed inside the box (上品)
SKY_EARLY = 2
GOLD_SCHEMA = {"listing_id": pl.Utf8, "kind": pl.Utf8, "variant": pl.Boolean, "big_box_id": pl.Utf8,
               "small_box_id": pl.Utf8, "known_at": pl.Date, "big_top": pl.Float64, "big_bottom": pl.Float64,
               "small_top": pl.Float64, "small_bottom": pl.Float64, "inner_distance": pl.Float64,
               "above_top": pl.Float64, "superseded_at": pl.Date}


def _est(boxes: pl.DataFrame) -> pl.DataFrame:
    return boxes.filter(pl.col("status") == "established")


def _superseded(boxes: pl.DataFrame) -> pl.Expr:
    """A version's superseded_at (none for slant boxes, which are not redrawn)."""
    return pl.col("superseded_at") if "superseded_at" in boxes.columns else pl.lit(None, pl.Date).alias("superseded_at")


def _lineage(boxes: pl.DataFrame) -> pl.Expr:
    """The box a version belongs to (a slant box is its own)."""
    return (pl.coalesce("lineage", "box_id") if "lineage" in boxes.columns else pl.col("box_id")).alias("lineage")


def _level() -> pl.Expr:
    """Which boxes follow one another: the big machine's 中箱 / 大箱, the 中箱 drawn inside a 大箱 (the inner machine's,
    id …:inner:…), 小箱."""
    big = pl.col("scale").is_in(["mid", "long"])
    return (pl.when(big & pl.col("box_id").str.contains(":inner:")).then(pl.lit("inner"))
            .when(big).then(pl.lit("big")).otherwise(pl.col("scale")).alias("_level"))


def _suffixed(df: pl.DataFrame, suffix: str) -> pl.DataFrame:
    """Every column but the join key renamed (a join only suffixes names both sides share)."""
    return df.rename({c: f"{c}{suffix}" for c in df.columns if c != "listing_id"})


def nest(boxes: pl.DataFrame, tol: float = 0.015) -> pl.DataFrame:
    """(big_box_id, small_box_id, known_at) for each small box inside a larger-scale box: it starts
    inside P's life, becomes known before P ended, and its range stays within P's bands."""
    b = _est(boxes).select("listing_id", "box_id", pl.col("scale").replace_strict(SCALE_RANK).alias("rank"), "start",
                           "known_at", "ended_at", "top", "bottom", _superseded(boxes))
    big = b.filter(pl.col("rank") > 0)
    small = _suffixed(b.drop("ended_at"), "_c")
    # "known before P ended" means before we learned that it ended (ended_at), not before its break bar
    pairs = big.join(small, on="listing_id").filter(
        (pl.col("rank_c") < pl.col("rank"))
        & (pl.col("start_c") >= pl.col("start"))
        & (pl.col("ended_at").is_null() | (pl.col("known_at_c") <= pl.col("ended_at")))
        & (pl.col("top_c") <= pl.col("top") * (1 + tol)) & (pl.col("bottom_c") >= pl.col("bottom") * (1 - tol)))
    # every enclosing scale is kept: picking "the nearest" here would change what is known at t once a
    # middle box that was confirmed later shows up (spec §5.8); consumers choose among those known by t
    # a pair stands until either box is redrawn (a version of a box is superseded by the next one)
    pairs = pairs.filter((pl.col("superseded_at").is_null() | (pl.col("known_at_c") < pl.col("superseded_at")))
                         & (pl.col("superseded_at_c").is_null() | (pl.col("known_at") < pl.col("superseded_at_c"))))
    return pairs.select("listing_id", pl.col("box_id").alias("big_box_id"), pl.col("box_id_c").alias("small_box_id"),
                        pl.max_horizontal("known_at", "known_at_c").alias("known_at"),
                        pl.min_horizontal("superseded_at", "superseded_at_c").alias("superseded_at")
                        ).sort("listing_id", "known_at", "big_box_id", "small_box_id")


def gold(boxes: pl.DataFrame, bars: pl.DataFrame, tol: float = 0.015, pairs: pl.DataFrame | None = None, small: str = "lower",
         near: float = 0.06, soon: int = 5, events: pl.DataFrame | None = None, floor: float = 0.35) -> pl.DataFrame:
    """`bars`: the listings' bars (listing_id, date), to count bars after a break. `pairs`: the NEST table when already
    computed. small (H5): "lower" — any box of a lower scale can be P's 金品; "short" — only a small box or a slant box
    (a small consolidation). near, soon: 天品's small box sits at the old top — its bottom at most `near` above it — and starts
    at most `soon` bars after the break (a low-volume pullback after the break, held at the old top). `events`: the boxes'
    events; with them every break_up counts, also one the box later came back from (a pullback a little under the top).
    floor: 上品 is a small range pressed under the top, inside the big box — a flat one's bottom at least this
    share of the big box's height up, or within two of the edge's bands (2 × tol) under the top; only 变异上品 rises to the
    top from the lower middle. A flat small box reaching lower is the big box's own last swing, no 金品 (2026-10-11: half of
    what had been called 上品 sat under 39% of the big box; the 25 上品 of the reference cases sit at 29%–84%, median 59%, all kept by
    0.35 with the bands; its signals did as well as the reference reading)."""
    rising = (pl.col("slope").fill_null(0) > 0) if "slope" in boxes.columns else pl.lit(False)
    b = _est(boxes).with_columns(pl.col("scale").replace_strict(SCALE_RANK).alias("rank"), rising.alias("rising"),
                                 _superseded(boxes), _lineage(boxes))
    big = b.select(pl.col("box_id").alias("big_box_id"), pl.col("top").alias("big_top"), pl.col("bottom").alias("big_bottom"))
    smalls = b.filter(pl.col("rank") == 0) if small == "short" else b
    small_t = smalls.select(pl.col("box_id").alias("small_box_id"), pl.col("top").alias("small_top"),
                            pl.col("bottom").alias("small_bottom"), pl.col("rising").alias("variant"), pl.col("known_at").alias("_ck"),
                            pl.col("start").alias("_cs"))
    pairs = nest(boxes, tol) if pairs is None else pairs
    if "superseded_at" not in pairs.columns:
        pairs = pairs.with_columns(pl.lit(None, pl.Date).alias("superseded_at"))
    inside = pairs.rename({"superseded_at": "_ps"}).join(big, on="big_box_id").join(small_t, on="small_box_id")
    # P's last small box (末端): each is replaced when the next one in P becomes known, or when either is redrawn
    inside = inside.sort("big_box_id", "_ck", "_cs", "small_box_id").with_columns(   # the id settles a tie, every time
        pl.min_horizontal(pl.col("known_at").shift(-1).over("big_box_id"), "_ps").alias("superseded_at"))
    up = ((pl.col("big_top") - pl.col("small_top")) / pl.col("big_top")).abs() <= tol
    # pressed under the top: a flat small box in the big box's upper part, or within two of its bands under the top (a
    # narrow big box — one case's is 2.3% tall — has no upper half a small box fits in)
    pressed = (pl.col("variant") | (pl.col("small_bottom") - pl.col("big_bottom") >= floor * (pl.col("big_top") - pl.col("big_bottom")))
               | (pl.col("small_bottom") >= pl.col("big_top") * (1 - 2 * tol)))
    inside = inside.filter(~up | pressed).with_columns(pl.when(up).then(pl.lit("上品")).otherwise(pl.lit("下品")).alias("kind"),
                                 pl.when(up).then(None).otherwise((pl.col("big_top") - pl.col("small_top")) / pl.col("small_top"))
                                 .alias("inner_distance"), pl.lit(None, pl.Float64).alias("above_top"))
    # 天品: the first small box confirmed after P broke up (by confirmation, not start: a box that started
    # earlier but was confirmed later must not change what was already known), if it sits at P's top and began at the
    # break. The breaks: P's end as last drawn (every version carries it once known) and, with the events, each break_up
    idx = bars.select("listing_id", "date").sort("listing_id", "date").with_columns(pl.int_range(pl.len()).over("listing_id").alias("_i"))
    breaks = b.filter((pl.col("end_reason") == "上破离箱") & pl.col("superseded_at").is_null()).select("box_id", pl.col("end").alias("brk"))
    if events is not None:
        breaks = pl.concat([breaks, events.filter(pl.col("event") == "break_up").select("box_id", pl.col("date").alias("brk"))]).unique()
    p = (b.filter(pl.col("rank") > 0).select("listing_id", "box_id", "lineage", "rank", "top", "bottom").join(breaks, on="box_id")
         .join(idx.rename({"date": "brk", "_i": "brk_i"}), on=["listing_id", "brk"], how="left"))
    lin_known = pl.col("known_at").min().over("lineage").alias("lineage_known")   # a box confirmed before the break is 上品's
    cands = (p.join(_suffixed(smalls.with_columns(lin_known).select("listing_id", "box_id", "rank", "start", "known_at", "top", "bottom",
                                                                    "rising", "lineage", "lineage_known", "superseded_at"), "_c"), on="listing_id")
             .join(idx.rename({"date": "start_c", "_i": "start_i_c"}), on=["listing_id", "start_c"], how="left")
             .filter((pl.col("rank_c") < pl.col("rank")) & (pl.col("lineage_known_c") >= pl.col("brk"))
                     & (pl.col("start_i_c") >= pl.col("brk_i") - SKY_EARLY)))
    # the first small box drawn after the break, every version of it
    firsts = (cands.sort("box_id", "brk", "known_at_c", "start_c", "box_id_c").unique(["box_id", "brk"], keep="first", maintain_order=True)
              .select("box_id", "brk", pl.col("lineage_c").alias("_first")))
    first = (cands.join(firsts, on=["box_id", "brk"]).filter(pl.col("lineage_c") == pl.col("_first"))
             .filter(pl.col("bottom_c").is_between(pl.col("top") * (1 - tol), pl.col("top") * (1 + near))
                     & (pl.col("start_i_c") - pl.col("brk_i") <= soon))
             .sort("brk").unique(["lineage", "box_id_c"], keep="first", maintain_order=True))   # P's 天品 from its first break
    sky = first.select("listing_id", pl.lit("天品").alias("kind"), pl.col("rising_c").alias("variant"),
                       pl.col("box_id").alias("big_box_id"), pl.col("box_id_c").alias("small_box_id"),
                       pl.max_horizontal("brk", "known_at_c").alias("known_at"),
                       pl.col("top").alias("big_top"), pl.col("bottom").alias("big_bottom"),
                       pl.col("top_c").alias("small_top"), pl.col("bottom_c").alias("small_bottom"),
                       pl.lit(None, pl.Float64).alias("inner_distance"), (pl.col("bottom_c") / pl.col("top") - 1).alias("above_top"),
                       pl.col("superseded_at_c").alias("superseded_at"))
    # a small box that is P's 天品 (formed after its break) is not also its 上品 from inside, should P have come back
    sky_of = first.select(pl.col("lineage").alias("_pl"), pl.col("box_id_c").alias("small_box_id"))
    inside = (inside.join(b.select(pl.col("box_id").alias("big_box_id"), pl.col("lineage").alias("_pl")), on="big_box_id")
              .join(sky_of, on=["_pl", "small_box_id"], how="anti"))
    both = pl.concat([inside.select(*GOLD_SCHEMA, "_cs"), sky.with_columns(pl.col("known_at").alias("_cs")).select(*GOLD_SCHEMA, "_cs")],
                     how="vertical_relaxed")
    # P's 金品 is its last small box: the 天品 after the break replaces the last one inside it too
    both = both.sort("big_box_id", "known_at", "_cs", "small_box_id").with_columns(
        pl.min_horizontal("superseded_at", pl.col("known_at").shift(-1).over("big_box_id")).alias("superseded_at"))
    return both.select(list(GOLD_SCHEMA)).cast(GOLD_SCHEMA).sort("listing_id", "known_at", "big_box_id", "small_box_id")


def relations(boxes: pl.DataFrame, bars: pl.DataFrame, tol: float = 0.015) -> pl.DataFrame:
    """Each box B (as first drawn) and the box A before it at its level: the one starting last before B among those
    drawn by then, as A was drawn on that day — kind and, when A had ended by then, the 寻箱线 from A's break to B's
    start. Known when B is: a box drawn later never changes a pair already known. The bars are joined once by date."""
    est = _est(boxes).with_columns(_superseded(boxes), _lineage(boxes),
                                   _level())
    first = est.sort("known_at", "box_id").unique("lineage", keep="first", maintain_order=True)
    b = first.select("listing_id", "_level", pl.col("lineage").alias("lineage_b"), pl.col("box_id").alias("box_id_b"),
                     pl.col("start").alias("start_b"), pl.col("known_at").alias("known_at_b"), pl.col("top").alias("top_b"),
                     pl.col("bottom").alias("bottom_b"))
    a = est.select("listing_id", "_level", "lineage", "box_id", "scale", "start", "known_at", "superseded_at", "end", "ended_at",
                   "top", "bottom")
    pairs = (b.join(a, on=["listing_id", "_level"])
             .filter((pl.col("lineage") != pl.col("lineage_b")) & (pl.col("start") < pl.col("start_b"))
                     & (pl.col("known_at") <= pl.col("known_at_b"))
                     & (pl.col("superseded_at").is_null() | (pl.col("superseded_at") > pl.col("known_at_b"))))
             .sort("box_id_b", "start", "lineage").unique("box_id_b", keep="last", maintain_order=True)
             .with_columns(pl.when(pl.col("ended_at") <= pl.col("known_at_b")).then(pl.col("end")).alias("end")))
    if pairs.is_empty():
        return pl.DataFrame(schema=REL_SCHEMA)
    idx = bars.select("listing_id", "date", "close").with_columns(pl.int_range(pl.len()).over("listing_id").alias("i"))
    a_top, a_bot, b_top, b_bot = pl.col("top"), pl.col("bottom"), pl.col("top_b"), pl.col("bottom_b")
    kind = (pl.when(((b_bot - a_top).abs() <= tol * a_top) | ((b_top - a_bot).abs() <= tol * a_bot)).then(pl.lit("邻"))
            .when(((b_top <= a_top * (1 + tol)) & (b_bot >= a_bot * (1 - tol)))
                  | ((a_top <= b_top * (1 + tol)) & (a_bot >= b_bot * (1 - tol)))).then(pl.lit("中"))
            .when((b_bot < a_top) & (b_top > a_bot)).then(pl.lit("交"))
            .otherwise(pl.lit("隔")))
    out = (pairs.join(idx.rename({"date": "end", "i": "i0", "close": "c0"}), on=["listing_id", "end"], how="left")
           .join(idx.rename({"date": "start_b", "i": "i1", "close": "c1"}), on=["listing_id", "start_b"], how="left")
           .with_columns(kind.alias("kind"), (pl.col("c1") / pl.col("c0") - 1).alias("seek_change"),
                         (pl.col("i1") - pl.col("i0")).alias("seek_bars")))
    return out.select("listing_id", "scale", "kind", pl.col("box_id").alias("from_box_id"),
                      pl.col("box_id_b").alias("to_box_id"), pl.col("lineage").alias("from_lineage"),
                      pl.col("lineage_b").alias("to_lineage"), pl.col("known_at_b").alias("known_at"),
                      pl.when(pl.col("seek_change") > 0).then(pl.lit("up")).when(pl.col("seek_change").is_not_null())
                      .then(pl.lit("down")).alias("seek_direction"), "seek_change", "seek_bars"
                      ).cast(REL_SCHEMA).sort("listing_id", "scale", "known_at", "from_box_id", "to_box_id")


REL_SCHEMA = {"listing_id": pl.Utf8, "scale": pl.Utf8, "kind": pl.Utf8, "from_box_id": pl.Utf8, "to_box_id": pl.Utf8,
              "from_lineage": pl.Utf8, "to_lineage": pl.Utf8, "known_at": pl.Date, "seek_direction": pl.Utf8, "seek_change": pl.Float64, "seek_bars": pl.Int64}


FB_SCHEMA = {"listing_id": pl.Utf8, "box_id": pl.Utf8, "source": pl.Utf8, "gold_box_id": pl.Utf8, "date": pl.Date,
             "price": pl.Float64, "previous_low": pl.Float64, "known_at": pl.Date, "end": pl.Date, "ended_at": pl.Date,
             "end_reason": pl.Utf8}
_END_ORDER = {"失守": 0, "新箱": 1, "到期": 2}   # two ends on one day: the loss is what happened


def false_bottoms(boxes: pl.DataFrame, touches: pl.DataFrame, events: pl.DataFrame, gold: pl.DataFrame, bars: pl.DataFrame,
                  starts: pl.DataFrame | None = None, tol: float = 0.015, horizon: int = 60) -> pl.DataFrame:
    """假底 (design 2026-10-06 §5). For a mid or long box P, on its first
    upward break that has one (a later break counts when the first had none, e.g. after a 回箱) — at most one
    per box, so a rule's stage knows it by its box:

      金品  P has a current 上品 / 下品 (变异 too) whose small box sits above P's bottom band: its bottom
            (the 金品 is the 假底);
      C 点  else the lowest low between P's last top touch and the break bar, if it is above P's bottom band
            and above P's last bottom touch before that top touch (the last bottom has to be higher than the one
            before; a pullback that reaches the bottom leaves none).

    Only P's own touches count (no two-bar fractals: a small wiggle is not a bottom), all known on the break bar,
    which is when the 假底 is known (before the break the price first found support at C). Its line runs from C (or the
    金品's start) to `end`: the first close under it by more than tol (失守), a box of P's scale that starts on
    or after the break becoming known (新箱), or `horizon` bars after the break (到期) — whichever comes first,
    each known on its own bar (`ended_at` = `end`, as a box's). `starts`: box_id → start of every box a 金品's
    small box can be (flat and slant)."""
    est = boxes.filter(pl.col("status") == "established").with_columns(_lineage(boxes))
    start_of = dict(zip(*(est if starts is None else starts).select("box_id", "start").get_columns(), strict=True))
    line = dict(zip(*est.select("box_id", "lineage").get_columns(), strict=True))
    brk: dict[str, list] = defaultdict(list)
    for bid, d in events.filter(pl.col("event") == "break_up").select("box_id", "date").sort("date").iter_rows():
        brk[bid].append(d)
    tch: dict[str, list] = defaultdict(list)          # box (all its versions) -> [(date, side, price, known_at)] in date order
    for bid, *rest in touches.select("box_id", "date", "side", "price", "known_at").sort("date").iter_rows():
        tch[line.get(bid, bid)].append(tuple(rest))
    golds: dict[str, list] = defaultdict(list)        # big box -> [(known_at, small box, small bottom, superseded_at)]
    for big, *rest in (gold.filter(pl.col("kind").is_in(["上品", "下品"]))
                       .select("big_box_id", "known_at", "small_box_id", "small_bottom", "superseded_at")
                       .sort("known_at", "small_box_id").iter_rows()):
        golds[big].append(tuple(rest))
    same = {}                                          # (listing, level) -> box starts, and the earliest known_at from each on
    firsts = est.sort("known_at", "box_id").unique("lineage", keep="first", maintain_order=True).with_columns(_level())
    for (lid, sc), g in firsts.sort("start").group_by("listing_id", "_level", maintain_order=True):
        k = g["known_at"].to_list()
        for i in range(len(k) - 2, -1, -1):
            k[i] = min(k[i], k[i + 1])
        same[(lid, sc)] = (g["start"].to_list(), k)
    big = defaultdict(list)
    for p in est.filter(pl.col("scale").is_in(["mid", "long"])).iter_rows(named=True):
        big[p["listing_id"]].append(p)
    rows = []
    for (lid,), bb in bars.sort("listing_id", "date").group_by("listing_id", maintain_order=True):
        if lid not in big:
            continue
        dates = bb["date"].to_list()
        low = bb["low"].cast(pl.Float64).fill_null(np.nan).to_numpy()
        close = bb["close"].cast(pl.Float64).fill_null(np.nan).to_numpy()
        done = set()                                   # one 假底 per box, whichever version broke
        for p in sorted(big[lid], key=lambda x: (x["known_at"], x["box_id"])):
            if p["lineage"] in done:
                continue
            for bd in brk.get(p["box_id"], []):
                t = bisect_left(dates, bd)
                one = _false_bottom(p, t, dates, low, tch.get(p["lineage"], []), golds.get(p["box_id"], []), start_of, tol)
                if one is not None:
                    done.add(p["lineage"])
                    lvl = "inner" if ":inner:" in p["box_id"] else "big"   # a new box of P's own level
                    end, why = _fb_end(one["price"], t, dates, close, same.get((lid, lvl)), tol, horizon)
                    rows.append({"listing_id": lid, "box_id": p["box_id"], **one, "known_at": bd, "end": end, "ended_at": end,
                                 "end_reason": why})
                    break
    return pl.DataFrame(rows, schema=FB_SCHEMA).sort("listing_id", "known_at", "box_id")


def _false_bottom(p, t, dates, low, touches, golds, start_of, tol, kd=None):
    """P's 假底 on a break on bar t, from what is known on `kd` — the break bar's date; the close before for a break as
    if it came on bar t (false_bottoms_if): the lows before bar t, the touches and 金品 known by then."""
    kd = dates[t] if kd is None else kd
    floor = p["bottom"] * (1 + tol)
    current = [g for g in golds if g[0] <= kd and (g[3] is None or g[3] > kd)]
    if current and current[-1][2] > floor:
        _, small, bottom, _ = current[-1]
        return {"source": "金品", "gold_box_id": small, "date": start_of.get(small), "price": bottom,
                "previous_low": _last_bottom(touches, kd, start_of.get(small))}
    tops = [x for x in touches if x[1] == "top" and x[3] <= kd and (t >= len(dates) or x[0] < dates[t])]
    if not tops:
        return None
    j = bisect_left(dates, tops[-1][0])
    if t - j < 2 or np.isnan(low[j + 1:t]).all():
        return None                              # no bar between P's last top touch and the break
    k = j + 1 + int(np.nanargmin(low[j + 1:t]))
    prev = _last_bottom(touches, kd, dates[j])
    if low[k] <= floor or (prev is not None and low[k] <= prev):
        return None
    return {"source": "C 点", "gold_box_id": None, "date": dates[k], "price": float(low[k]), "previous_low": prev}


class FalseBottomsIf:
    """The 假底 a box standing at a day's close would have if it broke on its next bar (design 2026-10-11 §6: a cross
    buys during that day, before any break is known) — false_bottoms()' method on what is known at that close: the lows
    up to it, the touches and 金品 known by then. The tables are read once: a check-up asks for every variant's 潜伏
    rows on the same chunk."""

    def __init__(self, boxes: pl.DataFrame, touches: pl.DataFrame, gold: pl.DataFrame, bars: pl.DataFrame,
                 starts: pl.DataFrame | None = None, tol: float = 0.015):
        est = boxes.filter(pl.col("status") == "established").with_columns(_lineage(boxes))
        self.start_of = dict(zip(*(est if starts is None else starts).select("box_id", "start").get_columns(), strict=True))
        self.big = {p["box_id"]: p for p in est.filter(pl.col("scale").is_in(["mid", "long"])).iter_rows(named=True)}
        line = dict(zip(*est.select("box_id", "lineage").get_columns(), strict=True))
        self.tch: dict[str, list] = defaultdict(list)
        for bid, *rest in touches.select("box_id", "date", "side", "price", "known_at").sort("date").iter_rows():
            self.tch[line.get(bid, bid)].append(tuple(rest))
        self.golds: dict[str, list] = defaultdict(list)
        for b, *rest in (gold.filter(pl.col("kind").is_in(["上品", "下品"]))
                         .select("big_box_id", "known_at", "small_box_id", "small_bottom", "superseded_at")
                         .sort("known_at", "small_box_id").iter_rows()):
            self.golds[b].append(tuple(rest))
        self.bars, self.lows, self.tol = bars, {}, tol

    def _low(self, lid: str):
        if lid not in self.lows:
            bb = self.bars.filter(pl.col("listing_id") == lid).sort("date")
            self.lows[lid] = (bb["date"].to_list(), bb["low"].cast(pl.Float64).fill_null(np.nan).to_numpy())
        return self.lows[lid]

    def __call__(self, evenings: pl.DataFrame) -> pl.DataFrame:
        """`evenings`: listing_id, box_id, date. Adds false_bottom (null: none, a small box's, a box not found) and source
        (金品 / C 点)."""
        price, source = [], []
        for lid, bid, d in evenings.select("listing_id", "box_id", "date").iter_rows():
            p = self.big.get(bid)
            dates, low = self._low(lid) if p is not None else ([], None)
            t = bisect_left(dates, d) + 1   # the next bar: the break, as if
            one = (_false_bottom(p, t, dates, low, self.tch.get(p["lineage"], []), self.golds.get(bid, []), self.start_of,
                                 self.tol, kd=d) if p is not None and t <= len(dates) and dates[t - 1] == d else None)
            price.append(one["price"] if one else None)
            source.append(one["source"] if one else None)
        return evenings.with_columns(pl.Series("false_bottom", price, dtype=pl.Float64), pl.Series("source", source, dtype=pl.Utf8))


def false_bottoms_if(evenings: pl.DataFrame, boxes: pl.DataFrame, touches: pl.DataFrame, gold: pl.DataFrame,
                     bars: pl.DataFrame, starts: pl.DataFrame | None = None, tol: float = 0.015) -> pl.DataFrame:
    """FalseBottomsIf on one set of evenings."""
    return FalseBottomsIf(boxes, touches, gold, bars, starts, tol)(evenings)


def _last_bottom(touches, bd, before):
    """P's last bottom touch dated before `before`, as known on the break bar."""
    if before is None:
        return None
    b = [x for x in touches if x[1] == "bottom" and x[3] <= bd and x[0] < before]
    return b[-1][2] if b else None


def _fb_end(price, t, dates, close, same, tol, horizon):
    ends = []
    stop = min(len(dates), t + horizon + 1)
    lost = np.nonzero(close[t + 1:stop] < price * (1 - tol))[0]
    if len(lost):
        ends.append((dates[t + 1 + lost[0]], _END_ORDER["失守"], "失守"))
    if same is not None:
        starts, first_known = same
        i = bisect_left(starts, dates[t])          # P's scale's boxes that start on or after the break
        if i < len(starts):
            ends.append((first_known[i], _END_ORDER["新箱"], "新箱"))
    if t + horizon < len(dates):
        ends.append((dates[t + horizon], _END_ORDER["到期"], "到期"))
    if not ends:
        return None, None
    d, _, why = min(ends)
    return d, why
