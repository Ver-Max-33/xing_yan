"""股票资料 · 公司 (spec §10.3): who the company is, how big, who holds it and what to watch — for a person to read, each
part led by one line that says it. A-shares from Tushare (each API tried on your account 2026-10-07: stock_company,
fina_mainbz, top10_holders, top10_floatholders, stk_holdernumber, hk_hold, pledge_stat, share_float, stk_holdertrade,
fina_indicator; fund_portfolio cannot be asked by stock), asked when a stock's 公司 tab is opened and kept until the next
day (`<CN market dir>/profile/<ts_code>.json`). Shown as of a day: only what was announced by it (a report's holders from
its announcement day). What a source may not keep here (spec §4.10) is not asked. Japanese companies wait for EDINET (U10)."""
import json
import re
from datetime import date, datetime, timedelta

import httpx
import polars as pl

from wb.config import Paths
from wb.data.providers.tushare import TushareError
from wb.files import move_into_place, tmp_for
from wb.listing import parse_id

H = "ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio,hold_float_ratio,hold_change,holder_type"
APIS = {
    "stock_company": "ts_code,com_name,chairman,manager,reg_capital,setup_date,province,city,introduction,website,employees,main_business",
    "fina_mainbz": "ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost,curr_type",
    "top10_holders": H,
    "top10_floatholders": H,
    "stk_holdernumber": "ts_code,ann_date,end_date,holder_num",
    "hk_hold": "code,trade_date,ts_code,name,vol,ratio,exchange",
    "pledge_stat": "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio",
    "share_float": "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type",
    "stk_holdertrade": "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_ratio,avg_price,begin_date,close_date",
    "fina_indicator": "ts_code,ann_date,end_date,grossprofit_margin,netprofit_margin,roe,or_yoy,netprofit_yoy,debt_to_assets",
}
NAMES = {"stock_company": "公司概况", "fina_mainbz": "主营构成", "top10_holders": "前十大股东", "top10_floatholders": "前十大流通股东",
         "stk_holdernumber": "股东户数", "hk_hold": "陆股通持股", "pledge_stat": "股权质押", "share_float": "限售解禁",
         "stk_holdertrade": "股东增减持", "fina_indicator": "财务指标"}
SUBTOTALS = {"主营业务", "其他业务", "产品", "主营业务收入", "其他业务收入"}
NOT_A_LINE = re.compile(r"合计|小计|总计|调整|抵销|抵消|分部间")   # 茅台 2026 半年报's 合计特别调整 is not a business

# holder groups: Tushare's 25 holder types (seen on 7,610 rows of 2026-08 reports) and, for 外资 and 国资, the name
PUBLIC_FUNDS = {"开放式投资基金", "封闭式投资基金"}   # domestic funds, the joint-venture names too (国投瑞银, 交银施罗德)
PENSION = {"社保基金、社保机构", "基本养老保险基金", "企业年金"}
INSURANCE = {"保险投资组合", "金融机构—保险公司", "保险资管产品"}
PRIVATE = {"其他金融产品", "基金专户理财", "券商集合资产管理计划", "信托公司集合信托计划", "信托公司单一证券信托"}
FINANCIAL = {"金融机构—证券公司", "金融机构—银行", "金融机构—信托公司", "金融机构—金融租赁公司", "财务公司"}
# 外资 by the name (10-19, 深赛格's person LISHERYNZHANMING counted): a word that says so; where it is registered — a country at
# the start or in brackets (韩国银行, 国民年金公团(韩国); not 华美国际, 施罗德国企); Latin letters — beside Chinese words only with
# a foreign company's own form (J.P.MorganSecuritiesPLC-自有资金; not OPPO广东移动通信有限公司, nor a bond's code 26GXKEB1)
FOREIGN_WORDS = re.compile(r"QFII|香港|澳门|科威特|挪威|阿布达比|阿布扎比|新加坡|淡马锡|瑞银|高盛|摩根|巴克莱|花旗|美林|汇丰|野村|麦格理|德意志|法国巴黎|株式会社")
COUNTRIES = "韩国|日本|瑞士|英国|美国|法国|德国|荷兰|西班牙|意大利|加拿大|澳大利亚|瑞典|丹麦|卢森堡|爱尔兰|比利时|马来西亚|卡塔尔|沙特|阿联酋|新西兰|芬兰"
DOMICILE = re.compile(rf"^(?:{COUNTRIES})|[（(](?:{COUNTRIES})[)）]")
LATIN, CJK = re.compile(r"[A-Za-z]{4,}"), re.compile(r"[\u4e00-\u9fff]")
ENTITY = re.compile(r"PLC|INC[.,)]|INC$|LTD|LIMITED|LLC|L\.P\.|S\.A\.|GMBH|FUNDS?\b|COMPANY|CORP", re.I)
NOMINEE = re.compile(r"HKSCC\s*NOMINEES|香港中央结算\s*[（(]?代理人", re.I)   # holds the H shares for their owners (brackets or not)
STATE = re.compile(r"国有资产|国资委|人民政府|财政局|财政厅|财政部|汇金|中国证券金融|国新发展|国新投资|中国诚通|国务院")


