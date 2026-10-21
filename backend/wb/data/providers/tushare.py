"""Tushare Pro adapter for A shares (spec §4.2), via its plain HTTP API.

One adapter serves both profiles: the free tier (120 points) only has `daily`, the 2000-point
tier has everything below. APIs that answer "no permission" are remembered and skipped for the
rest of the run, and the columns they would fill stay null (unknown), never guessed.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import polars as pl

from wb.data.http import HttpClient
from wb.data.providers.base import Probe, Provider, Unavailable, listed_on, ymd
from wb.datasets import empty
from wb.listing import index_id, tushare_id_expr, tushare_mic_expr
from wb.registry import PROVIDERS
from wb.secrets import get_secret

URL = "https://api.tushare.pro"  # the token travels in every request body
PAGE = {"daily": 6000, "adj_factor": 6000, "stk_limit": 5800, "daily_basic": 6000, "dividend": 2000,
        "stock_basic": 6000, "index_daily": 8000}
INDEXES = {"000001": "000001.SH", "399001": "399001.SZ", "399006": "399006.SZ",
           "000688": "000688.SH", "899050": "899050.BJ", "000300": "000300.SH"}
# First day index_daily has rows (checked 2026-10-02); days before are not "missing".
INDEX_SINCE = {"000001": "1990-12-19", "399001": "1991-04-03", "399006": "2010-05-31",
               "000688": "2019-12-31", "899050": "2022-04-29", "000300": "2002-01-04"}
DATASET_APIS = {  # dataset -> (required apis, optional apis)
    "bars_1d": (["daily"], ["adj_factor", "stk_limit", "suspend_d"]),
    "instruments": (["stock_basic"], []),
    "status_intervals": (["namechange"], []),
    "calendar": (["trade_cal"], []),
    "corp_actions": (["dividend"], []),
    "valuation": (["daily_basic"], []),
    "industry": (["index_classify", "index_member_all"], []),
    "index_members": (["index_weight"], []),
    "index_bars_1d": (["index_daily"], []),
    "financials": (["income_vip"], ["balancesheet_vip", "cashflow_vip", "fina_indicator_vip"]),
    "moneyflow": (["moneyflow"], []),
    "unlocks": (["share_float"], []),
    "float_holders": (["top10_floatholders"], []),
}
UNLOCK_BACK, UNLOCK_AHEAD = 7, 60  # calendar days around the update day asked of share_float (design 2026-10-15 §5.2)


class TushareError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"tushare {code}: {msg}")
        self.code, self.msg = code, msg


class PermissionDenied(TushareError, Unavailable): ...


class RateLimited(TushareError):
    def __init__(self, code: int, msg: str, per_minute: int):
        super().__init__(code, msg)
        self.per_minute = per_minute


class QuotaExceeded(TushareError): ...  # daily/hourly quota used up: stop loudly, not "not on this plan"


def _error(code: int, msg: str) -> TushareError:
    # Every limit message ends with "权限的具体详情访问：…", so "权限" alone means nothing.
    m = re.search(r"每分钟最多访问该接口(\d+)次", msg)
    if m:
        return RateLimited(code, msg, int(m.group(1)))
    if re.search(r"每(天|小时)最多访问该接口", msg):
        return QuotaExceeded(code, msg)
    if ("没有" in msg and "权限" in msg) or "积分不足" in msg:
        return PermissionDenied(code, msg)
    return TushareError(code, msg)


@PROVIDERS.register("tushare")
class Tushare(Provider):
    name = "tushare"
    complete_list = True  # stock_basic covers listed, paused and delisted stocks
    workers = 4           # days asked side by side: one at a time waits on the network (~0.3 s a call) far below the
                          # per-minute quota; the shared limiter keeps every call under it
    index_since = {index_id("CN", k): date.fromisoformat(v) for k, v in INDEX_SINCE.items()}
    unit_kinds = {"bars_1d": "date", "valuation": "date", "corp_actions": "date",
                  "instruments": "all", "status_intervals": "all", "calendar": "all",
                  "industry": "all", "index_bars_1d": "all", "index_members": "all", "financials": "period",
                  "moneyflow": "date", "unlocks": "all", "float_holders": "period"}

    def __init__(self, market: str = "CN", token: str | None = None, http: HttpClient | None = None,
                 sleep=None):
        if market != "CN":
            raise ValueError("tushare only serves CN")
        self.market = market
        self.token = token or get_secret("tushare_token")
        self.http = http or HttpClient(per_minute=50)
        self._sleep = sleep or time.sleep
        self.denied: set[str] = set()

    # ---- transport -----------------------------------------------------------------------
    def query(self, api: str, fields: str, **params) -> pl.DataFrame:
        if not self.token:
            raise PermissionDenied(-1, "还没有 Tushare 的 token：在 设置 → 数据源与能力 里填（命令行：wb secret set tushare_token）")
        if api in self.denied:
            raise PermissionDenied(-1, f"{api}: no permission (cached)")
        page, offset, frames, first_rows = PAGE.get(api, 5000), 0, [], set()
        names = fields.split(",")
        while True:
            got, items = self._ask(api, fields, {**params, "limit": page, "offset": offset})
            if missing := [n for n in names if n not in got]:   # Tushare leaves such a field out, without an error
                raise TushareError(-1, f"Tushare 的 {api} 接口没有字段 {', '.join(missing)}（它不报错，只是不返回这一列）")
            if items and tuple(items[0]) in first_rows:  # API ignored offset: stop instead of looping
                break
            if items:
                first_rows.add(tuple(items[0]))
            if items:
                frames.append(pl.DataFrame(items, schema=got, orient="row", infer_schema_length=None).select(names))
            if len(items) < page:
                break
            offset += page
        if not frames:  # e.g. a holiday: typed empty columns, so string/number casts still work
            return pl.DataFrame(schema={n: pl.Utf8 for n in names})
        return pl.concat(frames, how="diagonal_relaxed")

    def _call(self, api: str, fields: str, params: dict) -> list[list]:
        return self._ask(api, fields, params)[1]

    def _ask(self, api: str, fields: str, params: dict) -> tuple[list[str], list[list]]:
        """The fields the answer carries (not always those asked for) and its rows."""
        for _ in range(3):
            r = self.http.request("POST", URL, json={"api_name": api, "token": self.token,
                                                     "params": params, "fields": fields})
            r.raise_for_status()
            j = r.json()
            if j["code"] == 0:
                return j["data"].get("fields") or fields.split(","), j["data"]["items"]
            err = _error(j["code"], j.get("msg") or "")
            if isinstance(err, RateLimited):
                self.http.limiter.set_rate(err.per_minute)
                self._sleep(60)
                continue
            if isinstance(err, PermissionDenied):
                self.denied.add(api)
            raise err
        raise RateLimited(-1, "still rate limited after 3 tries", self.http.limiter.per_minute)

    def _each(self, fn, items) -> list:
        """fn over items, `workers` at a time, the answers in the items' order."""
        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(fn, items))

    def _optional(self, api: str, fields: str, **params) -> pl.DataFrame | None:
        try:
            return self.query(api, fields, **params)
        except PermissionDenied:
            return None

    # ---- datasets ------------------------------------------------------------------------
    def fetch(self, dataset: str, unit: str, start: date, end: date) -> dict[str, pl.DataFrame]:
        day = date.fromisoformat(unit) if self.unit_kinds[dataset] == "date" else None
        df = getattr(self, f"_{dataset}")(day=day, unit=unit, start=start, end=end)
        if "listing_id" in df.columns:  # drop codes that are not A-share stocks (funds, odd codes)
            df = df.filter(pl.col("listing_id").is_not_null())
        return {dataset: df}

    def _bars_1d(self, day: date, **_) -> pl.DataFrame:
        d = f"{day:%Y%m%d}"
        daily = self.query("daily", "ts_code,trade_date,open,high,low,close,pre_close,vol,amount", trade_date=d)
        bars = daily.select(
            pl.col("ts_code"), ymd("trade_date").alias("date"),
            *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "pre_close")],
            (pl.col("vol").cast(pl.Float64) * 100).alias("volume"),       # lots -> shares
            (pl.col("amount").cast(pl.Float64) * 1000).alias("amount"),   # thousand yuan -> yuan
            pl.lit(False).alias("suspended"),
        )
        adj = self._optional("adj_factor", "ts_code,adj_factor", trade_date=d)
        if adj is not None:
            bars = bars.join(adj.with_columns(pl.col("adj_factor").cast(pl.Float64)), on="ts_code", how="left")
        lim = self._optional("stk_limit", "ts_code,up_limit,down_limit", trade_date=d)
        if lim is not None:
            bars = bars.join(lim.with_columns(pl.col("up_limit", "down_limit").cast(pl.Float64)),
                             on="ts_code", how="left")
        sus = self._optional("suspend_d", "ts_code,suspend_type", trade_date=d, suspend_type="S")
        if sus is not None and sus.height:
            only_suspended = sus.select("ts_code").unique().join(bars.select("ts_code"), on="ts_code", how="anti")
            bars = pl.concat([bars, only_suspended.with_columns(pl.lit(day).alias("date"),
                                                                pl.lit(True).alias("suspended"))],
                             how="diagonal_relaxed")
        return bars.with_columns(tushare_id_expr().alias("listing_id"), pl.lit(self.name).alias("source")
                                 ).drop("ts_code")

    def _instruments(self, **_) -> pl.DataFrame:
        fields = "ts_code,symbol,name,enname,market,exchange,industry,list_status,list_date,delist_date"
        df = pl.concat([self.query("stock_basic", fields, list_status=s) for s in ("L", "D", "P")],
                       how="diagonal_relaxed")
        return df.select(
            tushare_id_expr().alias("listing_id"),
            pl.lit("CN").alias("market"), pl.col("symbol").alias("code"),
            tushare_mic_expr().alias("exchange"),
            "name", pl.col("enname").alias("name_en"), pl.col("market").alias("board"), "industry",
            ymd("list_date"), ymd("delist_date"), pl.col("list_status").alias("status"),
            pl.lit(self.name).alias("source"),
        )

    def _status_intervals(self, **_) -> pl.DataFrame:
        df = self.query("namechange", "ts_code,name,start_date,end_date")
        st = df.filter(pl.col("name").str.contains("ST"))
        return st.select(
            tushare_id_expr().alias("listing_id"),
            pl.lit("st").alias("kind"), pl.col("name").alias("value"),
            ymd("start_date").alias("start"), ymd("end_date").alias("end"),
            pl.lit(self.name).alias("source"),
        ).unique(subset=["listing_id", "kind", "start"], keep="last")

    def _calendar(self, start: date, end: date, **_) -> pl.DataFrame:
        df = self.query("trade_cal", "cal_date,is_open", exchange="SSE",
                        start_date=f"{start:%Y%m%d}", end_date=f"{end:%Y%m%d}")
        return df.select(ymd("cal_date").alias("date"), (pl.col("is_open").cast(pl.Int64) == 1).alias("is_open"),
                         pl.lit(self.name).alias("source"))

    def _corp_actions(self, day: date, **_) -> pl.DataFrame:
        df = self.query("dividend", "ts_code,div_proc,stk_div,cash_div_tax,ex_date", ex_date=f"{day:%Y%m%d}")
        df = df.filter(pl.col("div_proc") == "实施")
        return df.select(
            tushare_id_expr().alias("listing_id"),
            ymd("ex_date"), pl.lit("dividend").alias("kind"),
            (1 + pl.col("stk_div").cast(pl.Float64).fill_null(0.0)).alias("ratio"),
            pl.col("cash_div_tax").cast(pl.Float64).alias("cash_div"),
            pl.lit(self.name).alias("source"),
        ).unique(subset=["listing_id", "ex_date", "kind"], keep="last")

    def _financials(self, unit: str, **_) -> pl.DataFrame:
        """One report period (a quarter end) of every company: the income statement is the spine; the balance sheet,
        cash flow and indicators join on the same announcement. A corrected copy (update_flag 1) wins over its first
        copy of the same day; one that came out later (f_ann_date, 实际公告日期, after its ann_date) is its own row from
        that day — real data has corrections that keep the first announcement's ann_date."""
        period = unit.replace("-", "")
        key = ["ts_code", "end_date", "ann_date"]

        def latest(df: pl.DataFrame | None, cols: dict[str, str]) -> pl.DataFrame | None:
            if df is None or df.is_empty():
                return None
            out_day = pl.when(pl.col("f_ann_date") > pl.col("ann_date")).then("f_ann_date").otherwise("ann_date") \
                if "f_ann_date" in df.columns else pl.col("ann_date")
            return (df.with_columns(out_day.alias("ann_date")).sort("update_flag").unique(key, keep="last")
                    .select(*key, *[pl.col(c).cast(pl.Float64).alias(a) for c, a in cols.items()]))
        base = "ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,"
        out = latest(self.query("income_vip", base + "revenue,operate_profit,n_income_attr_p,basic_eps", period=period),
                     {"revenue": "revenue", "operate_profit": "op_profit", "n_income_attr_p": "net_profit", "basic_eps": "eps"})
        if out is None:  # the quarter end just passed: no report yet
            return empty("financials")
        statement = base.replace("report_type,", "")
        for api, fields, cols in (
                ("balancesheet_vip", statement + "total_assets,total_hldr_eqy_exc_min_int", {"total_assets": "total_assets", "total_hldr_eqy_exc_min_int": "equity"}),
                ("cashflow_vip", statement + "n_cashflow_act", {"n_cashflow_act": "cfo"}),
                # the indicators have no f_ann_date: one row a report, dated by its first announcement (checked 2026-10-08)
                ("fina_indicator_vip", statement.replace("f_ann_date,", "") + "roe,netprofit_yoy,or_yoy",
                 {"roe": "roe", "netprofit_yoy": "profit_yoy", "or_yoy": "revenue_yoy"})):
            part = latest(self._optional(api, fields, period=period), cols)
            if part is None:
                out = out.with_columns(pl.lit(None, pl.Float64).alias(a) for a in cols.values())
            elif "f_ann_date" in fields:
                out = out.join(part, on=key, how="left")
            else:   # each announcement takes the indicators known by its day (a later corrected copy: the same ones)
                day = pl.col("ann_date").cast(pl.Int64).alias("_d")
                out = (out.with_columns(day).sort("_d")
                       .join_asof(part.with_columns(day).drop("ann_date").sort("_d"), on="_d", by=["ts_code", "end_date"], check_sortedness=False)
                       .drop("_d"))
        quarter = {3: "Q1", 6: "H1", 9: "Q3", 12: "FY"}   # A-share reports: cumulative to these quarter ends
        return out.select(
            tushare_id_expr().alias("listing_id"), ymd("end_date").alias("period_end"),
            ymd("end_date").dt.month().replace_strict(quarter, default=None, return_dtype=pl.Utf8).alias("period_type"),
            ymd("ann_date").alias("ann_date"), pl.lit(None, pl.Utf8).alias("ann_time"),
            "revenue", "op_profit", "net_profit", "eps", "total_assets", "equity", "cfo", "roe", "revenue_yoy", "profit_yoy",
            pl.lit("合并报表").alias("doc"), pl.lit(self.name).alias("source"),
        )

    def _valuation(self, day: date, **_) -> pl.DataFrame:
        df = self.query("daily_basic", "ts_code,trade_date,turnover_rate,pe,pe_ttm,pb,ps_ttm,dv_ttm,"
                                       "total_share,float_share,total_mv,circ_mv", trade_date=f"{day:%Y%m%d}")
        wan = 10_000  # share counts in 万股, market value in 万元
        return df.select(
            tushare_id_expr().alias("listing_id"),
            ymd("trade_date").alias("date"),
            *[pl.col(c).cast(pl.Float64) for c in ("pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm", "turnover_rate")],
            *[(pl.col(c).cast(pl.Float64) * wan).alias(c) for c in ("total_share", "float_share", "total_mv", "circ_mv")],
            pl.lit(self.name).alias("source"),
        )

    def _moneyflow(self, day: date, **_) -> pl.DataFrame:
        kinds = ("sm", "md", "lg", "elg")
        cols = [f"{s}_{k}_amount" for k in kinds for s in ("buy", "sell")]
        df = self.query("moneyflow", "ts_code,trade_date," + ",".join(cols) + ",net_mf_amount", trade_date=f"{day:%Y%m%d}")
        wan = 10_000  # 万元
        return df.select(
            tushare_id_expr().alias("listing_id"), ymd("trade_date").alias("date"),
            *[(pl.col(f"{s}_{k}_amount").cast(pl.Float64) * wan).alias(f"{s}_{k}") for k in kinds for s in ("buy", "sell")],
            (pl.col("net_mf_amount").cast(pl.Float64) * wan).alias("net_mf"),
            pl.lit(self.name).alias("source"),
        )

    def _unlocks(self, end: date, **_) -> pl.DataFrame:
        """The unlocks due from a week before `end` to two months after, one float date a call (a month at once can pass
        the 100,000-row offset cap); each holder's row as first announced (the same unlock comes again in later notices,
        its share count printed to another precision)."""
        fields = "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type"
        days = [end + timedelta(days=k) for k in range(-UNLOCK_BACK, UNLOCK_AHEAD + 1)]
        frames = [f for f in self._each(lambda d: self.query("share_float", fields, float_date=f"{d:%Y%m%d}"), days) if f.height]
        if not frames:
            return empty("unlocks")
        df = pl.concat(frames, how="diagonal_relaxed")
        return (df.select(tushare_id_expr().alias("listing_id"), ymd("float_date"),
                          pl.col("holder_name").fill_null("").alias("holder"), pl.col("share_type").fill_null("").alias("share_type"),
                          ymd("ann_date"), pl.col("float_share").cast(pl.Float64), pl.col("float_ratio").cast(pl.Float64),
                          pl.lit(self.name).alias("source"))
                .filter(pl.col("float_date").is_not_null())
                .sort("ann_date", nulls_last=True)
                .unique(["listing_id", "float_date", "holder", "share_type"], keep="first", maintain_order=True))

    def _float_holders(self, unit: str, **_) -> pl.DataFrame:
        """One report period's top-10 float holders of every company (one call a page, ~58,000 rows: 12 pages, ~30 s on
        2026-10-17) — asked by period, not by announcement day: a fifth of the lists are dated a Saturday. A holder's two
        accounts in one list (431 of 2.06 million since 2015) are one row, their float shares summed."""
        df = self.query("top10_floatholders", "ts_code,ann_date,end_date,holder_name,hold_float_ratio,holder_type", period=unit.replace("-", ""))
        return (df.select(tushare_id_expr().alias("listing_id"), ymd("end_date").alias("period_end"), ymd("ann_date"),
                          pl.col("holder_name").alias("holder"), "holder_type", pl.col("hold_float_ratio").cast(pl.Float64).alias("float_ratio"))
                .filter(pl.col("period_end").is_not_null() & pl.col("ann_date").is_not_null() & pl.col("holder").is_not_null())
                .group_by("listing_id", "period_end", "ann_date", "holder", maintain_order=True)
                .agg(pl.col("holder_type").first(), pl.when(pl.col("float_ratio").count() > 0).then(pl.col("float_ratio").sum()).alias("float_ratio"))
                .with_columns(pl.lit(self.name).alias("source")))

    def _industry(self, **_) -> pl.DataFrame:
        l1 = self.query("index_classify", "index_code,industry_name", level="L1", src="SW2021")
        fields = "l1_code,l1_name,l2_code,l2_name,ts_code,in_date,out_date"   # 二级 comes in the same answer
        rows = self._each(lambda cn: self.query("index_member_all", fields, l1_code=cn[0], is_new=cn[1]),
                          [(c, n) for c in l1["index_code"].to_list() for n in ("Y", "N")])
        df = pl.concat(rows, how="diagonal_relaxed") if rows else pl.DataFrame(schema={c: pl.Utf8 for c in fields.split(",")})
        return df.select(
            tushare_id_expr().alias("listing_id"),
            pl.lit("SW2021_L1").alias("scheme"), pl.col("l1_code").alias("code"), pl.col("l1_name").alias("name"),
            ymd("in_date"), ymd("out_date"), pl.lit(self.name).alias("source"), "l2_code", "l2_name",
        ).filter(pl.col("in_date").is_not_null()).unique(subset=["listing_id", "scheme", "in_date"], keep="last")

    def _index_members(self, start: date, end: date, **_) -> pl.DataFrame:
        """Each index's constituents as its month-end weight list (index_weight), month by month: one month of
        上证指数 is ~2,200 rows, under the per-call limit (verified 2026-10-03: 沪深300 300 rows summing to 100%).
        A month's list is dated its last trading day and comes out after that close, so the month before `start` is
        asked too (a weekly refresh whose window starts after a month ended mid-week); none before an index existed."""
        asks = []
        y, m = (start.year, start.month - 1) if start.month > 1 else (start.year - 1, 12)
        while (y, m) <= (end.year, end.month):
            first = date(y, m, 1)
            last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
            asks += [(name, code, first, last) for name, code in INDEXES.items() if last >= date.fromisoformat(INDEX_SINCE[name])]
            y, m = y + (m == 12), m % 12 + 1

        def month(a):
            name, code, first, last = a
            df = self.query("index_weight", "index_code,con_code,trade_date,weight", index_code=code,
                            start_date=f"{first:%Y%m%d}", end_date=f"{last:%Y%m%d}")
            return df.select(
                pl.lit(index_id("CN", name)).alias("index_id"), ymd("trade_date").alias("date"),
                tushare_id_expr("con_code").alias("listing_id"), pl.col("weight").cast(pl.Float64),
                pl.lit(None, pl.Utf8).alias("segment"), pl.lit(self.name).alias("source")) if df.height else None
        frames = [f for f in self._each(month, asks) if f is not None]
        return pl.concat(frames) if frames else pl.DataFrame(schema={
            "index_id": pl.Utf8, "date": pl.Date, "listing_id": pl.Utf8, "weight": pl.Float64, "segment": pl.Utf8, "source": pl.Utf8})

    def _index_bars_1d(self, start: date, end: date, **_) -> pl.DataFrame:
        frames = []
        for name, code in INDEXES.items():
            df = self.query("index_daily", "trade_date,open,high,low,close,vol,amount", ts_code=code,
                            start_date=f"{start:%Y%m%d}", end_date=f"{end:%Y%m%d}")
            frames.append(df.select(
                pl.lit(index_id("CN", name)).alias("index_id"), ymd("trade_date").alias("date"),
                *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close")],
                (pl.col("vol").cast(pl.Float64) * 100).alias("volume"),
                (pl.col("amount").cast(pl.Float64) * 1000).alias("amount"),
                pl.lit(self.name).alias("source"),
            ))
        return pl.concat(frames)

    # ---- capability probing (spec §4.9) --------------------------------------------------
    def probe(self) -> list[Probe]:
        end = date.today()
        start = end - timedelta(days=14)
        r = {"start_date": f"{start:%Y%m%d}", "end_date": f"{end:%Y%m%d}"}
        tests = {
            "daily": ("ts_code,trade_date", {"ts_code": "000001.SZ", **r}),
            "adj_factor": ("ts_code,trade_date", {"ts_code": "000001.SZ", **r}),
            "stk_limit": ("ts_code,trade_date", {"ts_code": "000001.SZ", **r}),
            "suspend_d": ("ts_code,trade_date", {"ts_code": "000001.SZ", **r}),
            "stock_basic": ("ts_code", {"ts_code": "000001.SZ"}),
            "namechange": ("ts_code,name", {"ts_code": "000001.SZ"}),
            "trade_cal": ("cal_date", {"exchange": "SSE", **r}),
            "dividend": ("ts_code,ex_date", {"ts_code": "000001.SZ"}),
            "daily_basic": ("ts_code,trade_date", {"ts_code": "000001.SZ", **r}),
            "moneyflow": ("ts_code,trade_date", {"ts_code": "000001.SZ", **r}),
            "share_float": ("ts_code,float_date", {"ts_code": "000001.SZ"}),
            "top10_floatholders": ("ts_code,holder_name", {"ts_code": "000001.SZ", "period": "20231231"}),
            "index_classify": ("index_code", {"level": "L1", "src": "SW2021"}),
            "index_member_all": ("ts_code", {"ts_code": "000001.SZ"}),
            "index_daily": ("trade_date", {"ts_code": "000300.SH", **r}),
            "index_weight": ("con_code", {"index_code": "000300.SH", **r}),
            **{api: ("ts_code", {"period": "20231231", "ts_code": "600000.SH"})   # 财务 by period (VIP), one stock to ask
               for api in ("income_vip", "balancesheet_vip", "cashflow_vip", "fina_indicator_vip")},
        }
        def ask(item):
            api, (fields, params) = item
            try:
                self.query(api, fields, **params)
                return api, None
            except TushareError as e:
                return api, e.msg
        answers = self._each(ask, tests.items())
        ok, why = {api: msg is None for api, msg in answers}, {api: msg for api, msg in answers if msg is not None}
        # 120 points: 50/min. 2000+: 200/min, 5000+: 500/min; the two cannot be told apart by which
        # APIs answer, so start at 500 and let "每分钟最多访问该接口N次" lower it (see _call).
        rate = 50 if not ok["adj_factor"] else 500
        out = []
        for ds, (required, optional) in DATASET_APIS.items():
            avail = all(ok[a] for a in required)
            missing = [a for a in optional if not ok[a]]
            detail = "; ".join([f"{a}: {why[a]}" for a in required if not ok[a]]
                               + ([f"optional apis unavailable, their columns stay null: {missing}"] if missing else []))
            out.append(Probe(ds, avail, rate_per_min=rate, detail=detail))
        return out

    def ready_count(self, day: date, instruments: pl.DataFrame) -> tuple[int, int]:
        n = self.query("daily", "ts_code", trade_date=f"{day:%Y%m%d}").height
        return n, listed_on(instruments, day).height
