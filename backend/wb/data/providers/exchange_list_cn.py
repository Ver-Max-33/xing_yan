"""A-share lists straight from the exchanges (test profile CN instruments, spec §4.2).

Request logic ported from AkShare `stock_info_sh_name_code` / `stock_info_sz_name_code` (MIT),
without importing AkShare. Verified 2026-10-02. The Beijing exchange endpoint answers bots with a
307 loop, so BSE stocks are not listed here; the pipeline adds a placeholder instrument (status U)
for any code that appears in daily bars without a list entry. Only currently listed stocks.
"""
import io
import warnings
from datetime import date

import pandas as pd
import polars as pl

from wb.data.http import HttpClient
from wb.data.providers.base import Probe, Provider
from wb.registry import PROVIDERS

SSE_URL = "https://query.sse.com.cn/sseQuery/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport"
SSE_BOARDS = {"1": "主板", "8": "科创板"}


def sse_params(stock_type: str) -> dict[str, str]:
    return {"STOCK_TYPE": stock_type, "REG_PROVINCE": "", "CSRC_CODE": "", "STOCK_CODE": "",
            "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L", "COMPANY_STATUS": "2,4,5,7,8", "type": "inParams",
            "isPagination": "true", "pageHelp.cacheSize": "1", "pageHelp.beginPage": "1",
            "pageHelp.pageSize": "10000", "pageHelp.pageNo": "1", "pageHelp.endPage": "1"}


def parse_sse(result: list[dict], board: str) -> pl.DataFrame:
    df = pl.DataFrame([{k: r.get(k) for k in ("A_STOCK_CODE", "SEC_NAME_CN", "LIST_DATE")} for r in result],
                      schema={"A_STOCK_CODE": pl.Utf8, "SEC_NAME_CN": pl.Utf8, "LIST_DATE": pl.Utf8})
    return df.select(
        (pl.lit("CN:") + pl.col("A_STOCK_CODE")).alias("listing_id"), pl.col("A_STOCK_CODE").alias("code"),
        pl.lit("XSHG").alias("exchange"), pl.col("SEC_NAME_CN").alias("name"), pl.lit(board).alias("board"),
        pl.col("LIST_DATE").str.strptime(pl.Date, "%Y%m%d", strict=False).alias("list_date"),
    )


def parse_szse_xlsx(content: bytes) -> pl.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the SZSE file has no default style; openpyxl warns about it
        pdf = pd.read_excel(io.BytesIO(content), dtype=str)
    df = pl.DataFrame({c: pdf[c].tolist() for c in ("板块", "A股代码", "A股简称", "A股上市日期", "所属行业")},
                      schema={c: pl.Utf8 for c in ("板块", "A股代码", "A股简称", "A股上市日期", "所属行业")})
    code = pl.col("A股代码").str.zfill(6)
    return df.filter(pl.col("A股代码").is_not_null()).select(
        (pl.lit("CN:") + code).alias("listing_id"), code.alias("code"), pl.lit("XSHE").alias("exchange"),
        pl.col("A股简称").str.replace_all(r"\s|　", "").alias("name"), pl.col("板块").alias("board"),
        pl.col("所属行业").alias("industry"),
        pl.col("A股上市日期").str.strptime(pl.Date, "%Y-%m-%d", strict=False).alias("list_date"),
    )


@PROVIDERS.register("exchange_list_cn")
class ExchangeListCN(Provider):
    name = "exchange_list_cn"
    unit_kinds = {"instruments": "all"}

    def __init__(self, market: str = "CN", http: HttpClient | None = None):
        self.market = market
        self.http = http or HttpClient(per_minute=10)

    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        frames = []
        for stock_type, board in SSE_BOARDS.items():
            r = self.http.request("GET", SSE_URL, params=sse_params(stock_type),
                                  headers={"Referer": "https://www.sse.com.cn/assortment/stock/list/share/"})
            r.raise_for_status()
            frames.append(parse_sse(r.json()["result"], board))
        r = self.http.request("GET", SZSE_URL, params={"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"})
        r.raise_for_status()
        frames.append(parse_szse_xlsx(r.content))
        df = pl.concat(frames, how="diagonal_relaxed")
        return {"instruments": df.with_columns(pl.lit("CN").alias("market"), pl.lit("L").alias("status"),
                                               pl.lit(self.name).alias("source"))}

    def probe(self) -> list[Probe]:
        try:
            n = self.fetch("instruments", "all", date.today(), date.today())["instruments"].height
            return [Probe("instruments", n > 1000, detail=f"{n} listings (SSE + SZSE; BSE not available)")]
        except Exception as e:  # noqa: BLE001 - probing reports, never crashes
            return [Probe("instruments", False, detail=str(e))]
