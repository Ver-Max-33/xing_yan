"""The V1 primitives (spec §5.4) as registry entries. `bars` everywhere: listing_id, date, open, high,
low, close, volume (backward-adjusted), limit_up_close, and dif (MACD DIF) when divergences are
wanted; sorted by listing then date (see run.l2_bars)."""
from dataclasses import fields, replace

import numpy as np
import polars as pl

from wb.engine.l2.attrs import AttrParams, box_attrs
from wb.engine.l2.box import SCALES, Shared, detect
from wb.engine.l2.derived import false_bottoms, gold, nest, relations
from wb.engine.l2.more import macd_divergences, swings
from wb.engine.l2.pivots import pivots
from wb.engine.l2.slant import SlantParams, detect_slant
from wb.engine.l2.spec import BOX_EVOLVING, Param, Primitive, Table, register

ATTRS = {f.name for f in fields(AttrParams)}
SHARED = {f.name for f in fields(Shared)}
_SCALE_RANGES = {"min_len": (3, 160, 1), "max_len": (10, 320, 5), "min_top": (0, 5, 1), "min_bottom": (0, 5, 1),
                 "max_width": (0.02, 0.6, 0.01), "ret": (1, 20, 1), "dense": (0.1, 0.8, 0.05)}
_SCALE_DOCS = {"min_len": "最少几根", "max_len": "最多几根：小箱超过就结束（超长）；中箱、大箱是找箱时最多往回看几根，找到后一直到破箱才结束", "min_top": "上沿至少被碰到并守住几次",
               "min_bottom": "下沿至少被碰到并守住几次", "max_width": "箱宽最多多少（(上沿 − 下沿) ÷ 下沿）",
               "ret": "破箱后几根内回到箱里算回箱（R）",
               "dense": "箱沿画在成交密集区的边上：至少有成交最密那个价位多少比例的 K 线成交过的价位，才算在箱里"}
_SCALE_NAMES = {"short": "小箱", "mid": "中箱", "long": "大箱"}
_SHARED_DOCS = {"tol": ("收盘超出箱沿多少（比例）才算破箱（越线，不是贴线）", 0.005, 0.03, 0.005),
                "touch_tol": ("离箱沿多近算碰到（比例）：手画的线本身有一两个百分点宽", 0.0, 0.05, 0.005),
                "inside": ("收盘落在箱内的最低比例", 0.5, 0.95, 0.05),
                "gap": ("同一条沿的两次碰触最少相隔几根", 1, 10, 1),
                "spike_return": ("找大中箱时，当天连续收在箱外最多几根还算在箱里", 0, 6, 1),
                "alternate": ("反复受阻、反复受撑：上下沿至少轮流被碰到几次（先全在底、后全在顶的是一段上涨，不是箱）", 0, 6, 1),
                "rev": ("箱子还在长时，箱沿移动超过多少（比例）就重画一次（新的一版，从那天起才知道）", 0.002, 0.05, 0.002),
                "runin": ("大中箱的起点：价格在之前多少根内从另一边之外过来（一段涨或跌的尽头，极限点）", 5, 60, 5),
                "stray_run": ("大中箱里连续收在箱外最多几根（再多就不是这个箱）", 3, 40, 1),
                "stray_depth": ("大中箱里收盘离箱最远几个箱高（再远就不是这个箱）", 0.3, 3.0, 0.1),
                "rel": ("小箱最多是同时那个大箱宽度的几成（先画大箱，再在大箱里分小箱）", 0.2, 1.0, 0.05),
                "flat": ("平箱：箱内 5 日线起伏不超过箱高的比例", 0.1, 0.5, 0.05),
                "curve_k": ("曲转平：末端几根 5 日线走平", 4, 15, 1),
                "limit_hit": ("撞顶涨停：0 涨停收在箱顶带里；1 涨停收过箱顶也算（同时是破箱）", 0, 1, 1),
                "gapf": ("找成交密集区时，窄于价格这一比例的低谷跨过去（分钟线、周线、月线按一根 K 线的大小换算）", 0.0002, 0.05, 0.0002)}


