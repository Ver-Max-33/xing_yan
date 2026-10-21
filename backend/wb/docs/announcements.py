"""公告 (spec §10.3 evidence): an A-share's announcements from 巨潮资讯 (cninfo)'s public query — title, the day (China
gives no time: known from the next trading day), the PDF's address — asked when a stock is researched and kept here by
id (`<CN market dir>/docs/announcements.parquet`); the newest ones' text is read from their PDFs (wb.docs.pdf) and kept
one file each (`docs/text/<id>.txt`). Probed 2026-10-04: 6,259 stocks with their orgId; the query by stock and date range
needs no key; the PDFs' text comes out whole (pypdf, checked on 12 real announcements of three stocks). Japanese filings
(EDINET) are not here yet."""
import io
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from wb.config import Paths
from wb.data.http import HttpClient
from wb.docs.pdf import MAX_BYTES, texts
from wb.files import move_into_place, tmp_for

BASE, STATIC = "https://www.cninfo.com.cn", "https://static.cninfo.com.cn/"   # https: titles and PDFs as the site sent them
TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = {"listing_id": pl.Utf8, "doc_id": pl.Utf8, "day": pl.Date, "title": pl.Utf8, "url": pl.Utf8, "size_kb": pl.Float64,
          "source": pl.Utf8}


class Cninfo:
    name = "cninfo"

    def __init__(self, http: HttpClient | None = None):
        self.http = http or HttpClient(per_minute=20)   # a public site: asked gently
        self._org: dict[str, str] | None = None

    def org_ids(self) -> dict[str, str]:
        """Every A-share's code → the orgId the query asks for."""
        if self._org is None:
            r = self.http.request("GET", f"{BASE}/new/data/szse_stock.json")
            r.raise_for_status()
            self._org = {x["code"]: x["orgId"] for x in r.json().get("stockList", [])}
        return self._org

    def announcements(self, listing_id: str, start: date, end: date) -> pl.DataFrame:
        """The stock's announcements in [start, end], newest first."""
        code = listing_id.split(":", 1)[1]
        org = self.org_ids().get(code)
        if org is None:   # a fetch that did not happen, never "nothing announced"
            raise LookupError(f"巨潮的股票列表里没有 {code}")
        rows, page = [], 1
        while True:
            r = self.http.request("POST", f"{BASE}/new/hisAnnouncement/query", data={
                "stock": f"{code},{org}", "tabName": "fulltext", "pageSize": 30, "pageNum": page, "column": "szse",
                "category": "", "plate": "", "seDate": f"{start}~{end}", "searchkey": "", "secid": "", "sortName": "",
                "sortType": "", "isHLtitle": "true"})
            r.raise_for_status()
            j = r.json()
            for a in j.get("announcements") or []:
                if not str(a["announcementId"]).isdigit():   # it becomes a file name (docs/text/<id>.txt)
                    continue
                rows.append((listing_id, str(a["announcementId"]),
                             datetime.fromtimestamp(a["announcementTime"] / 1000, TZ).date(), a.get("announcementTitle") or "",
                             STATIC + a["adjunctUrl"] if a.get("adjunctUrl") else None, a.get("adjunctSize"), self.name))
            if not j.get("hasMore"):
                break
            page += 1
        return pl.DataFrame(rows, schema=SCHEMA, orient="row").unique("doc_id", keep="first", maintain_order=True)   # pages can repeat one


def _file(paths: Paths):
    return paths.market_dir("CN") / "docs" / "announcements.parquet"


def _read(f) -> pl.DataFrame:
    """Read through bytes: on Windows a memory-mapped file cannot be replaced while it is open."""
    return pl.read_parquet(io.BytesIO(f.read_bytes())) if f.exists() else pl.DataFrame(schema=SCHEMA)


def kept(paths: Paths, listing_id: str, start: date, end: date) -> pl.DataFrame:
    """The announcements kept here for the stock in [start, end], newest first."""
    return (_read(_file(paths)).filter((pl.col("listing_id") == listing_id) & pl.col("day").is_between(start, end))
            .sort(["day", "doc_id"], descending=True))


def refresh(paths: Paths, listing_id: str, start: date, end: date, source: Cninfo | None = None) -> int:
    """Ask the source for the stock's announcements in [start, end] and keep the new ones; how many were new. A source
    kept from storing here (spec §4.10) is never asked."""
    from wb.datause import allowed
    from wb.workspace.store import Workspace
    if not allowed(Workspace(paths).settings(), "cninfo", "store"):
        return 0
    got = (source or Cninfo()).announcements(listing_id, start, end)
    f = _file(paths)
    old = _read(f)
    new = got.join(old.select("doc_id"), on="doc_id", how="anti")
    if new.height:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = tmp_for(f)
        pl.concat([old, new]).write_parquet(tmp)
        move_into_place(tmp, f)
    return new.height


PERIODIC = re.compile(r"(年度|季度)报告")   # a periodic report (or its summary): long, and its figures are in the financials


def for_text(rows: pl.DataFrame, n: int = 5) -> pl.DataFrame:
    """The newest `n` of `rows` worth reading for an excerpt: not the periodic reports."""
    return rows.filter(~pl.col("title").str.contains(PERIODIC.pattern)).head(n)


def _text(paths: Paths, doc_id: str):
    return paths.market_dir("CN") / "docs" / "text" / f"{doc_id}.txt"


def text_of(paths: Paths, doc_id: str) -> str | None:
    f = _text(paths, doc_id)
    return f.read_text(encoding="utf-8") if f.exists() else None


def fetch_texts(paths: Paths, rows: pl.DataFrame, source: Cninfo | None = None) -> list[str]:
    """Read the PDFs of `rows` whose text is not kept yet and keep it; why each one that could not be read (it is tried
    again next time). A source kept from storing here (spec §4.10) is never asked."""
    from wb.datause import allowed
    from wb.workspace.store import Workspace
    if not allowed(Workspace(paths).settings(), "cninfo", "store"):
        return []
    todo = [r for r in rows.iter_rows(named=True) if r["url"] and not _text(paths, r["doc_id"]).exists()]
    errors, got, pdfs, src = [], [], [], source or Cninfo()
    for r in todo:
        if (r["size_kb"] or 0) * 1000 > MAX_BYTES:
            errors.append(f"{r['title']}：PDF 太大（{r['size_kb'] / 1000:.0f} MB），没读")
            continue
        try:
            resp = src.http.request("GET", r["url"])
            resp.raise_for_status()
            got.append(r)
            pdfs.append(resp.content)
        except Exception as e:  # noqa: BLE001 - offline, the site down: said; the others still come
            errors.append(f"{r['title']}：{e}")
    for r, (kind, text) in zip(got, texts(pdfs) if pdfs else [], strict=True):
        if kind != "ok":
            errors.append(f"{r['title']}：{text}")
            continue
        if not text.strip():   # kept (it is not read again), and said
            errors.append(f"{r['title']}：扫描件，没有文字")
        f = _text(paths, r["doc_id"])
        tmp = tmp_for(f)   # each writer its own: two reads at once
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            move_into_place(tmp, f)
        except OSError as e:   # a full disk, a file held open: said; read again next time
            tmp.unlink(missing_ok=True)
            errors.append(f"{r['title']}：存不下来（{e}）")
    return errors
