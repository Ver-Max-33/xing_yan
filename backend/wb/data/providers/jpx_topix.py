"""TOPIX's constituent list from JPX (spec §7.1 股票池: 指数成分; both JP profiles).

A CSV in Shift_JIS (cp932) published monthly: 日付, 銘柄名, コード, 業種, TOPIXに占める個別銘柄のウエイト,
ニューインデックス区分 (verified 2026-10-03: 1,650 lines dated the last month-end, then disclaimer lines). Only the
current list is published, so the history starts with the first list fetched. The size class (TOPIX Core30,
Large70, Mid400, Small 1 / 2) gives TOPIX100 and TOPIX500 (wb.engine.l1.frame.index_membership)."""
import io
from datetime import date

import polars as pl

from wb.data.http import HttpClient
from wb.data.providers.base import Probe, Provider
from wb.registry import PROVIDERS

URL = "https://www.jpx.co.jp/automation/markets/indices/topix/files/topixweight_j.csv"


def parse_topix(content: bytes) -> pl.DataFrame:
    text = content.decode("cp932")
    raw = pl.read_csv(io.StringIO(text), infer_schema=False, truncate_ragged_lines=True)
    rows = raw.filter(pl.col("日付").str.contains(r"^\d{8}$") & pl.col("コード").str.contains(r"^[0-9][0-9A-Z]{3}$"))
    return rows.select(
        pl.lit("JP:IDX:TOPIX").alias("index_id"), pl.col("日付").str.strptime(pl.Date, "%Y%m%d").alias("date"),
        (pl.lit("JP:") + pl.col("コード")).alias("listing_id"),
        pl.col("TOPIXに占める個別銘柄のウエイト").str.strip_suffix("%").cast(pl.Float64).alias("weight"),
        pl.col("ニューインデックス区分").alias("segment"), pl.lit("jpx_topix").alias("source"))


@PROVIDERS.register("jpx_topix")
class JpxTopix(Provider):
    name = "jpx_topix"
    unit_kinds = {"index_members": "all"}

    def __init__(self, market: str = "JP", http: HttpClient | None = None):
        self.market = market
        self.http = http or HttpClient(per_minute=10)

    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        r = self.http.request("GET", URL)
        r.raise_for_status()
        return {"index_members": parse_topix(r.content)}

    def probe(self) -> list[Probe]:
        r = self.http.request("HEAD", URL)
        return [Probe("index_members", r.status_code == 200, detail=f"HTTP {r.status_code}")]