def box_scales(params: dict | None) -> tuple[dict, Shared]:
    """Apply parameter overrides: shared names to every scale, `scale.name` to one scale."""
    params = params or {}
    scales = {name: replace(p, **{k.split(".", 1)[1]: v for k, v in params.items() if k.startswith(f"{name}.")})
              for name, p in SCALES.items()}
    return scales, replace(Shared(), **{k: v for k, v in params.items() if k in SHARED})


def pivot_table(bars: pl.DataFrame, method: str = "fractal", k: int = 2, threshold: float = 0.05,
                atr_n: int = 14, atr_m: float = 2.0) -> pl.DataFrame:
    rows = []
    for (lid,), g in bars.group_by("listing_id", maintain_order=True):
        dates = g["date"].to_list()
        h, lo, c = (g[x].cast(pl.Float64).fill_null(np.nan).to_numpy() for x in ("high", "low", "close"))
        idx, kind, price, known = pivots(h, lo, c, method, k, threshold, atr_n, atr_m)
        rows += [{"listing_id": lid, "idx": int(i), "date": dates[i], "kind": "high" if kd > 0 else "low",
                  "price": float(p), "known_at": dates[kn]} for i, kd, p, kn in zip(idx, kind, price, known, strict=True)]
    return pl.DataFrame(rows, schema={"listing_id": pl.Utf8, "idx": pl.Int64, "date": pl.Date, "kind": pl.Utf8,
                                      "price": pl.Float64, "known_at": pl.Date})


register(Primitive(
    name="PIVOTS", doc="已确认的高低点：分形（左右各 k 根）、按百分比回撤、或按 ATR 倍数回撤",
    params=(Param("method", "fractal", doc="fractal 分形 | pct 百分比回撤 | atr ATR 回撤", choices=("fractal", "pct", "atr")),
            Param("k", 2, 1, 5, 1, "分形两侧根数"),
            Param("threshold", 0.05, 0.02, 0.2, 0.01, "百分比回撤的阈值"),
            Param("atr_n", 14, 5, 30, 1, "ATR 根数"), Param("atr_m", 2.0, 0.5, 5.0, 0.5, "回撤达到几倍 ATR")),
    attributes={"date": "高低点所在日", "kind": "high / low", "price": "价格", "known_at": "确认日"},
    states=("confirmed",), events=(), known_at="分形：高低点之后第 k 根；回撤：从极值回撤达到阈值的那根",
    draw={"geometry": "point", "at": "date", "price": "price"},
    understanding={"pivots": "{date} 的{kind_name} {price:.2f}，{known_at} 确认"},
    tables={"pivots": Table()},
    compute=lambda bars, params=None, deps=None: {"pivots": pivot_table(bars, **(params or {}))}))


def _swings(bars, params=None, deps=None):
    p = {"min_change": 0.05, "direction": "both", "min_bars": 1, **(params or {})}
    s = swings(bars, threshold=p["min_change"]).filter(pl.col("bars") >= p["min_bars"])
    return {"swings": s if p["direction"] == "both" else s.filter(pl.col("direction") == p["direction"])}


register(Primitive(
    name="SWING", doc="相邻两个已确认高低点之间的一段上涨或下跌",
    params=(Param("min_change", 0.05, 0.02, 0.3, 0.01, "转折所需的回撤幅度（也是一段的最小涨跌幅）"),
            Param("direction", "both", doc="both | up 上涨段 | down 下跌段", choices=("both", "up", "down")),
            Param("min_bars", 1, 1, 60, 1, "一段至少几根")),
    attributes={"direction": "up / down", "start": "起点", "end": "终点", "change": "涨跌幅", "bars": "根数",
                "slope": "每根平均涨跌"},
    states=("confirmed",), events=(), known_at="终点高低点的确认日",
    draw={"geometry": "polyline", "points": [["start", "start_price"], ["end", "end_price"]]},
    understanding={"swings": "{start} 到 {end} {direction_name} {change:+.1%}，共 {bars} 根，{known_at} 确认"},
    tables={"swings": Table()},
    compute=_swings))


