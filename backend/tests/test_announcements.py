"""公告 (spec §10.3 evidence): an A-share's announcements from 巨潮资讯's public query — titles, days (China gives no time),
PDF addresses — asked for a stock and kept here by id; a source kept from storing here (§4.10) is never asked."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from wb.data.http import HttpClient
from wb.docs.announcements import Cninfo, kept, refresh

LIST = {"stockList": [{"code": "600519", "orgId": "gssh0600519", "zwjc": "贵州茅台"}]}


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


def ann(i, day, title):
    return {"announcementId": str(i), "announcementTitle": title, "announcementTime": ms(day),
            "adjunctUrl": f"finalpage/{day}/{i}.PDF", "adjunctType": "PDF", "adjunctSize": 100}


def source(pages, calls=None, pdf=b""):
    def handler(req: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(req.content.decode())) if req.method == "POST" else {}
        if calls is not None:
            calls.append((req.method, req.url.path, form))
        if req.url.path.endswith("szse_stock.json"):
            return httpx.Response(200, json=LIST)
        if req.url.path.startswith("/finalpage/"):                              # the PDF itself
            return httpx.Response(200, content=pdf)
        page = int(form["pageNum"])
        return httpx.Response(200, json={"announcements": pages[page - 1], "hasMore": page < len(pages),
                                         "totalAnnouncement": sum(map(len, pages))})
    return Cninfo(HttpClient(10_000, transport=httpx.MockTransport(handler), sleep=lambda s: None))


PAGES = [[ann(2, "2026-08-15", "贵州茅台2026年半年度报告"), ann(1, "2026-08-15", "贵州茅台关于会计政策变更的公告")],
         [ann(0, "2026-07-01", "贵州茅台2025年年度权益分派实施公告")]]


def test_a_stocks_announcements_come_page_by_page_with_their_days_and_addresses():
    calls = []
    df = source(PAGES, calls).announcements("CN:600519", date(2026, 7, 1), date(2026, 10, 4))
    assert df["doc_id"].to_list() == ["2", "1", "0"] and df["day"].to_list()[0] == date(2026, 8, 15)
    assert df["url"][0] == "https://static.cninfo.com.cn/finalpage/2026-08-15/2.PDF" and df["title"][0] == "贵州茅台2026年半年度报告"
    form = calls[1][2]
    assert form["stock"] == "600519,gssh0600519" and form["seDate"] == "2026-07-01~2026-10-04" and form["pageNum"] == "1"


def test_the_kept_announcements_grow_by_id_and_are_read_back_by_day(paths):
    assert refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), source(PAGES)) == 3
    more = [[ann(3, "2026-09-30", "贵州茅台关于回购股份的公告"), *PAGES[0]], PAGES[1]]
    assert refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), source(more)) == 1      # only the new one
    got = kept(paths, "CN:600519", date(2026, 8, 1), date(2026, 10, 4))
    assert got["doc_id"].to_list() == ["3", "2", "1"]                                               # newest first, in range


def test_a_source_kept_from_storing_here_is_never_asked(paths):
    from wb.workspace.store import Workspace
    Workspace(paths).set_setting("datause.cninfo.store", False)
    calls = []
    assert refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), source(PAGES, calls)) == 0 and calls == []



def test_an_announcements_text_is_read_once_and_kept_and_the_periodic_reports_are_not_read(paths):
    # spec §10.3 文档片段: the newest announcements' text, read from their PDFs; a periodic report is long and its
    # figures are in the financials already
    from tests.test_docs_pdf import make_pdf
    from wb.docs.announcements import fetch_texts, for_text, text_of
    from wb.workspace.store import Workspace
    calls = []
    src = source(PAGES, calls, pdf=make_pdf("Board approved the buyback"))
    refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), src)
    rows = for_text(kept(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4)))
    assert rows["doc_id"].to_list() == ["1", "0"]                                  # not 2: the half-year report
    assert fetch_texts(paths, rows, src) == [] and text_of(paths, "1") == "Board approved the buyback\f"
    asked = len(calls)
    assert fetch_texts(paths, rows, src) == [] and len(calls) == asked             # kept: never asked again
    broken = source(PAGES, pdf=b"broken")
    errors = fetch_texts(paths, for_text(kept(paths, "CN:600519", date(2026, 8, 1), date(2026, 8, 31)), 5), broken)
    assert errors == [] and text_of(paths, "2") is None                            # (only the report that day: not read)
    more = [[ann(3, "2026-09-30", "贵州茅台关于回购股份的公告"), *PAGES[0]], PAGES[1]]
    refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), source(more))
    errors = fetch_texts(paths, for_text(kept(paths, "CN:600519", date(2026, 9, 1), date(2026, 10, 4))), broken)
    assert len(errors) == 1 and "回购" in errors[0] and text_of(paths, "3") is None     # said, and tried again next time
    Workspace(paths).set_setting("datause.cninfo.store", False)
    calls.clear()
    assert fetch_texts(paths, for_text(kept(paths, "CN:600519", date(2026, 9, 1), date(2026, 10, 4))), src) == [] and calls == []



def test_a_code_the_source_does_not_list_is_said_never_taken_for_no_announcements():
    # 北交所 and new listings: an empty answer would read as "nothing announced"; it is a fetch that did not happen
    import pytest
    with pytest.raises(LookupError, match="600000"):
        source(PAGES).announcements("CN:600000", date(2026, 7, 1), date(2026, 10, 4))


def test_an_announcement_repeated_across_pages_is_kept_once(paths):
    pages = [[ann(2, "2026-08-15", "半年度报告"), ann(1, "2026-08-15", "会计政策变更")], [ann(1, "2026-08-15", "会计政策变更"), ann(0, "2026-07-01", "权益分派")]]
    assert refresh(paths, "CN:600519", date(2026, 6, 1), date(2026, 10, 4), source(pages)) == 3
    assert kept(paths, "CN:600519", date(2026, 6, 1), date(2026, 10, 4))["doc_id"].to_list() == ["2", "1", "0"]



def test_an_id_that_is_not_a_number_never_becomes_a_file_name():
    pages = [[ann("../../evil", "2026-08-15", "x"), ann(1, "2026-08-15", "会计政策变更")]]
    assert source(pages).announcements("CN:600519", date(2026, 7, 1), date(2026, 10, 4))["doc_id"].to_list() == ["1"]


def test_a_scanned_announcement_is_said_to_have_no_text_and_is_not_read_again(paths):
    from tests.test_docs_pdf import make_pdf
    from wb.docs.announcements import fetch_texts, for_text, text_of
    calls = []
    src = source([[ann(5, "2026-09-01", "律师意见书")]], calls, pdf=make_pdf(""))
    refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), src)
    rows = for_text(kept(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4)))
    errors = fetch_texts(paths, rows, src)
    assert len(errors) == 1 and "律师意见书" in errors[0] and "没有文字" in errors[0] and text_of(paths, "5") is not None
    asked = len(calls)
    assert fetch_texts(paths, rows, src) == [] and len(calls) == asked


def test_two_reads_of_one_stock_at_once_both_come_through(paths, monkeypatch):
    # 取最新公告 and 开始研究 (or the daily job) on the same stock: each writer has its own temporary file
    import threading
    from pathlib import Path

    from tests.test_docs_pdf import make_pdf
    from wb.docs.announcements import fetch_texts, for_text, text_of
    src = source(PAGES, pdf=make_pdf("Board approved the buyback"))
    refresh(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4), src)
    rows = for_text(kept(paths, "CN:600519", date(2026, 7, 1), date(2026, 10, 4)), 1)
    both, real, got = threading.Barrier(2), Path.write_text, []

    def write_then_wait(self, *a, **k):        # both have written before either moves its file into place
        n = real(self, *a, **k)
        both.wait(10)
        return n
    monkeypatch.setattr(Path, "write_text", write_then_wait)
    runs = [threading.Thread(target=lambda: got.append(fetch_texts(paths, rows, src))) for _ in range(2)]
    for t in runs:
        t.start()
    for t in runs:
        t.join(60)
    assert got == [[], []] and text_of(paths, "1").startswith("Board approved")