def foreign_name(n: str) -> bool:
    """A name that says 外资 (see FOREIGN_WORDS above); a person's never counts — group_of asks the type first."""
    if FOREIGN_WORDS.search(n) or DOMICILE.search(n):
        return True
    return bool(LATIN.search(n)) and (not CJK.search(n) or bool(ENTITY.search(n)))


def group_of(name: str | None, kind: str | None) -> str:
    """A holder's group for the summary: 个人 / 国资 / 企业 / 公募基金 / 社保养老 / 保险 / 私募资管 / 外资 / 金融机构, and
    H股代持 — an A+H company's H shares held by the nominee for their owners, whoever they are."""
    n, k = name or "", kind or ""
    if NOMINEE.search(n):
        return "H股代持"
    if "香港中央结算" in n:          # the 陆股通 (Northbound) account
        return "外资"
    if k == "自然人":                # a person, whatever the script or words of the name (LISHERYNZHANMING, 陈美林): never 外资
        return "个人"
    if "证券投资基金" in n and "私募" not in n:   # a public fund's name, whatever the type says (指数分级基金 as 其他金融产品)
        return "公募基金"
    for kinds, g in ((PUBLIC_FUNDS, "公募基金"), (PENSION, "社保养老"), (INSURANCE, "保险")):
        if k in kinds:
            return g
    # a domestic fund company's product keeps its own group, whatever its name (摩根士丹利华鑫基金-…); a foreign one's fund does not
    # (首域投资管理(英国)有限公司-首域中国A股基金, 瑞典第二国家养老基金)
    if foreign_name(n) and not ("基金" in n and not (re.search(r"QFII|香港", n) or DOMICILE.search(n))):
        return "外资"
    if k == "国资局" or STATE.search(n):
        return "国资"
    if k in PRIVATE:
        return "私募资管"
    if k in FINANCIAL:
        return "金融机构"
    return "企业"


# ---- fetching and keeping ----------------------------------------------------------------------------------------
def _file(paths: Paths, ts_code: str):
    return paths.market_dir("CN") / "profile" / f"{ts_code}.json"


def _params(api: str, ts_code: str, today: date) -> dict:
    if api == "fina_mainbz":
        return {"ts_code": ts_code, "type": "P"}   # by product
    if api == "stk_holdertrade":                  # it answers nothing without a date range
        return {"ts_code": ts_code, "start_date": f"{today - timedelta(days=3 * 366):%Y%m%d}", "end_date": f"{today:%Y%m%d}"}
    return {"ts_code": ts_code}


def fetch(paths: Paths, ts_code: str, source=None, refresh: bool = False, now: datetime | None = None) -> dict:
    """{"fetched_at", "rows": {api: [records]}, "errors": {api: message}} — asked once a day (or when you say)."""
    now = now or datetime.now()
    f = _file(paths, ts_code)
    if not refresh and f.exists():
        kept = json.loads(f.read_text(encoding="utf-8"))
        if datetime.fromisoformat(kept["fetched_at"]).date() == now.date():
            return kept
    if source is None:
        from wb.data.providers.tushare import Tushare
        source = Tushare("CN")
    rows, errors = {}, {}
    for api, fields in APIS.items():
        try:
            rows[api] = source.query(api, fields, **_params(api, ts_code, now.date())).to_dicts()
        except TushareError as e:
            errors[api] = e.msg
        except (httpx.HTTPError, OSError) as e:
            errors[api] = f"网络：{e}"
    kept = {"fetched_at": now.isoformat(timespec="seconds"), "rows": rows, "errors": errors}
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_for(f)
    tmp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    move_into_place(tmp, f)
    return kept