LEVELS = {"long": "大箱", "mid": "中箱", "short": "小箱"}


def _boxes(bars, params=None, deps=None):
    params = params or {}
    b, t, e = detect(bars, *box_scales(params))
    attrs = AttrParams(**{k: v for k, v in params.items() if k in ATTRS})
    # 层级: the 中箱 drawn inside a 大箱 apart from one standing alone (its own machine, ids …:inner:…)
    b = b.with_columns(pl.when(pl.col("box_id").str.contains(":inner:")).then(pl.lit("大箱里的中箱"))
                       .otherwise(pl.col("scale").replace_strict(LEVELS, default=None)).alias("level"))
    return {"boxes": box_attrs(b, bars, attrs), "touches": t, "events": e}


register(Primitive(
    name="BOX", doc="箱体：箱沿画在成交密集区的边上（多根 K 线都成交过的价位，只有一两根到过的价位不算：枭首斩尾）；先画大箱（一段涨跌的尽头之后的整段横盘，箱子长着时重画），再在大箱里分小箱",
    params=(*[Param(k, getattr(Shared(), k), lo, hi, step, doc) for k, (doc, lo, hi, step) in _SHARED_DOCS.items()],
            Param("lookback", 60, 20, 120, 10, "上横 / 下横：看起点前几根"),
            Param("year", 250, 120, 500, 10, "位置：一年低点的根数"),
            Param("mid_gain", 0.5, 0.2, 2.0, 0.1, "位置：距一年低点涨多少算中继"),
            Param("high_gain", 1.0, 0.5, 3.0, 0.1, "位置：距一年低点涨多少算高位"),
            Param("swing", 0.15, 0.03, 0.3, 0.01, "数波段用的回撤阈值"),
            Param("wave", 0.30, 0.1, 0.6, 0.05, "涨多少算一波"),
            Param("mid_waves", 2, 1, 5, 1, "进箱前走了几波算中继"),
            Param("high_waves", 3, 2, 6, 1, "进箱前走了几波算高位"),
            *[Param(f"{s}.{k}", getattr(p, k), *_SCALE_RANGES[k], f"{_SCALE_NAMES[s]}：{_SCALE_DOCS[k]}") for s, p in SCALES.items()
              for k in ("min_len", "max_len", "min_top", "min_bottom", "max_width", "ret", "dense")]),
    attributes={"scale": "级别 short 小箱 / mid 中箱（不到 60 根）/ long 大箱",
                "level": "层级：大箱 / 大箱里的中箱（大箱里更窄的一段横盘）/ 中箱（单独的）/ 小箱",
                "status": "established 成立", "start": "起点",
                "known_at": "成立日", "end": "结束（破箱那根；小箱也可能超长）", "end_reason": "上破离箱 / 下破离箱 / 超长（只有小箱）",
                "top": "上沿", "bottom": "下沿", "width": "箱宽 (上沿 − 下沿) ÷ 下沿", "length": "根数（结束后才有）",
                "touches_top": "上沿触点数", "touches_bottom": "下沿触点数",
                "inside_ratio": "收盘落箱比例", "excluded_before": "被枭首的箱前高低点", "shape": "平箱 / 曲箱",
                "ma5_range": "箱内 5 日线起伏 ÷ 箱高", "ma5_troughs": "5 日线波谷数", "context": "上横 / 下横",
                "position": "低位 / 中继 / 高位", "gain_1y": "距一年低点的涨幅", "waves": "进箱前走了几波",
                "lineage": "这个箱子（各版共用）", "superseded_at": "这一版被重画的那天"},
    states=("established", "pending", "ended"),
    events=("spike_up", "spike_down", "break_up", "break_down", "return", "leave_up", "leave_down",
            "limit_hit_top", "too_long", "touch_top", "touch_bottom", "curve_to_flat"),
    known_at="箱体（这一版）被画出来的那一根：每根都看一遍，箱子长着时边移动了就重画一版",
    draw={"geometry": "rect", "from": "start", "to": "end", "top": "top", "bottom": "bottom",
          "style": {"proto": "dashed", "established": "solid"}, "markers": {"touches": "circle", "events": "marker"}},
    understanding={"boxes": "{scale_name}箱{status_name}：{start} 起，上沿 {top:.2f}、下沿 {bottom:.2f}（宽 {width:.1%}），"
                            "上沿触碰 {touches_top} 次、下沿 {touches_bottom} 次，{known_at} {known_text}，{context}、{position}{end_text}"},
    tables={"boxes": BOX_EVOLVING, "touches": Table(), "events": Table()},
    compute=_boxes))

