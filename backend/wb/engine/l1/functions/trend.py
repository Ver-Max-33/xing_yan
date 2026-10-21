"""Standard indicators (spec §5.3 "指标"), 通达信 parameters and formulas."""
import polars as pl

from wb.engine.l1.functions.base import EMA, HHV, LLV, MA, SMA, STD


def MACD(close: pl.Expr, short: int = 12, long: int = 26, mid: int = 9) -> dict[str, pl.Expr]:
    """DIF = EMA(C,12) - EMA(C,26); DEA = EMA(DIF,9); MACD = 2 * (DIF - DEA)."""
    dif = EMA(close, short) - EMA(close, long)
    dea = EMA(dif, mid)
    return {"dif": dif, "dea": dea, "macd": 2 * (dif - dea)}


def KDJ(close: pl.Expr, high: pl.Expr, low: pl.Expr, n: int = 9, m1: int = 3, m2: int = 3) -> dict[str, pl.Expr]:
    """RSV = (C - LLV(L,9)) / (HHV(H,9) - LLV(L,9)) * 100; K = SMA(RSV,3,1); D = SMA(K,3,1); J = 3K - 2D.
    A flat 9-bar window (HHV == LLV) gives RSV 0, as 通达信's division by zero does."""
    lo, hi = LLV(low, n), HHV(high, n)
    rsv = pl.when(hi > lo).then((close - lo) / (hi - lo) * 100).otherwise(pl.when(hi.is_not_null()).then(0.0))
    k = SMA(rsv, m1, 1)
    d = SMA(k, m2, 1)
    return {"k": k, "d": d, "j": 3 * k - 2 * d}


def BOLL(close: pl.Expr, n: int = 20, width: float = 2.0) -> dict[str, pl.Expr]:
    mid = MA(close, n)
    sd = STD(close, n)
    return {"boll_mid": mid, "boll_up": mid + width * sd, "boll_low": mid - width * sd}


def MAS(close: pl.Expr, periods=(5, 10, 20, 60)) -> dict[str, pl.Expr]:
    return {f"ma{p}": MA(close, p) for p in periods}


def VOL_MAS(volume: pl.Expr, periods=(5, 10)) -> dict[str, pl.Expr]:
    return {f"vol_ma{p}": MA(volume, p) for p in periods}


# ---- formula registrations (spec §5.3 指标) --------------------------------------------------
from wb.engine.l1.functions.base import REF  # noqa: E402
from wb.engine.l1.spec import F, N, register  # noqa: E402
from wb.engine.l1.units import U  # noqa: E402


@register("MACD", [N("SHORT", 12), N("LONG", 26), N("MID", 9)], U.PRICE,
          "MACD：DIF = EMA(C,短) − EMA(C,长)，DEA = EMA(DIF,中)，HIST = 2 × (DIF − DEA)；写 MACD.DIF 即默认参数",
          "指标", outputs=(("DIF", U.PRICE), ("DEA", U.PRICE), ("HIST", U.PRICE)), aliases={"MACD": "HIST", "BAR": "HIST"})
def _macd(ctx, short, long, mid):
    m = MACD(ctx.field("C"), short, long, mid)
    return {"DIF": m["dif"], "DEA": m["dea"], "HIST": m["macd"]}


@register("KDJ", [N("N", 9), N("M1", 3), N("M2", 3)], U.NUMBER,
          "KDJ（通达信）：RSV 用 N 根高低价，K = SMA(RSV,M1,1)，D = SMA(K,M2,1)，J = 3K − 2D", "指标",
          outputs=(("K", U.NUMBER), ("D", U.NUMBER), ("J", U.NUMBER)))
def _kdj(ctx, n, m1, m2):
    k = KDJ(ctx.field("C"), ctx.field("H"), ctx.field("L"), n, m1, m2)
    return {"K": k["k"], "D": k["d"], "J": k["j"]}


@register("BOLL", [N("N", 20), F("WIDTH", 2.0)], U.PRICE, "布林线：MID = MA(C,N)，UB/LB = MID ± 宽度 × STD(C,N)", "指标",
          outputs=(("MID", U.PRICE), ("UB", U.PRICE), ("LB", U.PRICE)), aliases={"BOLL": "MID", "UPPER": "UB", "LOWER": "LB"})
def _boll(ctx, n, width):
    b = BOLL(ctx.field("C"), n, width)
    return {"MID": b["boll_mid"], "UB": b["boll_up"], "LB": b["boll_low"]}


@register("RSI", [N("N", 6)], U.NUMBER, "RSI（通达信）：SMA(MAX(C−LC,0),N,1) / SMA(|C−LC|,N,1) × 100", "指标")
def _rsi(ctx, n):
    c = ctx.field("C")
    d = c - REF(c, 1)
    up = SMA(pl.when(d.is_not_null()).then(pl.max_horizontal(d, pl.lit(0.0))), n, 1)
    total = SMA(d.abs(), n, 1)
    return pl.when(total > 0).then(up / total * 100)


@register("ATR", [N("N", 14)], U.PRICE, "平均真实波幅：MA(MAX(H−L, |H−LC|, |L−LC|), N)", "指标")
def _atr(ctx, n):
    h, lo, lc = ctx.field("H"), ctx.field("L"), REF(ctx.field("C"), 1)
    tr = pl.when(lc.is_not_null()).then(pl.max_horizontal(h - lo, (h - lc).abs(), (lo - lc).abs()))
    return MA(tr, n)


@register("OBV", [], U.VOLUME, "能量潮：上涨日加成交量、下跌日减成交量的累计", "指标")
def _obv(ctx):
    c, v = ctx.field("C"), ctx.field("V")
    lc = REF(c, 1)
    step = pl.when(c > lc).then(v).when(c < lc).then(-v).otherwise(0.0)
    return step.cum_sum()
