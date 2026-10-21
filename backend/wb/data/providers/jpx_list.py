"""JPX listed-issue list (test profile JP instruments, spec §4.2).

Excel published by JPX monthly (3rd business day). Verified 2026-10-02: the file is now
data_j.xlsx (the old data_j.xls URL returns 404) with columns
日付, コード, 銘柄名, 市場・商品区分, 33業種コード, 33業種区分, 17業種コード, 17業種区分, 規模コード, 規模区分.
It lists every issue (ETF, REIT, PRO Market too); `board` keeps 市場・商品区分 so later phases
can choose the universe. It has no delisting history.
"""
import io
from datetime import date

import pandas as pd
import polars as pl

from wb.data.http import HttpClient
from wb.data.providers.base import Probe, Provider
from wb.registry import PROVIDERS

URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"


def parse_jpx_xlsx(content: bytes) -> pl.DataFrame:
    pdf = pd.read_excel(io.BytesIO(content), dtype=str)
    df = pl.DataFrame({c: pdf[c].tolist() for c in ("コード", "銘柄名", "市場・商品区分", "33業種区分")},
                      schema={c: pl.Utf8 for c in ("コード", "銘柄名", "市場・商品区分", "33業種区分")})
    return df.filter(pl.col("コード").str.contains(r"^[0-9][0-9A-Z]{3}$")).select(
        (pl.lit("JP:") + pl.col("コード")).alias("listing_id"), pl.lit("JP").alias("market"),
        pl.col("コード").alias("code"), pl.lit("XTKS").alias("exchange"),
        pl.col("銘柄名").alias("name"), pl.col("市場・商品区分").alias("board"),
        pl.when(pl.col("33業種区分") != "-").then(pl.col("33業種区分")).alias("industry"),
        pl.lit("L").alias("status"), pl.lit("jpx_list").alias("source"),
    )


@PROVIDERS.register("jpx_list")
class JpxList(Provider):
    name = "jpx_list"
    complete_list = True
    unit_kinds = {"instruments": "all"}

    def __init__(self, market: str = "JP", http: HttpClient | None = None):
        self.market = market
        self.http = http or HttpClient(per_minute=10)

    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        r = self.http.request("GET", URL)
        r.raise_for_status()
        return {"instruments": parse_jpx_xlsx(r.content)}

    def probe(self) -> list[Probe]:
        r = self.http.request("HEAD", URL)
        return [Probe("instruments", r.status_code == 200, detail=f"HTTP {r.status_code}")]
