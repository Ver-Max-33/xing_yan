"""J-Quants API V2 adapter for Japanese stocks (spec §4.2).

Plain REST (base https://api.jquants.com/v2, header x-api-key, cursor `pagination_key`), so we
control rate limiting and can record responses for contract tests. Field names follow the
official V2 reference (checked 2026-10-02):
  /equities/bars/daily   Date Code O H L C UL LL Vo Va AdjFactor ...  (no-trade days: OHLC/Vo/Va null)
  /equities/master       Date Code CoName CoNameEn S17 S17Nm S33 S33Nm ScaleCat Mkt MktNm Mrgn ...
  /equities/valuation    Date Code EPS FwdEPS BPS ROE FwdROE PER FwdPER PBR MktCap(JPY millions)
  /indices/bars/daily    Date Code O H L C
  /markets/calendar      Date HolDiv  (0 closed, 1 open, 2 TSE half day, 3 closed with OSE holiday trading)
AdjFactor is per event (0.5 on the ex-date of a 1:2 split, 1 otherwise), not cumulative.
History comes a month at a time from the bulk files (/bulk/get: a gzip'd CSV a month with the endpoint's own fields,
Light plan and up), as J-Quants advises: one request a month instead of one or more a day.
"""
import gzip
import io
import re
from datetime import date, timedelta

import polars as pl

from wb.data.http import HttpClient
from wb.data.providers.base import Probe, Provider, Unavailable, listed_on
from wb.listing import index_id, jquants_id_expr
from wb.registry import PROVIDERS
from wb.secrets import get_secret

BASE = "https://api.jquants.com/v2"
INDEXES = {"0000": "TOPIX", "0500": "PRIME", "0501": "STANDARD", "0502": "GROWTH"}
# the market segment indexes began with their segments' first quote day (J-Quants rows from 2022-06-27, checked
# 2026-10-08); a day before it is not "missing"
SEGMENTS_SINCE = date(2022, 6, 27)
RATE_BY_YEARS = [(3, 5), (6, 60), (99, 120)]  # history years -> requests/min (Free, Light, Standard+)
# 商品区分 (ProdCat, checked 2026-10-08): 011 domestic and 021 foreign stocks keep their market segment as the board; the
# rest are not stocks and are named as JPX's list names them (the market segment of a fund is only その他)
PRODUCTS = {"012": "出資証券", "013": "REIT・ベンチャーファンド・カントリーファンド・インフラファンド", "014": "ETF・ETN",
            "023": "ETF・ETN"}


class JQuantsError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(f"J-Quants {status}: {msg}")
        self.status, self.msg = status, msg


class JQuantsDenied(JQuantsError, Unavailable):
    """Bad/missing key, or the date range/dataset is outside the subscription."""


def _d(col: str) -> pl.Expr:
    return pl.col(col).str.strptime(pl.Date, "%Y-%m-%d", strict=False).alias(col)


def _frame(src: list[dict] | pl.DataFrame, schema: dict) -> pl.DataFrame:
    """The columns `schema` names, typed: an endpoint's rows (JSON values, '' for unknown where it says so) or a bulk
    file's (CSV text, nothing for no value)."""
    if isinstance(src, list):
        return pl.DataFrame([{c: r.get(c) for c in schema} for r in src], schema=schema)
    return src.select([(pl.col(c) if c in src.columns else pl.lit(None, pl.Utf8)).cast(t, strict=False).alias(c)
                       for c, t in schema.items()])