# ---- reading it as of a day --------------------------------------------------------------------------------------
def _d(v) -> date | None:
    s = str(v or "")
    return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) >= 8 and s[:8].isdigit() else None


def _frame(rows: list[dict] | None, dates: tuple[str, ...]) -> pl.DataFrame:
    df = pl.DataFrame(rows or [], infer_schema_length=None)
    return df.with_columns([pl.col(c).map_elements(_d, return_dtype=pl.Date).alias(c) for c in dates if c in df.columns])


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


PERIOD = {3: "一季报", 6: "半年报", 9: "三季报", 12: "年报"}


def period_name(d: date) -> str:
    return f"{d.year} {PERIOD.get(d.month, d.isoformat())}"


def _holders(df: pl.DataFrame, as_of: date, ratio: str) -> dict | None:
    known = df.filter(pl.col("ann_date") <= as_of) if df.height else df
    if not known.height:
        return None
    ends = sorted(set(known["end_date"].to_list()), reverse=True)
    def latest(end):
        g = known.filter(pl.col("end_date") == end)
        return g.filter(pl.col("ann_date") == g["ann_date"].max())   # a restatement replaces the first
    now = latest(ends[0])
    before = latest(ends[1]) if len(ends) > 1 else None
    prev = set(before["holder_name"].to_list()) if before is not None else None
    rows = []
    for r in now.sort(ratio, descending=True, nulls_last=True).iter_rows(named=True):
        ch = _num(r.get("hold_change")) or 0.0
        state = ("新进" if prev is not None and r["holder_name"] not in prev else
                 "增持" if ch > 0 else "减持" if ch < 0 else "不变")
        rows.append({"name": r["holder_name"], "type": r.get("holder_type"), "group": group_of(r["holder_name"], r.get("holder_type")),
                     "ratio": _num(r.get(ratio)), "shares": _num(r.get("hold_amount")), "change": ch, "state": state})
    left = sorted(prev - set(now["holder_name"].to_list())) if prev is not None else []
    return {"period": _iso(ends[0]), "known": _iso(now["ann_date"].max()), "rows": rows, "left": left,
            "sum": sum(r["ratio"] or 0 for r in rows)}


def _segments(mainbz: pl.DataFrame, ind: pl.DataFrame, as_of: date) -> dict | None:
    if not mainbz.height:
        return None
    ann = {r["end_date"]: r["ann_date"] for r in ind.sort("ann_date").iter_rows(named=True)} if ind.height else {}
    real = mainbz.filter(~pl.col("bz_item").is_in(list(SUBTOTALS)) & ~pl.col("bz_item").str.contains(NOT_A_LINE.pattern)
                         & pl.col("bz_sales").is_not_null())
    for end in sorted(set(real["end_date"].to_list()), reverse=True):
        known = ann.get(end) or (end + timedelta(days=120))   # the report's day; else the latest a report may come out
        g = real.filter(pl.col("end_date") == end)
        if known > as_of or g.height < 2:
            continue
        total = float(g["bz_sales"].sum())
        items = [{"name": r["bz_item"], "sales": _num(r["bz_sales"]), "share": _num(r["bz_sales"]) / total if total else None,
                  "margin": (_num(r["bz_profit"]) / _num(r["bz_sales"])) if _num(r["bz_sales"]) and r.get("bz_profit") is not None else None}
                 for r in g.sort("bz_sales", descending=True).iter_rows(named=True)]
        return {"period": _iso(end), "known": _iso(known), "items": items}
    return None


def _valuation(paths: Paths, listing_id: str, as_of: date) -> dict | None:
    from wb.data.store import Store
    st = Store(paths, "CN")
    snap = st.latest()
    if snap is None or not snap.manifest.get("valuation"):
        return None
    v = (st.scan("valuation", snap).filter((pl.col("listing_id") == listing_id) & (pl.col("date") <= as_of))
         .sort("date").tail(1).collect())
    if not v.height:
        return None
    r = v.row(0, named=True)
    return {"date": _iso(r["date"]), **{k: r.get(k) for k in ("total_mv", "circ_mv", "pe_ttm", "pb", "dv_ttm", "total_share", "float_share")}}