register(Primitive(
    name="SLANT_BOX", doc="斜箱：两条近似平行的斜线，通道压得紧；突破看当时的斜上沿",
    params=(Param("min_len", 10, 5, 40, 1, "最少根数"), Param("max_len", 60, 20, 160, 5, "最多根数"),
            Param("tol", 0.015, 0.005, 0.03, 0.005, "触点容差"), Param("gap", 3, 1, 10, 1, "两次触碰最少相隔几根"),
            Param("min_slope", 0.002, 0.0005, 0.01, 0.0005, "斜率至少每根多少（更平的算横箱）"),
            Param("slope_tol", 0.003, 0.0005, 0.01, 0.0005, "两条线斜率最多差多少"),
            Param("max_width", 0.15, 0.05, 0.3, 0.01, "通道最宽"), Param("inside", 0.8, 0.6, 0.95, 0.05, "收盘落在通道内的最低比例"),
            Param("spike_max", 2, 0, 5, 1, "允许连续越线几根"), Param("ret", 5, 1, 10, 1, "破箱后几根内回箱")),
    attributes={"top_slope": "上沿每根的斜率（价格比例）", "bottom_slope": "下沿斜率", "top": "成立时的上沿值",
                "bottom": "成立时的下沿值"},
    states=("established", "pending", "ended"), events=("break_up", "break_down", "return", "leave_up", "leave_down", "too_long"),
    known_at="斜箱成立的那一根",
    draw={"geometry": "polyline", "lines": [["top_a", "top_a_price", "top_b", "top_b_price"],
                                             ["bottom_a", "bottom_a_price", "bottom_b", "bottom_b_price"]]},
    understanding={"slant": "斜箱：{start} 起，上沿每根 {top_slope:+.2%}、下沿每根 {bottom_slope:+.2%}，{known_at} 成立{end_text}"},
    tables={"slant": Table(evolving={"end": "ended_at", "ended_at": "ended_at", "end_reason": "ended_at", "length": "ended_at"}),
            "events": Table()},
    compute=lambda bars, params=None, deps=None: dict(zip(("slant", "events"),
                                                          detect_slant(bars, SlantParams(**(params or {}))), strict=True))))


def _divergences(bars, params=None, deps=None):
    p = {"k": 2, "kind": "both", **(params or {})}
    d = macd_divergences(bars, pivot_table(bars, k=p["k"]))
    return {"divergences": d if p["kind"] == "both" else d.filter(pl.col("kind") == p["kind"])}


register(Primitive(
    name="MACD_DIV", doc="MACD 背离：两个已确认低点价格更低而 DIF 更高为底背离，高点反之（箱体内的背离不作箱体条件）",
    params=(Param("k", 2, 1, 5, 1, "找高低点的分形根数"),
            Param("kind", "both", doc="both | bull 底背离 | bear 顶背离", choices=("both", "bull", "bear"))),
    attributes={"kind": "bull / bear", "first": "第一个高低点", "second": "第二个", "price_change": "价格变化",
                "dif_change": "DIF 变化"},
    states=("confirmed",), events=(), known_at="第二个高低点的确认日",
    draw={"geometry": "polyline", "points": [["first", None], ["second", None]]},
    understanding={"divergences": "{second} {kind_name}：价格 {price_change:+.1%}，DIF 反向变化 {dif_change:+.3f}，{known_at} 确认"},
    tables={"divergences": Table()},
    compute=_divergences))


