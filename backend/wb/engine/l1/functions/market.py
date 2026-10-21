"""Exchange facts as formula functions (spec §5.3 市场; §4.4 涨跌停).

CN uses the exchange's limit prices (stk_limit); JP uses J-Quants' UL/LL flags when the source has
them, otherwise the TSE price-limit table (test profile). Limit prices are raw exchange prices, so
they are compared with RAW_* prices. A flag or price the source did not give is unknown."""
import polars as pl

from wb.engine.l1.spec import W, register
from wb.engine.l1.units import U

EPS = 1e-6


def _up_touch(ctx):
    raw_h = ctx.field("RAW_H")
    by_price = raw_h >= ctx.column("up_limit") - EPS
    if ctx.market == "JP":
        return pl.coalesce(ctx.column("ul"), by_price)
    return by_price


def _up_close(ctx):
    raw_c = ctx.field("RAW_C")
    by_price = raw_c >= ctx.column("up_limit") - EPS
    if ctx.market == "JP":  # UL marks a touch; a close at the limit also closes at the high
        return pl.coalesce(ctx.column("ul") & (raw_c >= ctx.field("RAW_H") - EPS), by_price)
    return by_price


def _down_touch(ctx):
    by_price = ctx.field("RAW_L") <= ctx.column("down_limit") + EPS
    return pl.coalesce(ctx.column("ll"), by_price) if ctx.market == "JP" else by_price


def _down_close(ctx):
    raw_c = ctx.field("RAW_C")
    by_price = raw_c <= ctx.column("down_limit") + EPS
    if ctx.market == "JP":
        return pl.coalesce(ctx.column("ll") & (raw_c <= ctx.field("RAW_L") + EPS), by_price)
    return by_price


register("LIMIT_UP_TOUCH", [], U.BOOL, "盘中触及涨停（A 股：最高价 = 涨停价；日股：UL 标志）", "市场", scope="el")(_up_touch)
register("LIMIT_UP_CLOSE", [], U.BOOL, "收盘封涨停（A 股：收盘价 = 涨停价；日股：UL 且收在最高价）", "市场", scope="el")(_up_close)
register("LIMIT_DOWN_TOUCH", [], U.BOOL, "盘中触及跌停", "市场", scope="el")(_down_touch)
register("LIMIT_DOWN_CLOSE", [], U.BOOL, "收盘封跌停", "市场", scope="el")(_down_close)


@register("LIMIT_UP_STREAK", [], U.COUNT, "到这一根为止连续收盘涨停的天数（当天没封住为 0；连板中间有一天不知道是否涨停，就不知道）", "市场")
def _streak(ctx):
    flag = _up_close(ctx).cast(pl.Int32)
    idx = pl.int_range(pl.len())
    since_unknown = pl.when(flag.is_null()).then(idx).forward_fill()
    since_zero = pl.when(flag == 0).then(idx).forward_fill().fill_null(-1)
    # an unknown day inside the current run makes the count unknown; a known 0 starts it again
    broken = flag.is_null() | (since_unknown.fill_null(-2) > since_zero)
    return pl.when(broken).then(None).otherwise(_run_length(flag.fill_null(0)))


def _run_length(flag: pl.Expr) -> pl.Expr:
    """Length of the current run of 1s ending at each bar (0 where flag is 0)."""
    idx = pl.int_range(pl.len())
    last_zero = pl.when(flag == 0).then(idx).forward_fill().fill_null(-1)
    return pl.when(flag == 1).then(idx - last_zero).otherwise(0)


register("INDEX_STATE", [], U.NUMBER,
         "对照指数所处状态（§7.3）：1 上升（收在 60、200 日线之上且 60 日线比 5 根前高），−1 下降（都在下面且 60 日线比 5 根前低），"
         "0 震荡；按指数自己的交易日算，200 日线还没有时未知", "市场环境", scope="el")(lambda ctx: ctx.column("idx_state"))


def _has(col: pl.Expr, name: str) -> pl.Expr:
    """A list column of names holds `name`, without case (words in formulas are read in lower case)."""
    return col.list.eval(pl.element().str.to_lowercase()).list.contains(name.lower())


register("IN_BOARD", [W("NAME", ())], U.BOOL, "在这个板块 / 市场区分（A 股：主板、创业板、科创板、北交所；日股：プライム、スタンダード、"
         "グロース），写名字里的一段就行", "股票池", scope="el")(
    lambda ctx, name: ctx.column("board").str.to_lowercase().str.contains(name.lower(), literal=True))
register("S_SHARE", [], U.BOOL, "未股改：名称以 S 开头、不是 ST（如 S佳通；按现在的名称；A 股才有）", "股票池", scope="el",
         markets=("CN",))(
    lambda ctx: ctx.column("s_share"))
register("IN_INDUSTRY", [W("NAME", ())], U.BOOL, "所在行业是这个（A 股申万一级，按当天的归属；日股 33 業種）", "股票池", scope="el")(
    lambda ctx, name: ctx.column("industry").str.to_lowercase() == name.lower())
register("TAGGED", [W("NAME", ())], U.BOOL, "你给这只股票打过这个题材标签（在 K 线页加）", "股票池", scope="el")(
    lambda ctx, name: _has(ctx.column("tags"), name))
register("IN_GROUP", [W("NAME", ())], U.BOOL, "在你的这个自选分组里", "股票池", scope="el")(
    lambda ctx, name: _has(ctx.column("watch_groups"), name))
register("IN_INDEX", [W("ID", ())], U.BOOL, "是这个指数的成分股（CN:IDX:000300 这样写；A 股按每月月末的成分表，日股 JP:IDX:TOPIX、"
         "TOPIX100、TOPIX500、CORE30；从成分表那天的下一天起算，最早一份之前未知）", "股票池", scope="el")(
    lambda ctx, iid: ctx.column(f"in:{iid.upper()}"))