def _industry(paths: Paths, listing_id: str, as_of: date) -> tuple[str | None, str | None]:
    """(申万一级, 申万二级) on `as_of` — the spells the rules read (wb.market.industry)."""
    from wb.data.store import Store
    from wb.market.industry import at
    st = Store(paths, "CN")
    got = at(st, st.latest(), "CN", as_of).filter(pl.col("listing_id") == listing_id)
    return (got["l1"][0], got["l2"][0]) if got.height else (None, None)


def _ts_code(paths: Paths, listing_id: str) -> str:
    """The Tushare code: the exchange from the instruments list, else from the code (6/9 上交所, 0/2/3 深交所, others 北交所)."""
    from wb.data.store import Store
    _, code = parse_id(listing_id)
    st = Store(paths, "CN")
    snap = st.latest()
    if snap is not None:
        ex = st.scan("instruments", snap).filter(pl.col("listing_id") == listing_id).select("exchange").collect()
        suffix = {"XSHG": "SH", "XSHE": "SZ", "BJSE": "BJ"}.get(ex["exchange"][0] if ex.height else "", None)
        if suffix:
            return f"{code}.{suffix}"
    return f"{code}.{'SH' if code[0] in '69' else 'SZ' if code[0] in '023' else 'BJ'}"


def _region(province: str | None, city: str | None) -> str | None:
    """上海 / 上海市 → 上海; 广东 / 深圳市 → 广东 深圳市."""
    if not province:
        return city
    return province if not city or city in (province, province + "市") else f"{province} {city}"


def _yi(v: float | None) -> str:
    return "—" if v is None else f"{v / 1e8:,.2f} 亿" if abs(v) >= 1e8 else f"{v / 1e4:,.0f} 万"