@PROVIDERS.register("jquants")
class JQuants(Provider):
    name = "jquants"
    complete_list = True  # /equities/master lists every current issue
    index_since = {index_id("JP", name): SEGMENTS_SINCE for name in ("PRIME", "STANDARD", "GROWTH")}
    unit_kinds = {"bars_1d": "date", "valuation": "date", "index_bars_1d": "date", "financials": "date",
                  "instruments": "all", "calendar": "all"}

    def __init__(self, market: str = "JP", api_key: str | None = None, http: HttpClient | None = None):
        if market != "JP":
            raise ValueError("jquants only serves JP")
        self.market = market
        self.api_key = api_key or get_secret("jquants_api_key")
        self.http = http or HttpClient(per_minute=5)
        self._month: dict[str, tuple[str, pl.DataFrame | None]] = {}   # endpoint -> (YYYY-MM, its file's rows or None)

    def get(self, path: str, **params) -> list[dict]:
        if not self.api_key:
            raise JQuantsDenied(401, "还没有 J-Quants 的 API key：在 设置 → 数据源与能力 里填（命令行：wb secret set jquants_api_key）")
        rows: list[dict] = []
        while True:
            r = self.http.request("GET", BASE + path, params=params, headers={"x-api-key": self.api_key})
            if r.status_code != 200:
                try:
                    msg = r.json().get("message", r.text)
                except ValueError:
                    msg = r.text
                denied = r.status_code in (401, 403) or "subscription" in msg.lower()
                raise (JQuantsDenied if denied else JQuantsError)(r.status_code, msg)
            j = r.json()
            rows += j.get("data", [])
            if not j.get("pagination_key"):
                return rows
            params = {**params, "pagination_key": j["pagination_key"]}

    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        return getattr(self, f"_{dataset}")(unit=unit, start=start, end=end)

    def _day(self, path: str, unit: str, end: date, day: str = "Date") -> list[dict] | pl.DataFrame:
        """A day's rows of a daily endpoint: from its month's bulk file when the month lies before the range's last
        (a backfill), else (the days being updated, a month whose file is not out yet, a plan without files) asked
        for the day."""
        month = unit[:7]
        if month < f"{end:%Y-%m}":
            if self._month.get(path, ("",))[0] != month:
                self._month[path] = (month, self._bulk(path, month))
            rows = self._month[path][1]
            if rows is not None:
                return rows.filter(pl.col(day) == unit)
        return self.get(path, date=unit)

    def _bulk(self, path: str, month: str) -> pl.DataFrame | None:
        r = self.http.request("GET", BASE + "/bulk/get", params={"endpoint": path, "date": month},
                              headers={"x-api-key": self.api_key})
        if r.status_code != 200 or "url" not in (j := r.json()):
            return None   # "The specified file does not exist" (not out yet), or no files on this plan
        f = self.http.request("GET", j["url"])   # a signed address, valid 5 minutes: the key is not sent there
        f.raise_for_status()
        return pl.read_csv(io.BytesIO(gzip.decompress(f.content)), infer_schema_length=0)

    def _bars_1d(self, unit: str, end: date, **_) -> dict[str, pl.DataFrame]:
        df = _frame(self._day("/equities/bars/daily", unit, end),
                    {"Date": pl.Utf8, "Code": pl.Utf8, "O": pl.Float64, "H": pl.Float64, "L": pl.Float64, "C": pl.Float64,
                     "UL": pl.Utf8, "LL": pl.Utf8, "Vo": pl.Float64, "Va": pl.Float64, "AdjFactor": pl.Float64})
        df = df.with_columns(jquants_id_expr("Code").alias("listing_id"), _d("Date"))
        bars = df.select(
            "listing_id", pl.col("Date").alias("date"),
            pl.col("O").alias("open"), pl.col("H").alias("high"), pl.col("L").alias("low"), pl.col("C").alias("close"),
            pl.col("Vo").alias("volume"), pl.col("Va").alias("amount"),
            (pl.col("UL") == "1").alias("ul"), (pl.col("LL") == "1").alias("ll"),
            (pl.col("C").is_null() & pl.col("Vo").is_null()).alias("suspended"),  # record exists, no trade
            pl.lit(self.name).alias("source"),
        )
        actions = df.filter(pl.col("AdjFactor").is_not_null() & (pl.col("AdjFactor") != 1.0)).select(
            "listing_id", pl.col("Date").alias("ex_date"), pl.lit("split").alias("kind"),
            (1.0 / pl.col("AdjFactor")).alias("ratio"), pl.lit(self.name).alias("source"),
        )
        return {"bars_1d": bars, "corp_actions": actions}

    def _listings(self, rows: list[dict], status: str) -> pl.DataFrame:
        cols = ["Code", "CoName", "CoNameEn", "S33Nm", "MktNm", "ProdCat"]
        df = pl.DataFrame([{c: r.get(c) for c in cols} for r in rows], schema={c: pl.Utf8 for c in cols})
        return df.select(
            jquants_id_expr("Code").alias("listing_id"), pl.lit("JP").alias("market"),
            jquants_id_expr("Code").str.slice(3).alias("code"), pl.lit("XTKS").alias("exchange"),
            pl.col("CoName").alias("name"), pl.col("CoNameEn").alias("name_en"),
            pl.coalesce(pl.col("ProdCat").replace_strict(PRODUCTS, default=None, return_dtype=pl.Utf8), "MktNm").alias("board"),
            pl.col("S33Nm").alias("industry"), pl.lit(status).alias("status"), pl.lit(self.name).alias("source"),
        )

    def _instruments(self, **_) -> dict[str, pl.DataFrame]:
        return {"instruments": self._listings(self.get("/equities/master"), "L")}

    def describe(self, last: dict[str, date]) -> pl.DataFrame:
        """Listings no current master lists (delisted), as the master listed them on their last day (`last`): one
        request a month, on the earliest of the month's last days (the month's others were still listed then), and a
        listing's own day for one the master did not have yet (listed later that month). One it never had is left out."""
        left, found = dict(last), []
        while left:
            day = min(left.values())
            month = [lid for lid, d in left.items() if (d.year, d.month) == (day.year, day.month)]
            got = self._listings(self.get("/equities/master", date=day.isoformat()), "D").filter(pl.col("listing_id").is_in(month))
            found.append(got)
            named = set(got["listing_id"])
            for lid in month:
                if lid in named or left[lid] == day:   # named, or asked on its own day
                    del left[lid]
        return pl.concat(found) if found else self._listings([], "D")

    def _calendar(self, start: date, end: date, **_) -> dict[str, pl.DataFrame]:
        rows = self.get("/markets/calendar", **{"from": start.isoformat(), "to": end.isoformat()})
        df = pl.DataFrame(rows, schema={"Date": pl.Utf8, "HolDiv": pl.Utf8})
        return {"calendar": df.select(_d("Date").alias("date"), pl.col("HolDiv").is_in(["1", "2"]).alias("is_open"),
                                      pl.lit(self.name).alias("source"))}

    def _valuation(self, unit: str, end: date, **_) -> dict[str, pl.DataFrame]:
        df = _frame(self._day("/equities/valuation", unit, end),
                    {"Date": pl.Utf8, "Code": pl.Utf8, "PER": pl.Float64, "PBR": pl.Float64, "EPS": pl.Float64,
                     "BPS": pl.Float64, "MktCap": pl.Float64})
        return {"valuation": df.select(
            jquants_id_expr("Code").alias("listing_id"), _d("Date").alias("date"),
            pl.col("PER").alias("pe"), pl.col("PBR").alias("pb"), pl.col("EPS").alias("eps"), pl.col("BPS").alias("bps"),
            (pl.col("MktCap") * 1_000_000).alias("total_mv"), pl.lit(self.name).alias("source"),
        )}

    def _financials(self, unit: str, end: date, **_) -> dict[str, pl.DataFrame]:
        """The day's financial statements (spec §4.7) with their disclosure time; a forecast or dividend revision has no
        figures of its own and is left out; '' is unknown, never 0; ROE in % as the A-share data."""
        num = {"Sales": "revenue", "OP": "op_profit", "NP": "net_profit", "EPS": "eps", "TA": "total_assets", "Eq": "equity",
               "CFO": "cfo", "ROE": "roe"}
        cols = {"Code": pl.Utf8, "DiscDate": pl.Utf8, "DiscTime": pl.Utf8, "DocType": pl.Utf8, "CurPerType": pl.Utf8,
                "CurPerEn": pl.Utf8, **{k: pl.Utf8 for k in num}}
        src = self._day("/fins/summary", unit, end, day="DiscDate")
        if isinstance(src, list):
            src = [{c: (r.get(c) or None) for c in cols} for r in src]
        df = _frame(src, cols).filter(pl.col("DocType").str.contains("FinancialStatements"))
        kinds = {"1Q": "Q1", "2Q": "H1", "3Q": "Q3", "FY": "FY"}
        return {"financials": df.select(
            jquants_id_expr("Code").alias("listing_id"), _d("CurPerEn").alias("period_end"),
            pl.col("CurPerType").replace(kinds).alias("period_type"), _d("DiscDate").alias("ann_date"),
            pl.col("DiscTime").alias("ann_time"),
            *[((pl.col(k).cast(pl.Float64) * 100).round(6) if a == "roe" else pl.col(k).cast(pl.Float64)).alias(a)
              for k, a in num.items()],
            pl.lit(None, pl.Float64).alias("revenue_yoy"), pl.lit(None, pl.Float64).alias("profit_yoy"),
            pl.col("DocType").alias("doc"), pl.lit(self.name).alias("source"),
        ).filter(pl.col("period_end").is_not_null() & pl.col("ann_date").is_not_null())}   # '' can't be placed in time

    def _index_bars_1d(self, unit: str, end: date, **_) -> dict[str, pl.DataFrame]:
        cols = {"Date": pl.Utf8, "Code": pl.Utf8, "O": pl.Float64, "H": pl.Float64, "L": pl.Float64, "C": pl.Float64}
        df = _frame(self._day("/indices/bars/daily", unit, end), cols).filter(pl.col("Code").is_in(list(INDEXES)))
        return {"index_bars_1d": df.select(
            pl.col("Code").replace_strict({k: index_id("JP", v) for k, v in INDEXES.items()}).alias("index_id"),
            _d("Date").alias("date"), pl.col("O").alias("open"), pl.col("H").alias("high"),
            pl.col("L").alias("low"), pl.col("C").alias("close"), pl.lit(self.name).alias("source"),
        )}

    # ---- capability probing (spec §4.9) --------------------------------------------------
    def probe(self) -> list[Probe]:
        today = date.today()
        out: list[Probe] = []
        try:
            self.get("/equities/master", code="72030")
        except JQuantsError as e:
            return [Probe(ds, False, detail=e.msg) for ds in self.unit_kinds]
        out.append(Probe("instruments", True))
        start = latest = None
        detail = ""
        try:
            rows = self.get("/equities/bars/daily", code="72030",
                            **{"from": (today - timedelta(days=365 * 21)).isoformat(), "to": today.isoformat()})
            dates = sorted(r["Date"] for r in rows)
            if dates:
                start, latest = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        except JQuantsError as e:
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})?", e.msg)
            if m:  # "subscription covers the following dates: A ~ B": retry inside the window
                start = date.fromisoformat(m.group(1))
                latest = date.fromisoformat(m.group(2)) if m.group(2) else None
            detail = e.msg
        years = (today - start).days / 365 if start else 0
        rate = next(r for y, r in RATE_BY_YEARS if years < y)
        self.http.limiter.set_rate(rate)
        out.append(Probe("bars_1d", start is not None, start, latest, rate, detail))
        # Query inside the subscription window: the free plan is ~12 weeks delayed, so "the last two
        # weeks" would look unavailable even though the dataset is included.
        hi = latest or today
        window = {"from": (hi - timedelta(days=14)).isoformat(), "to": hi.isoformat()}
        for ds, path, params in [
            ("valuation", "/equities/valuation", {"code": "72030"}),
            ("index_bars_1d", "/indices/bars/daily", {"code": "0000"}),
            ("calendar", "/markets/calendar", {}),
        ]:
            try:
                self.get(path, **params, **window)
                out.append(Probe(ds, True, start, latest, rate))
            except JQuantsError as e:
                out.append(Probe(ds, False, rate_per_min=rate, detail=e.msg))
        try:   # 财务 (spec §4.7): the statement summaries, asked by company (they come by disclosure day)
            self.get("/fins/summary", code="72030")
            out.append(Probe("financials", True, start, latest, rate))
        except JQuantsError as e:
            out.append(Probe("financials", False, rate_per_min=rate, detail=e.msg))
        return out

    def ready_count(self, day: date, instruments: pl.DataFrame) -> tuple[int, int]:
        n = len(self.get("/equities/bars/daily", date=day.isoformat()))
        return n, listed_on(instruments, day).height
