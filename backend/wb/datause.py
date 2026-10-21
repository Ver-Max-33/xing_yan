"""数据使用开关 (spec §4.10): per data source, whether its data may be kept here (all that is fetched is kept: off, it
is not fetched at all), sent to an LLM (as derived facts in an evidence pack), written into notifications (its
numbers), exported (CSV). The defaults are a conservative start, not legal advice: J-Quants' data is not exported."""
from wb.data.capability import ROUTES

SOURCES = {
    "tushare": "Tushare", "jquants": "J-Quants", "yfinance": "Yahoo Finance（测试档日线）", "jpx_list": "JPX 上市一览",
    "jpx_topix": "JPX TOPIX 构成", "exchange_list_cn": "沪深北交易所股票列表", "xcals": "交易所日历（exchange_calendars）",
    "tushare_rt": "Tushare 实时日线", "tickflow_rt": "TickFlow 分钟线（A 股盘中）", "tachibana": "立花证券 e支店",
    "yfinance_rt": "Yahoo 延迟分钟线（测试档盘中）",
    "cninfo": "巨潮资讯（A 股公告）",
}
USES = {"store": "本地存储", "llm": "发送给 LLM", "notify": "出现在通知中", "export": "导出"}
OFF = {("jquants", "export")}   # spec §4.10's defaults


def defaults() -> dict[str, bool]:
    return {f"datause.{s}.{u}": (s, u) not in OFF for s in SOURCES for u in USES}


def allowed(settings: dict, source: str, use: str) -> bool:
    return bool(settings.get(f"datause.{source}.{use}", (source, use) not in OFF))


def source_of(market: str, profile: str) -> str:
    """Where a market's prices come from in a data profile: its daily bars' source."""
    return ROUTES[profile][market]["bars_1d"][0]


def quiet(settings: dict, sources: dict[str, str]) -> set[str]:
    """The markets whose source (market → source name) may not put its numbers into notifications."""
    return {m for m, s in sources.items() if not allowed(settings, s, "notify")}