def profile(paths: Paths, listing_id: str, as_of: date, source=None, refresh: bool = False, now: datetime | None = None) -> dict:
    """The company profile as of `as_of`: company, scale, holders, risk, finance, a one-line summary of each, and the parts
    that could not be had (errors, by part)."""
    market, _ = parse_id(listing_id)
    base = {"listing_id": listing_id, "as_of": as_of.isoformat(), "source": "Tushare", "unavailable": None, "errors": {}}
    if market != "CN":
        return {**base, "unavailable": "日股的公司和股东资料来自 EDINET（日本金融厅的法定披露系统），还没有接入"}
    from wb.datause import allowed
    from wb.workspace.store import Workspace
    if not allowed(Workspace(paths).settings(), "tushare", "store"):
        return {**base, "unavailable": "数据使用开关里 Tushare 不允许存在本地，所以没有去取（设置 → 数据使用）"}
    raw = fetch(paths, _ts_code(paths, listing_id), source, refresh, now)
    rows, errors = raw["rows"], {NAMES[k]: v for k, v in raw["errors"].items()}
    fr = {api: _frame(rows.get(api), ("ann_date", "end_date", "trade_date", "float_date", "setup_date", "begin_date", "close_date"))
          for api in APIS}

    co = fr["stock_company"].row(0, named=True) if fr["stock_company"].height else {}
    ind = fr["fina_indicator"].filter(pl.col("ann_date") <= as_of) if fr["fina_indicator"].height else fr["fina_indicator"]
    company = {"name": co.get("com_name"), "main_business": co.get("main_business"), "introduction": co.get("introduction"),
               "region": _region(co.get("province"), co.get("city")),
               "setup_date": _iso(co.get("setup_date")) if co else None, "employees": co.get("employees"), "chairman": co.get("chairman"),
               "website": co.get("website"), **dict(zip(("industry", "industry2"), _industry(paths, listing_id, as_of), strict=True)),
               "segments": _segments(fr["fina_mainbz"], fr["fina_indicator"], as_of)}

    hn = fr["stk_holdernumber"]
    numbers = []
    if hn.height:
        k = hn.filter(pl.col("ann_date") <= as_of).sort("end_date", "ann_date").unique("end_date", keep="last", maintain_order=True).tail(8)
        prev = None
        for r in k.iter_rows(named=True):
            n = int(r["holder_num"]) if r["holder_num"] is not None else None
            numbers.append({"period": _iso(r["end_date"]), "known": _iso(r["ann_date"]), "number": n,
                            "change": (n / prev - 1) if n and prev else None})
            prev = n
    hk = fr["hk_hold"]
    hk_now = None
    if hk.height:
        k = hk.filter(pl.col("trade_date") <= as_of).sort("trade_date")
        if k.height:
            last = k.row(-1, named=True)
            hk_now = {"date": _iso(last["trade_date"]), "ratio": _num(last["ratio"]), "shares": _num(last["vol"]),
                      "series": [{"date": _iso(r["trade_date"]), "ratio": _num(r["ratio"])} for r in k.tail(8).iter_rows(named=True)]}
    float10 = _holders(fr["top10_floatholders"], as_of, "hold_float_ratio")
    top10 = _holders(fr["top10_holders"], as_of, "hold_ratio")
    groups: dict[str, float] = {}
    for r in (float10 or {}).get("rows", []):
        if r["ratio"] is not None:   # an H-share nominee has no share of the A float
            groups[r["group"]] = groups.get(r["group"], 0.0) + r["ratio"]
    named_foreign = [{"name": r["name"], "ratio": r["ratio"]} for r in (float10 or {}).get("rows", []) if r["group"] == "外资"]
    holders = {"top10": top10, "float10": float10, "groups": groups, "numbers": numbers,
               "foreign": {"hk": hk_now, "named": named_foreign}}

    pl_ = fr["pledge_stat"]
    pledge = None
    if pl_.height:
        k = pl_.filter(pl.col("end_date") <= as_of).sort("end_date")
        if k.height:
            r = k.row(-1, named=True)
            pledge = {"date": _iso(r["end_date"]), "ratio": _num(r["pledge_ratio"]), "count": r.get("pledge_count")}
    sf = fr["share_float"]
    unlocks = []
    if sf.height:
        k = sf.filter((pl.col("ann_date") <= as_of) & (pl.col("float_date") > as_of) & (pl.col("float_date") <= as_of + timedelta(days=90)))
        for (d,), g in sorted(k.group_by("float_date"), key=lambda x: x[0][0]):
            unlocks.append({"date": _iso(d), "shares": float(g["float_share"].cast(pl.Float64).sum()),
                            "ratio": float(g["float_ratio"].cast(pl.Float64).sum()), "holders": g.height,
                            "types": sorted(set(g["share_type"].drop_nulls().to_list()))})
    tr = fr["stk_holdertrade"]
    trades = []
    if tr.height:
        k = tr.filter((pl.col("ann_date") <= as_of) & (pl.col("ann_date") > as_of - timedelta(days=365))).sort("ann_date", descending=True)
        kinds = {"G": "高管", "P": "个人股东", "C": "公司股东"}
        trades = [{"ann_date": _iso(r["ann_date"]), "holder": r["holder_name"], "holder_type": kinds.get(r.get("holder_type"), r.get("holder_type")),
                   "direction": "增持" if r.get("in_de") == "IN" else "减持", "shares": _num(r.get("change_vol")),
                   "ratio": _num(r.get("change_ratio")), "after_ratio": _num(r.get("after_ratio")), "avg_price": _num(r.get("avg_price")),
                   "from": _iso(r.get("begin_date")), "to": _iso(r.get("close_date"))} for r in k.iter_rows(named=True)]
    risk = {"pledge": pledge, "unlocks": unlocks, "trades": trades}

    finance = None
    if ind.height:
        r = ind.sort("end_date", "ann_date").row(-1, named=True)
        finance = {"period": _iso(r["end_date"]), "period_name": period_name(r["end_date"]), "known": _iso(r["ann_date"]),
                   "gross_margin": _num(r.get("grossprofit_margin")), "net_margin": _num(r.get("netprofit_margin")),
                   "roe": _num(r.get("roe")), "revenue_yoy": _num(r.get("or_yoy")), "profit_yoy": _num(r.get("netprofit_yoy")),
                   "debt_to_assets": _num(r.get("debt_to_assets"))}
    scale = _valuation(paths, listing_id, as_of)
    out = {**base, "fetched_at": raw["fetched_at"], "errors": errors, "company": company, "scale": scale, "holders": holders,
           "risk": risk, "finance": finance}
    return {**out, "summary": summary(out)}


