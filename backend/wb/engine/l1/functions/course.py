"""Box-method indicators (spec §5.3, §8.6).

Inputs are one security's rows sorted by date. Price-like inputs should be backward-adjusted
(hfq) so windows that span an ex-date compare like with like; `limit_pct` uses raw prices
because exchanges set limits on raw prices.
"""
import polars as pl

from wb.engine.l1.functions.base import HHV, LLV, MA, REF, above, rel


def AMPLITUDE(high: pl.Expr, low: pl.Expr, n: int) -> pl.Expr:
    """(highest high - lowest low) / lowest low over N bars. 40/60/120 = two/three/six months."""
    lo = LLV(low, n)
    return (HHV(high, n) - lo) / lo


def VOL_MULT(volume: pl.Expr) -> pl.Expr:
    """Today's volume / yesterday's (>= 2 倍量, <= 0.5 倍缩); unknown after a day without trades."""
    prev = REF(volume, 1)
    return pl.when(prev > 0).then(volume / prev)


def GROUND_VOL(volume: pl.Expr, n: int) -> pl.Expr:
    """地量: today's volume is the lowest of the last N bars (N is a parameter)."""
    return volume <= LLV(volume, n)


def SMALL_YANG_RUN(open_: pl.Expr, close: pl.Expr, prev_close: pl.Expr, k: int, max_pct: float) -> pl.Expr:
    """k consecutive small yang bars: close > open and day change <= max_pct (the cap is a parameter)."""
    small = ((close > open_) & ((close / prev_close - 1) <= max_pct)).cast(pl.Int32)
    return small.rolling_sum(window_size=k, min_samples=k) == k


def MA_DIST(close: pl.Expr, n: int) -> pl.Expr:
    return rel(close, MA(close, n))


def NEW_HIGH(close: pl.Expr, high: pl.Expr, n: int) -> pl.Expr:
    """Close above the highest high of the previous N bars (年内向左: N=120 半年, 250 一年); a close within EQ of it is not above."""
    return above(close, REF(HHV(high, n), 1))


def YEAR_LEFT(close: pl.Expr, high: pl.Expr, half: int = 120, year: int = 250) -> dict[str, pl.Expr]:
    """年内向左: the 笑脸 marks where the close first breaks the half-year high, the 箭头
    where it first breaks the one-year high. HALF / YEAR = bars since the latest such first break;
    GAP = on the 箭头, how many bars after the 笑脸 that started the same advance it came (0 = both
    on one bar), carried forward. The two arriving close together is what to watch for."""
    idx = pl.int_range(pl.len())
    nh_half, nh_year = NEW_HIGH(close, high, half), NEW_HIGH(close, high, year)
    smile = nh_half & ~nh_half.shift(1).fill_null(False)
    arrow = nh_year & ~nh_year.shift(1).fill_null(False)
    last_smile = pl.when(smile).then(idx).forward_fill()
    return {"half": idx - last_smile, "year": idx - pl.when(arrow).then(idx).forward_fill(),
            "gap": pl.when(arrow).then(idx - last_smile).forward_fill()}


def LIMIT_PCT_CN(up_limit: pl.Expr, pre_close: pl.Expr) -> pl.Expr:
    """One limit-up as a fraction of the reference price; follows stk_limit, nothing hard-coded."""
    return up_limit / pre_close - 1


# ---- formula registrations (spec §5.3 常用指标、形态特征) ---------------------------------------
from wb.engine.l1.functions.base import SUM  # noqa: E402
from wb.engine.l1.functions.market import _up_close  # noqa: E402
from wb.engine.l1.spec import F, N, W, Arg, register  # noqa: E402
from wb.engine.l1.units import U  # noqa: E402


@register("AMPLITUDE", [N("N")], U.RATIO, "N 根内最高价与最低价之差 ÷ 最低价（两个月、三个月、六个月振幅取 40、60、120）", "常用指标")
def _amplitude(ctx, n):
    return AMPLITUDE(ctx.field("H"), ctx.field("L"), n)


@register("BAR_LEN", [], U.RATIO, "K 线长度：(最高 − 最低) ÷ 昨收。箱里的平均写 MEAN(BAR_LEN, of=箱体阶段)，即箱内 K 线平均长度："
          "越短越平稳（K 线短小平稳说明有人控盘；A 股 2016–2026 年大箱突破，箱内 K 线短的之后表现好）", "形态特征")
