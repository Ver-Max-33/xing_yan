"""Cross-sectional functions (spec §5.3 截面): computed per date across the market (or industry)."""
import polars as pl

from wb.engine.l1.spec import B, N, S, W, register, same
from wb.engine.l1.units import U

SMALL = 3  # an industry of this few listings or fewer is listed apart, not ranked against the big ones (§7.3)


def _by(by: str) -> tuple[str, ...]:
    return ("date", "industry") if by == "industry" else ("date",)


@register("RANK_XS", [S("X"), W("BY", ("market", "industry"), "market")], U.RATIO,
          "当天在全市场（或同行业）里的百分位排名，0 最小、1 最大；X 未知的不参与", "截面", scope="xs", partition=_by)
def _rank(ctx, x, by):
    return _known_group(ctx, by, pl.when(x.count() > 1).then((x.rank("average") - 1) / (x.count() - 1)))


@register("ZSCORE_XS", [S("X"), W("BY", ("market", "industry"), "market")], U.NUMBER,
          "当天在全市场（或同行业）里的标准分 (X − 均值) / 标准差", "截面", scope="xs", partition=_by)
def _zscore(ctx, x, by):
    sd = x.std()
    return _known_group(ctx, by, pl.when(sd > 0).then((x - x.mean()) / sd))


def _known_group(ctx, by: str, value: pl.Expr) -> pl.Expr:
    """Listings without a known industry are not one industry: their rank is unknown."""
    return pl.when(ctx.column("industry").is_null()).then(None).otherwise(value) if by == "industry" else value


@register("IND_MEDIAN", [S("X")], same, "当天同行业（A 股申万一级、日股 33 業種）成分股 X 的中位数；成分 3 只或更少的行业单独看待"
          "（未知），行业不明的股票也是未知", "截面", scope="xs", partition=("date", "industry"))
def _ind_median(ctx, x):
    return _known_group(ctx, "industry", pl.when(x.count() > SMALL).then(x.median()))


@register("DENSE_RANK_XS", [S("X")], U.COUNT, "当天 X 从大到小的名次：1 最大，相同的值同名次，X 未知的不排", "截面", scope="xs")
def _dense_rank(ctx, x):
    return x.rank("dense", descending=True)


@register("XS_SHARE", [B("COND")], U.RATIO, "当天全市场满足条件的股票占比（条件未知的不算在内）", "截面", scope="xs")
def _share(ctx, cond):
    return cond.cast(pl.Float64).mean()


register("INDUSTRY_RANK", [N("N")], U.COUNT, "所在行业近 N 根涨幅（成分股中位数）在当天各行业里的名次，1 最强；成分 3 只或更少的行业"
         "不和大行业比（未知）（§7.3）", "市场环境", scope="xs",
         expand=lambda n: f"DENSE_RANK_XS(IND_MEDIAN(C / REF(C, {n}) - 1))")(lambda ctx, n: None)
register("BREADTH", [], U.RATIO, "当天全市场收盘上涨（高于前一根）的股票占比（§7.3 市场宽度）", "市场环境", scope="xs",
         expand=lambda: "XS_SHARE(C > REF(C, 1))")(lambda ctx: None)
