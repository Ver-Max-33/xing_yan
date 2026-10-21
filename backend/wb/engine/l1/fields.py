"""Formula fields (spec §5.3, §3.5 mechanism 2): each name maps to a column of the evaluation frame
built by `frame.build_frame`, with its unit and the markets that have it. A field a market lacks is
a compile error, never a silent zero."""
from dataclasses import dataclass

from wb.engine.l1.units import U


@dataclass(frozen=True)
class Field:
    name: str
    column: str
    unit: U
    doc: str
    markets: tuple[str, ...] = ("CN", "JP")
    dataset: str = "bars_1d"


_F = [
    Field("O", "open", U.PRICE, "开盘价（后复权）"),
    Field("H", "high", U.PRICE, "最高价（后复权）"),
    Field("L", "low", U.PRICE, "最低价（后复权）"),
    Field("C", "close", U.PRICE, "收盘价（后复权）"),
    Field("V", "volume", U.VOLUME, "成交量（股，按股本变动调整到可比口径）"),
    Field("AMOUNT", "amount", U.AMOUNT, "成交额（元 / 日元）"),
    Field("VWAP", "vwap", U.PRICE, "均价（当天成交额 ÷ 实际成交股数，后复权，和 C 同一口径；分时图的黄线收在这里）"),
    Field("PRE", "pre", U.PRICE, "昨收（交易所给的昨收，和 C 同一口径；除权日是除权参考价）"),
    Field("RAW_O", "raw_open", U.RAW_PRICE, "开盘价（交易所原始价，用于绝对价格门槛）"),
    Field("RAW_H", "raw_high", U.RAW_PRICE, "最高价（原始价）"),
    Field("RAW_L", "raw_low", U.RAW_PRICE, "最低价（原始价）"),
    Field("RAW_C", "raw_close", U.RAW_PRICE, "收盘价（原始价）"),
    Field("RAW_V", "raw_volume", U.VOLUME, "成交量（当天实际股数）"),
    Field("IS_ST", "is_st", U.BOOL, "当天是否为 ST / *ST", ("CN",), "status_intervals"),
    Field("IDX_C", "idx_close", U.NUMBER, "对照指数的收盘点位（这只股票所在市场或板块的默认对照指数，按它的交易日对齐）",
          ("CN", "JP"), "index_bars_1d"),
]

# Valuation columns of the dataset catalog (datasets.py) as fields: name, unit, label and the markets
# whose source fills it. A column the catalog adds later becomes a numeric field of its own name.
_VALUATION = {
    "turnover_rate": ("TURNOVER", U.NUMBER, "换手率（%）", ("CN",)),
    "pe": ("PE", U.NUMBER, "市盈率", ("CN", "JP")),
    "pe_ttm": ("PE_TTM", U.NUMBER, "市盈率（TTM）", ("CN",)),
    "pb": ("PB", U.NUMBER, "市净率", ("CN", "JP")),
    "ps_ttm": ("PS_TTM", U.NUMBER, "市销率（TTM）", ("CN",)),
    "dv_ttm": ("DV_TTM", U.NUMBER, "股息率（TTM，%）", ("CN",)),
    "total_share": ("TOTAL_SHARE", U.NUMBER, "总股本（股）", ("CN",)),
    "float_share": ("FLOAT_SHARE", U.NUMBER, "流通股本（股）", ("CN",)),
    "total_mv": ("TOTAL_MV", U.AMOUNT, "总市值", ("CN", "JP")),
    "circ_mv": ("CIRC_MV", U.AMOUNT, "流通市值", ("CN",)),
    "eps": ("EPS", U.RAW_PRICE, "每股收益（日股估值接口）", ("JP",)),
    "bps": ("BPS", U.RAW_PRICE, "每股净资产（日股估值接口）", ("JP",)),
}


def _catalog_fields():
    from wb.datasets import DATASETS

    for col in DATASETS["valuation"].columns:
        if col not in ("listing_id", "date", "source"):
            name, unit, doc, markets = _VALUATION.get(col, (col.upper(), U.NUMBER, col, ("CN", "JP")))
            yield Field(name, col, unit, doc, markets, "valuation")


_F += list(_catalog_fields())

# 财务 PIT (spec §4.7): the latest report period known by the day's close (frame._financials); the figures are the
# report's own — cumulative from the start of the fiscal year (a Q1 report's are one quarter's, an annual report's a year's)
_FIN = {
    "revenue": ("FIN_REVENUE", U.AMOUNT, "营业收入（最近一期报告，年初至报告期末的累计数）"),
    "op_profit": ("FIN_OP_PROFIT", U.AMOUNT, "营业利润（最近一期报告的累计数）"),
    "net_profit": ("FIN_NET_PROFIT", U.AMOUNT, "归母净利润（最近一期报告的累计数）"),
    "eps": ("FIN_EPS", U.RAW_PRICE, "每股收益（最近一期报告的累计数，和交易所原始价同一口径）"),
    "roe": ("FIN_ROE", U.NUMBER, "ROE（%，最近一期报告；日股只有年报给）"),
    "revenue_yoy": ("FIN_REVENUE_YOY", U.NUMBER, "营业收入同比（%，A 股）"),
    "profit_yoy": ("FIN_PROFIT_YOY", U.NUMBER, "归母净利润同比（%，A 股）"),
    "equity": ("FIN_EQUITY", U.AMOUNT, "归母股东权益（最近一期报告期末）"),
    "total_assets": ("FIN_TOTAL_ASSETS", U.AMOUNT, "总资产（最近一期报告期末）"),
    "cfo": ("FIN_CFO", U.AMOUNT, "经营活动现金流净额（最近一期报告的累计数）"),
}
_F += [Field(name, f"fin_{col}", unit, doc, ("CN",) if col.endswith("_yoy") else ("CN", "JP"), "financials")
       for col, (name, unit, doc) in _FIN.items()]
# 筹码分布 (spec §8.6, l1/chips.py): 东方财富's algorithm over the 210 bars up to the day, as the K 线页 draws it
_F += [Field("CHIP_PROFIT", "chip_profit", U.RATIO, "获利比例：收盘价以下的筹码占全部的多少（筹码分布，东方财富的算法，210 根）", ("CN",), "chips"),
       Field("CHIP_COST", "chip_cost", U.PRICE, "平均成本：一半筹码在它以下的价格（后复权，和 C 同一口径）", ("CN",), "chips"),
       Field("CHIP_CONC90", "chip_conc90", U.RATIO, "90% 筹码的集中度：(上限 − 下限) ÷ (上限 + 下限)，越小越集中", ("CN",), "chips"),
       Field("CHIP_CONC70", "chip_conc70", U.RATIO, "70% 筹码的集中度，同上", ("CN",), "chips")]
# 多个外资持股 (10-17): 外资 as the 资料 page groups them, in the latest top-10 float holders' list (frame._float_holders)
_F += [Field("FOREIGN_HOLDERS", "foreign_holders", U.NUMBER, "前十大流通股东里的外资家数：最近一期已公告的报告（公告后的下一个交易日起算），"
             "QFII、香港和澳门的机构、外国的公司和陆股通都算，个人、H 股代持、国内基金不算", ("CN",), "float_holders")]
ALIASES = {"OPEN": "O", "HIGH": "H", "LOW": "L", "CLOSE": "C", "VOL": "V", "VOLUME": "V", "AMO": "AMOUNT"}
FIELDS: dict[str, Field] = {f.name: f for f in _F}


def lookup(name: str) -> Field | None:
    key = name.upper()
    return FIELDS.get(ALIASES.get(key, key))