def _pct(v: float | None, signed: bool = False, digits: int = 1) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def summary(p: dict) -> dict[str, str]:
    """One line for each part, from its figures (no AI): what a person would say first."""
    out = {}
    c = p["company"]
    seg = c.get("segments")
    lead = (c.get("main_business") or c.get("introduction") or "").strip().rstrip("。")
    if len(lead) > 60:
        lead = lead[:60] + "…"
    if seg and seg["items"]:
        top = seg["items"][0]
        lead = f"{lead}；收入 {top['share']:.0%} 来自{top['name']}（{period_name(date.fromisoformat(seg['period']))}）" if lead else \
            f"收入 {top['share']:.0%} 来自{top['name']}"
    out["company"] = lead or "没有取到公司概况"
    s = p["scale"]
    if s:
        parts = [f"总市值 {_yi(s.get('total_mv'))}", f"流通市值 {_yi(s.get('circ_mv'))}"]
        if s.get("pe_ttm") is not None:
            parts.append(f"市盈率(TTM) {s['pe_ttm']:.1f}")
        elif s.get("total_mv"):
            parts.append("市盈率(TTM)为负：近四个季度合计亏损")
        if s.get("pb") is not None:
            parts.append(f"市净率 {s['pb']:.2f}")
        if s.get("dv_ttm"):
            parts.append(f"股息率 {s['dv_ttm']:.2f}%")
        out["scale"] = "，".join(parts) + f"（{s['date']}）"
    else:
        out["scale"] = "没有估值数据"
    h = p["holders"]
    bits = []
    if h["top10"] and h["top10"]["rows"]:
        rows = h["top10"]["rows"]
        r = next((x for x in rows if x["group"] != "H股代持"), rows[0])
        nominee = next((x for x in rows if x["group"] == "H股代持"), None)
        note = f"；H 股代持账户 HKSCC 合计 {_pct(nominee['ratio'], digits=2)} 不计" if nominee and r is not nominee else ""
        bits.append(f"第一大股东 {r['name']}（{_pct(r['ratio'], digits=2)}{note}）")
    f10 = h["float10"]
    if f10:
        g = sorted(h["groups"].items(), key=lambda x: -x[1])[:3]
        bits.append(f"前十大流通股东合计 {_pct(f10['sum'])}（{'、'.join(f'{k} {v:.1f}%' for k, v in g)}）")
    hk, named = h["foreign"]["hk"], h["foreign"]["named"]
    if hk or named:
        fb = []
        if hk:
            fb.append(f"陆股通持有 A 股的 {_pct(hk['ratio'], digits=2)}（{hk['date']} 港交所披露）")
        others = [x for x in named if "香港中央结算" not in x["name"]]
        if others:
            fb.append(f"前十大流通股东里另有外资 {len(others)} 家，合计占流通股 {_pct(sum(x['ratio'] or 0 for x in others), digits=2)}")
        bits.append("外资：" + "，".join(fb))
    else:
        bits.append("没有看到外资（陆股通和前十大流通股东里都没有）")
    known = [x for x in h["numbers"] if x["number"] is not None]   # Tushare may list a period without its number (10-19)
    if known:
        n, ch = known[-1], known[-1]["change"]
        when = "" if n is h["numbers"][-1] else f"（{n['period']}，之后的没有公布）"
        bits.append(f"股东户数 {n['number']:,}{when}" + (f"，比上期 {ch:+.1%}（{'筹码在集中' if ch < 0 else '筹码在分散'}）" if ch is not None else ""))
    out["holders"] = "；".join(bits) if bits else "没有取到股东资料"
    r = p["risk"]
    rb = [f"质押 {_pct(r['pledge']['ratio'], digits=2)}（{r['pledge']['date']}）" if r["pledge"] and r["pledge"]["ratio"] else "没有股权质押"]
    if r["unlocks"]:
        u = r["unlocks"][0]
        more = f"，之后还有 {len(r['unlocks']) - 1} 批" if len(r["unlocks"]) > 1 else ""
        rb.append(f"{u['date']} 解禁 {u['shares'] / 1e4:,.0f} 万股（占总股本 {_pct(u['ratio'], digits=2)}）{more}")
    else:
        rb.append("未来 90 天没有限售解禁")
    if r["trades"]:
        ins = sum(1 for t in r["trades"] if t["direction"] == "增持")
        rb.append(f"近一年股东和高管增持 {ins} 笔、减持 {len(r['trades']) - ins} 笔")
    else:
        rb.append("近一年没有股东和高管增减持记录")
    out["risk"] = "；".join(rb)
    fin = p["finance"]
    out["finance"] = (f"{fin['period_name']}：营收同比 {_pct(fin['revenue_yoy'], True)}，净利润同比 {_pct(fin['profit_yoy'], True)}，"
                      f"毛利率 {_pct(fin['gross_margin'])}，ROE {_pct(fin['roe'], digits=2)}") if fin else "没有取到财务指标"
    return out
