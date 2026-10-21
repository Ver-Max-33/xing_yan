"""Dataset catalog: the standard tables every provider must produce (spec §3.5 mechanism 2, §4.4).

Units are fixed here and nowhere else:
  prices      raw (unadjusted) local currency; `adj_factor` is the cumulative backward-adjust
              factor, so hfq_price = price * adj_factor
  volume      shares (raw); `share_factor` is the cumulative split/bonus-share factor,
              so hfq_volume = volume / share_factor
  amount      local currency (yuan / yen)
              CN volume and amount include the STAR / ChiNext after-hours fixed-price trades (盘后固定价格
              交易) as the exchange counts them: the P0 exchange crosscheck matched 230/230 rows
  market cap  local currency; share counts in shares
Missing data is null, never 0 (spec §1.5 principle 5).
"""
from dataclasses import dataclass

import polars as pl

SCHEMA_VERSION = 1

S, F, D, B = pl.Utf8, pl.Float64, pl.Date, pl.Boolean


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Dataset:
    name: str
    key: tuple[str, ...]
    columns: dict[str, pl.DataType]
    partition: str | None  # "year" -> one file per calendar year of `date`; None -> single file


DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in [
        Dataset("bars_1d", ("listing_id", "date"), {
            "listing_id": S, "date": D,
            "open": F, "high": F, "low": F, "close": F,
            "pre_close": F,                   # CN: exchange reference price (ex-rights adjusted on ex-dates)
            "volume": F, "amount": F,
            "adj_factor": F, "share_factor": F,
            "up_limit": F, "down_limit": F,   # CN: stk_limit prices
            "ul": B, "ll": B,                 # JP: J-Quants UL/LL (touched limit during the day)
            "suspended": B,                   # True = no trading that day; null = unknown
            "source": S,
        }, "year"),
        Dataset("instruments", ("listing_id",), {
            "listing_id": S, "market": S, "code": S, "exchange": S,
            "name": S, "name_en": S, "board": S, "industry": S,
            "list_date": D, "delist_date": D,
            "status": S,                      # L listed, D delisted, P paused, U unknown
            "source": S,
        }, None),
        Dataset("status_intervals", ("listing_id", "kind", "start"), {
            "listing_id": S, "kind": S, "value": S, "start": D, "end": D, "source": S,
        }, None),
        Dataset("calendar", ("date",), {"date": D, "is_open": B, "source": S}, None),
        Dataset("index_bars_1d", ("index_id", "date"), {
            "index_id": S, "date": D,
            "open": F, "high": F, "low": F, "close": F, "volume": F, "amount": F,
            "source": S,
        }, None),
        Dataset("valuation", ("listing_id", "date"), {
            "listing_id": S, "date": D,
            "pe": F, "pe_ttm": F, "pb": F, "ps_ttm": F, "dv_ttm": F, "turnover_rate": F,
            "total_share": F, "float_share": F, "total_mv": F, "circ_mv": F,
            "eps": F, "bps": F,
            "source": S,
        }, "year"),
        Dataset("industry", ("listing_id", "scheme", "in_date"), {
            "listing_id": S, "scheme": S, "code": S, "name": S,
            "in_date": D, "out_date": D, "source": S,
            # 申万二级 of the same membership spell (design 2026-10-12 §10): the 市场 page's; rules read `name` (一级)
            "l2_code": S, "l2_name": S,
        }, None),
        Dataset("index_members", ("index_id", "date", "listing_id"), {
            "index_id": S, "date": D,         # the list's date (CN: month-end weights; JP: TOPIX's monthly list)
            "listing_id": S, "weight": F,     # weight in the index, %
            "segment": S,                     # JP: TOPIX's size class (TOPIX Core30, Large70, Mid400, Small 1 / 2)
            "source": S,
        }, None),
        Dataset("corp_actions", ("listing_id", "ex_date", "kind"), {
            "listing_id": S, "ex_date": D,
            "kind": S,                        # split (JP) | dividend (CN, may include bonus shares)
            "ratio": F,                       # share multiplier on ex_date (2.0 = 1:2 split, 1.3 = 10送3)
            "cash_div": F,                    # cash per share, local currency (informational)
            "source": S,
        }, None),
        # 财务 PIT (spec §4.7): one row an announcement of a period's report; figures are cumulative from the start of the
        # fiscal year, as both sources report them; a correction or restatement keeps its own announcement day
        Dataset("financials", ("listing_id", "period_end", "ann_date"), {
            "listing_id": S, "period_end": D,
            "period_type": S,                 # Q1 | H1 | Q3 | FY (JP's other periods as they come)
            "ann_date": D,                    # the day it was announced (CN 公告日, JP DiscDate)
            "ann_time": S,                    # JP DiscTime (HH:MM:SS); CN gives none
            "known_at": D,                    # the first trading day whose close may read it (set when published)
            "revenue": F, "op_profit": F,     # local currency
            "net_profit": F,                  # attributable to the parent
            "eps": F, "total_assets": F,
            "equity": F,                      # attributable to the parent
            "cfo": F,                         # operating cash flow
            "roe": F,                         # %
            "revenue_yoy": F, "profit_yoy": F,  # % against the same period a year before (CN)
            "doc": S,                         # JP DocType; CN 合并报表
            "source": S,
        }, None),
        # 资金流向 (design 2026-10-15 §5.1): Tushare moneyflow — each trade counted on both sides by order size (小单 < 5 万,
        # 中单 5–20 万, 大单 20–100 万, 特大单 ≥ 100 万); net_mf by the side that started the trade. Every listing, 北交所 too
        Dataset("moneyflow", ("listing_id", "date"), {
            "listing_id": S, "date": D,
            "buy_sm": F, "sell_sm": F, "buy_md": F, "sell_md": F, "buy_lg": F, "sell_lg": F, "buy_elg": F, "sell_elg": F,
            "net_mf": F,                      # yuan
            "source": S,
        }, "year"),
        # 解禁 (design 2026-10-15 §5.2): one row a holder's unlock as first announced; known from its ann_date
        Dataset("unlocks", ("listing_id", "float_date", "holder", "share_type"), {
            "listing_id": S, "float_date": D,
            "holder": S, "share_type": S,     # "" when the source names none
            "ann_date": D,
            "float_share": F,                 # shares
            "float_ratio": F,                 # % of the total shares
            "source": S,
        }, None),
        # 前十大流通股东 (多个外资持股, 10-17): one row a holder of a report's list as announced; a holder's two accounts in one
        # list are one row (their float shares summed). Known from the trading day after ann_date, as 财务
        Dataset("float_holders", ("listing_id", "period_end", "ann_date", "holder"), {
            "listing_id": S, "period_end": D, "ann_date": D,
            "holder": S, "holder_type": S,    # Tushare's own type (coarse: QFII come as 一般企业)
            "float_ratio": F,                 # % of the float
            "source": S,
        }, None),
    ]
}


def empty(name: str) -> pl.DataFrame:
    return pl.DataFrame(schema=DATASETS[name].columns)


def conform(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """Return `df` with exactly the dataset's columns, order and dtypes.

    Missing non-key columns become null; missing key columns or unknown columns are adapter
    bugs and raise SchemaError; values that cannot be cast raise too (no silent coercion).
    """
    ds = DATASETS[name]
    extra = set(df.columns) - set(ds.columns)
    if extra:
        raise SchemaError(f"{name}: unknown columns {sorted(extra)}")
    missing_keys = [k for k in ds.key if k not in df.columns]
    if missing_keys:
        raise SchemaError(f"{name}: missing key columns {missing_keys}")
    null_keys = df.filter(pl.any_horizontal([pl.col(k).is_null() for k in ds.key])).height
    if null_keys:
        raise SchemaError(f"{name}: {null_keys} rows with null key values")
    try:
        return df.select(
            [
                (pl.col(c) if c in df.columns else pl.lit(None)).cast(t, strict=True).alias(c)
                for c, t in ds.columns.items()
            ]
        )
    except pl.exceptions.InvalidOperationError as e:
        raise SchemaError(f"{name}: {e}") from e