def _derived(bars, params, deps):
    params = params or {}
    tol = params.get("tol", 0.02)
    boxes, slant = deps["BOX"]["boxes"], deps["SLANT_BOX"]["slant"]
    both = pl.concat([boxes, slant], how="diagonal_relaxed") if slant.height else boxes
    pairs = nest(both, tol)
    g = gold(both, bars, tol, pairs, params.get("gold_small", "short"), params.get("sky_near", 0.06), params.get("sky_soon", 5),
             deps["BOX"]["events"], params.get("up_floor", 0.35))
    return {"nest": pairs, "gold": g, "relations": relations(boxes, bars, tol),
            "false_bottoms": false_bottoms(boxes, deps["BOX"]["touches"], deps["BOX"]["events"], g, bars,
                                           both.select("box_id", "start"), tol, params.get("fb_horizon", 60))}


register(Primitive(
    name="BOX_DERIVED", doc="由箱体推出的结构：箱中箱、金品（天品 / 上品 / 下品，斜箱为变异）、同级箱体关系（中、交、邻、隔）与寻箱线、假底",
    params=(Param("tol", 0.02, 0.005, 0.03, 0.005, "判定贴近上沿、相邻用的容差（手画的线本身有一两个百分点宽）"),
            Param("sky_near", 0.06, 0.01, 0.15, 0.01, "天品：小箱底最多高出原箱顶多少（突破后回踩到原箱顶附近、在那里横住；涨远了再横盘不算）"),
            Param("sky_soon", 5, 0, 20, 1, "天品：小箱最晚在突破后第几根开始（突破后马上回踩横住；隔久了不算）"),
            Param("up_floor", 0.35, 0.0, 0.9, 0.05, "上品：平的小箱的底至少在大箱的几成高处，或在箱顶下方两个容差以内（0 是大箱底、1 是大箱顶；"
                  "在大箱内部、紧贴上沿；参考案例里的 25 个上品在两成九到八成四；变异上品从中下部缓升到顶，不受这一条限制）"),
            Param("fb_horizon", 60, 20, 250, 5, "假底线最多画到向上破箱后几根"),
            Param("gold_small", "short", doc="金品的小箱：short 只算小箱和斜箱（小整理结构）；lower 比大箱低一级的都算（大箱里的中箱也算）", choices=("lower", "short"))),
    attributes={"kind": "金品类别 / 箱体关系", "variant": "变异：小箱是向上的斜箱（变异天品、上品、下品）",
                "big_top": "大箱上沿", "big_bottom": "大箱下沿", "small_top": "小箱上沿", "small_bottom": "小箱下沿",
                "inner_distance": "下品：(大箱顶 − 小箱顶) ÷ 小箱顶",
                "above_top": "天品：小箱底高出原箱顶多少", "seek_change": "寻箱线涨跌幅", "seek_bars": "寻箱线根数",
                "source": "假底从哪来：金品（小箱下沿）/ C 点（最后一次回落的低点）", "previous_low": "假底：它之前的那个底",
                "end_reason": "假底线结束：失守 / 新箱 / 到期"},
    states=("confirmed",), events=(), known_at="所依赖结构里最晚的那个",
    draw={"geometry": "marker", "at": "known_at", "label": "kind"},
    understanding={"nest": "箱中箱：{small_box_id} 在 {big_box_id} 里，{known_at} 起",
                   "gold": "{kind}{variant_text}：大箱 {big_box_id}，小箱 {small_box_id}，{known_at} 成立",
                   "relations": "{kind}：{from_box_id} → {to_box_id}，寻箱线{seek_direction_name} {seek_change:+.1%}、{seek_bars} 根",
                   "false_bottoms": "假底 {price:.2f}（{source}，{date} 起），{known_at} 向上破箱时成立{end_text}"},
    # a 金品 is its big box's last small box: `superseded_at`, when a later one in the big box is known, comes later
    tables={"nest": Table(evolving={"superseded_at": "superseded_at"}), "gold": Table(evolving={"superseded_at": "superseded_at"}),
            "relations": Table(),
            "false_bottoms": Table(evolving={"end": "ended_at", "ended_at": "ended_at", "end_reason": "ended_at"})},
    compute=_derived, requires=("BOX", "SLANT_BOX")))