def _bar_len(ctx):
    return (ctx.field("H") - ctx.field("L")) / REF(ctx.field("C"), 1)


@register("VOL_MULT", [], U.RATIO, "当天量 ÷ 昨天量（≥ 2 为倍量，≤ 0.5 为倍缩）", "常用指标")
def _vol_mult(ctx):
    return VOL_MULT(ctx.field("V"))


@register("GROUND_VOL", [N("N", 20)], U.BOOL, "地量：当天量是此前 N 根内最低（N 默认 20）", "常用指标")
def _ground(ctx, n):
    return GROUND_VOL(ctx.field("V"), n)


@register("SMALL_YANG_RUN", [N("K"), F("MAX_PCT", 0.03)], U.BOOL,
          "连续 K 根小阳线：收盘高于开盘且涨幅不超过上限（上限默认 3%）", "常用指标")
def _small_yang(ctx, k, max_pct):
    c = ctx.field("C")
    return SMALL_YANG_RUN(ctx.field("O"), c, REF(c, 1), k, max_pct)


@register("LIMIT_PCT", [], U.RATIO, "当天一个涨停的幅度（按交易所的涨停价 ÷ 昨收；日股按价格档位表）", "常用指标")
def _limit_pct(ctx):
    pre, up = ctx.column("pre_close"), ctx.column("up_limit")
    known = pre > 0
    if ctx.market == "CN":   # a day without limits (new listings' first days): Tushare's placeholder (1,000,000 / 99,999.99) — unknown (§4.4)
        known = known & (up <= 2 * pre)
    return pl.when(known).then(up / pre - 1)


@register("MA_DIST", [N("N")], U.RATIO, "C ÷ MA(C,N) − 1", "常用指标")
def _ma_dist(ctx, n):
    return MA_DIST(ctx.field("C"), n)


@register("NEW_HIGH", [N("N")], U.BOOL, "收盘价高于此前 N 根的最高价（N = 120 半年新高，250 一年新高）", "常用指标")
def _new_high(ctx, n):
    return NEW_HIGH(ctx.field("C"), ctx.field("H"), n)


@register("YEAR_LEFT", [], U.COUNT, "年内向左：HALF / YEAR = 距最近一次突破半年 / 一年高点（一轮上涨里的第一根）几根；GAP = 箭头距同一轮的笑脸几根，0 为同一根",
          "常用指标", outputs=(("HALF", U.COUNT), ("YEAR", U.COUNT), ("GAP", U.COUNT)))
def _year_left(ctx):
    y = YEAR_LEFT(ctx.field("C"), ctx.field("H"))  # the same definition the chart draws
    return {"HALF": y["half"], "YEAR": y["year"], "GAP": y["gap"]}


@register("MA_SLOPE_PCT", [N("N"), N("K", 5)], U.RATIO, "均线每根平均变化：(MA(C,N) ÷ K 根前的 MA − 1) ÷ K，判断 60 日线走平或上翘", "形态特征")
def _ma_slope(ctx, n, k):
    ma = MA(ctx.field("C"), n)
    return rel(ma, REF(ma, k)) / k


@register("RANGE_CONTRACT", [N("K")], U.RATIO, "最近 K 根的波幅 ÷ 再往前 K 根的波幅（< 1 为收缩，判断形末量尽）", "形态特征")
def _range_contract(ctx, k):
    rng = HHV(ctx.field("H"), k) - LLV(ctx.field("L"), k)
    prev = REF(rng, k)
    return pl.when(prev > 0).then(rng / prev)


@register("VOL_CONTRACT", [N("K")], U.RATIO, "最近 K 根的均量 ÷ 再往前 K 根的均量（< 1 为缩量）", "形态特征")
def _vol_contract(ctx, k):
    m = MA(ctx.field("V"), k)
    prev = REF(m, k)
    return pl.when(prev > 0).then(m / prev)


