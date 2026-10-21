"""你导入的 IR 资料 (spec §10.3 文档片段): a document you add to a stock — a results deck, a briefing, your notes from a
call — its text read once and kept page by page (a PDF through wb.docs.pdf: in the separate process, with its limits;
a text file as UTF-8 or GB18030), the original not kept. The excerpt a research gets is yours to choose, with its page
(by default the first page with something to say), so a research's citation leads back to the original's place.
Yours: shared by the data profiles, always in the evidence pack like your notes, kept until you delete it."""
import uuid
from datetime import datetime
from pathlib import PurePath

from wb.config import Paths
from wb.db import connect
from wb.docs.pdf import MAX_BYTES, excerpt, texts
from wb.files import move_into_place, tmp_for

PAGES, CHARS = 40, 60_000   # a results deck is 20–60 pages; what a research can use is near the front
EXCERPT = 3_000              # your excerpt goes into every research's prompt


def _db(paths: Paths):
    return connect(paths.shared_db, "shared")


def _file(paths: Paths, rid: str):
    return paths.root / "docs" / "ir" / f"{rid}.txt"


def _decode(data: bytes) -> str:
    """A text file's own encoding: UTF-8 (a BOM is not text); else GB18030 or, for Japanese, Shift_JIS or EUC-JP —
    every Shift_JIS or EUC-JP byte pair is also valid GB18030, so the one that reads Japanese kana (and no half-width
    katakana, which is GBK read as Shift_JIS) is taken when there is one."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    read = {}
    for enc in ("gb18030", "cp932", "euc_jp"):
        try:
            read[enc] = data.decode(enc)
        except UnicodeDecodeError:
            continue
    kana = lambda t: sum("\u3040" <= c <= "\u30ff" for c in t) - sum("\uff61" <= c <= "\uff9f" for c in t)  # noqa: E731
    jp = [e for e in ("cp932", "euc_jp") if e in read and kana(read[e]) > 0]
    if jp:
        return read[max(jp, key=lambda e: kana(read[e]))]
    if "gb18030" in read:
        return read["gb18030"]
    raise ValueError("不是 PDF，也不是 UTF-8、GBK 或日文编码的文本")


def _pages(data: bytes) -> list[str]:
    """The text page by page: a PDF's pages, or a text file's (a form feed ends a page, as pdftotext writes them)."""
    if data[:5] == b"%PDF-":
        [(kind, text)] = texts([data], pages=PAGES, chars=CHARS)
        if kind != "ok":
            raise ValueError(f"PDF 读不出来：{text}")
    elif b"\x00" not in data[:4096]:
        text = _decode(data)[:CHARS]
    else:
        raise ValueError("不是 PDF，也不是 UTF-8、GBK 或日文编码的文本")
    return text.split("\f")[:-1] if text.endswith("\f") else text.split("\f")


def add(paths: Paths, listing_id: str, filename: str, data: bytes, title: str | None = None) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError(f"文件太大（{len(data) / 1e6:.1f} MB），最多 {MAX_BYTES / 1e6:.0f} MB")
    pages = _pages(data)
    if not any(excerpt(p) for p in pages):   # page numbers alone are not text (a scanned deck)
        raise ValueError("没有读出文字（扫描件？）")
    page = next((i for i, p in enumerate(pages, 1) if len(excerpt(p)) >= 30), next(i for i, p in enumerate(pages, 1) if p.strip()))
    rid = uuid.uuid4().hex[:12]
    f = _file(paths, rid)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_for(f)
    tmp.write_text("\f".join(pages), encoding="utf-8")
    move_into_place(tmp, f)
    _db(paths).execute("INSERT INTO ir_doc VALUES (?,?,?,?,?,?,?,?,?)",
                       (rid, listing_id, title or PurePath(filename).stem, filename, len(pages), sum(map(len, pages)),
                        excerpt(pages[page - 1]), page, datetime.now().astimezone().isoformat(timespec="seconds")))
    return get(paths, rid)


def get(paths: Paths, rid: str) -> dict:
    row = _db(paths).execute("SELECT * FROM ir_doc WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise KeyError(f"没有这份资料：{rid}")
    return dict(row)


def docs(paths: Paths, listing_id: str) -> list[dict]:
    """The stock's documents, newest first."""
    return [dict(r) for r in _db(paths).execute("SELECT * FROM ir_doc WHERE listing_id=? ORDER BY rowid DESC", (listing_id,))]


def page_texts(paths: Paths, rid: str) -> list[str]:
    get(paths, rid)
    return _file(paths, rid).read_text(encoding="utf-8").split("\f")


def set_excerpt(paths: Paths, rid: str, text: str, page: int) -> dict:
    d = get(paths, rid)
    if not 1 <= page <= d["pages"]:
        raise ValueError(f"这份资料只有 {d['pages']} 页，没有第 {page} 页")
    if not text.strip():
        raise ValueError("摘录不能是空的")
    if len(text.strip()) > EXCERPT:
        raise ValueError(f"摘录最多 {EXCERPT} 字（每次研究都会带上它）")
    _db(paths).execute("UPDATE ir_doc SET excerpt=?, page=? WHERE id=?", (text.strip(), page, rid))
    return get(paths, rid)


def remove(paths: Paths, rid: str) -> None:
    get(paths, rid)
    _file(paths, rid).unlink(missing_ok=True)
    _db(paths).execute("DELETE FROM ir_doc WHERE id=?", (rid,))