@register("RED_GREEN_SHARE", [N("N")], U.RATIO, "N 根里阳线（收盘高于开盘）的成交量占总量的比例，判断红肥绿瘦；只描述量柱，不推断资金流向", "形态特征")
def _red_green(ctx, n):
    v = ctx.field("V")
    red = pl.when(ctx.field("C") > ctx.field("O")).then(v).otherwise(pl.when(v.is_not_null()).then(0.0))
    total = SUM(v, n)
    return pl.when(total > 0).then(SUM(red, n) / total)


@register("VOL_RATIO", [N("N", 5)], U.RATIO, "量比：当天量 ÷ 此前 N 根的均量（不含当天）", "量价")
def _vol_ratio(ctx, n):
    v = ctx.field("V")
    base = MA(REF(v, 1), n)
    return pl.when(base > 0).then(v / base)


@register("MA_ALIGN", [Arg("PERIODS", "list"), W("DIR", ("up", "down"), "up")], U.BOOL,
          "均线排列：up = 短期均线依次在长期均线之上（多头排列），down 相反；两条均线相等（舍入误差以内）不算在上或在下", "量价")
def _ma_align(ctx, periods, direction):
    c = ctx.field("C")
    mas = [MA(c, int(p)) for p in periods]
    out = None
    for a, b in zip(mas, mas[1:], strict=False):
        part = above(a, b) if direction == "up" else above(b, a)   # a tie by rounding (a flat line) is neither
        out = part if out is None else out & part
    return out if out is not None else pl.lit(True)


@register("MA_SPREAD", [Arg("PERIODS", "list")], U.RATIO, "均线离散度：(最高的均线 − 最低的均线) ÷ 收盘价，越小越粘合", "量价")
def _ma_spread(ctx, periods):
    c = ctx.field("C")
    mas = [MA(c, int(p)) for p in periods]
    known = mas[0].is_not_null()
    for m in mas[1:]:
        known = known & m.is_not_null()
    return pl.when(known & (c > 0)).then((pl.max_horizontal(*mas) - pl.min_horizontal(*mas)) / c)


@register("REL_PERF", [N("N")], U.RATIO, "N 根内相对对照指数的强弱：(1 + 个股涨幅) ÷ (1 + 指数涨幅) − 1，> 0 为强于大盘", "形态特征")
def _rel_perf(ctx, n):
    c, idx = ctx.field("C"), ctx.column("idx_close")
    return rel(c / REF(c, n), idx / REF(idx, n))


@register("LIMIT_UP_COUNT", [N("N")], U.COUNT, "N 根内收盘涨停的次数", "形态特征")
def _limit_up_count(ctx, n):
    return _up_close(ctx).cast(pl.Int32).rolling_sum(window_size=n, min_samples=n)


@register("REL_PATTERN", [N("N"), F("FLAT", 0.01), F("MULT", 2.0)], U.COUNT,
          "与对照指数在同一段 N 根里比较，按七种强于大盘情形返回 1–7，都不是则 0：1 大盘跌个股涨；2 大盘跌个股横；"
          "3 都跌但个股跌得少；4 大盘创新低个股不创新低；5 大盘小涨个股大涨；6 大盘到前高个股已创新高；7 大盘横个股涨。"
          "“横”= 涨跌幅在 ±FLAT 以内，“大涨”= 个股涨幅至少是指数的 MULT 倍；这两个数是参数", "形态特征")
def _rel_pattern(ctx, n, flat, mult):
    c, idx = ctx.field("C"), ctx.column("idx_close")
    rs, ri = rel(c, REF(c, n)), rel(idx, REF(idx, n))
    i_down, s_up = ri < -flat, rs > flat
    return (pl.when(rs.is_null() | ri.is_null()).then(None)
            .when(i_down & s_up).then(1)
            .when(i_down & (rs.abs() <= flat)).then(2)
            .when(i_down & (rs < -flat) & (rs > ri)).then(3)
            .when((idx < REF(LLV(idx, n), 1)) & (c > REF(LLV(c, n), 1))).then(4)  # strictly new low
            .when((ri > flat) & (rs >= ri * mult)).then(5)
            # 前高 = the high of the N bars before this window: the index is back near it, the stock already beyond
            .when(((idx / REF(HHV(idx, n), n) - 1).abs() <= flat) & (c > REF(HHV(c, n), n) * (1 + flat))).then(6)
            .when((ri.abs() <= flat) & s_up).then(7)
            .otherwise(0))
