"""公告正文 (spec §10.3 文档片段): text out of announcement PDFs. A PDF from the internet is untrusted input: it is read
in a separate process with a time limit, only its first pages, only so much text; what cannot be read says why and never
stops the rest. The excerpt a research gets skips the header and the board's standard assurance."""
from wb.docs.pdf import excerpt, texts


def make_pdf(*pages: str) -> bytes:
    """A small PDF with one line of text on each page (Helvetica, ASCII)."""
    n = len(pages)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>".encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, t in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for k, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def test_each_pdfs_text_comes_out_in_order_and_a_broken_one_says_why():
    got = texts([make_pdf("Revenue rose 12 percent"), b"not a pdf at all", make_pdf("Second notice")])
    assert got[0] == ("ok", "Revenue rose 12 percent\f") and got[2] == ("ok", "Second notice\f")   # \f ends a page
    assert got[1][0] == "error" and got[1][1]


def test_only_the_first_pages_and_so_much_text_are_read():
    [(ok, text)] = texts([make_pdf("page one", "page two", "page three")], pages=2)
    assert ok == "ok" and "page two" in text and "page three" not in text
    [(_, short)] = texts([make_pdf("A" * 50)], chars=10)
    assert short == "A" * 10


def test_a_pdf_too_big_is_never_parsed_and_a_slow_one_is_stopped():
    [(kind, why)] = texts([b"%PDF-" + b"0" * 64], max_bytes=32)
    assert kind == "error" and "太大" in why
    got = texts([make_pdf("x"), make_pdf("y")], timeout=0.001)               # no time to start: both say so
    assert [k for k, _ in got] == ["error", "error"] and all("超时" in w for _, w in got)


SSE = """1
证券简称：贵州茅台 证券代码：600519 编号：临 2026－030
贵州茅台酒股份有限公司
关于会计政策变更的公告
本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者
重大遗漏，并对其内容的真实性、准确性和完整性承担法律责任。
重要内容提示：
●贵州茅台酒股份有限公司（以下简称“公司”）根据财政部发布的
相关规定变更会计政策，不会对公司财务状况产生重大影响。
2
"""
SZSE = """证券代码：000001 证券简称：平安银行 公告编号：2026-035
平安银行股份有限公司 2026 年半年度权益分派实施公告
本公司及董事会全体成员保证信息披露的内容真实、准确、完整，没有虚假记载、误导
性陈述或重大遗漏。
平安银行股份有限公司 2026 年半年度权益分派
方案已获 2026 年 8 月 20 日召开的股东大会审议通过，每 10 股派 2.36 元。
"""


def test_the_excerpt_skips_the_header_and_the_standard_assurance_and_joins_the_lines():
    e = excerpt(SSE)
    assert e.startswith("重要内容提示：●贵州茅台酒股份有限公司（以下简称“公司”）根据财政部发布的相关规定") and "保证" not in e
    assert not e.endswith("2")                                                # page numbers are not text
    z = excerpt(SZSE)
    assert z.startswith("平安银行股份有限公司 2026 年半年度权益分派方案已获") and "每 10 股派 2.36 元" in z
    assert len(excerpt("正文" * 1000, size=100)) <= 101                       # 100 characters and an ellipsis


def test_spaces_the_pdf_put_between_chinese_characters_are_taken_out():
    # real: 平安银行 2026-08-22 「高 鹏 先 生 的 简 历 请 见 本 行 于 2026 年 1 月 27 日」
    assert excerpt("高 鹏 先 生 的 简 历 请 见 本 行 于 2026 年 1 月 27 日") == "高鹏先生的简历请见本行于 2026 年 1 月 27 日"


def _pdf(objs: list[bytes]) -> bytes:
    out, offsets = b"%PDF-1.4\n", []
    for k, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _stream(d: bytes, data: bytes) -> bytes:
    return b"<< " + d + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _page(font: bytes, *more: bytes, content: bytes = b"BT /F1 12 Tf 72 720 Td (AB) Tj ET", resources: bytes = b"") -> bytes:
    return _pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> " + resources
                 + b">> /Contents 5 0 R >>", font, _stream(b"", content), *more])


def slow_pdf(n: int = 20) -> bytes:
    """Form XObjects that each draw 5000 lines and invoke the next: ~2 s for pypdf at n=20, 15 KB (review of P6c2)."""
    import zlib
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> /XObject << /X0 6 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", _stream(b"", b"/X0 Do\n")]
    for i in range(n):
        ops = b"BT /F1 12 Tf 72 720 Td (abc) Tj ET\n" * 5000 + (f"/X{i + 1} Do\n".encode() if i + 1 < n else b"")
        res = (f"/Resources << /Font << /F1 4 0 R >> /XObject << /X{i + 1} {7 + i} 0 R >> >>".encode() if i + 1 < n
               else b"/Resources << /Font << /F1 4 0 R >> >>")
        objs.append(_stream(b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] /Filter /FlateDecode " + res, zlib.compress(ops, 9)))
    return _pdf(objs)


def test_text_with_a_lone_surrogate_comes_out_as_utf8_never_a_crash():
    # a ToUnicode map pointing at <D800> (broken or hostile): pypdf passes the surrogate through; it would fail the
    # text file, the pack's hash and the database
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def\n1 begincodespacerange <00> <FF> "
            b"endcodespacerange\n2 beginbfchar <41> <D800> <42> <0042> endbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end\n")
    font = (b"<< /Type /Font /Subtype /TrueType /BaseFont /Arial /FirstChar 32 /LastChar 255 /Encoding /WinAnsiEncoding "
            b"/ToUnicode 6 0 R >>")
    [(kind, text)] = texts([_page(font, _stream(b"", cmap))])
    assert kind == "ok" and "B" in text and text.encode("utf-8") and not any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def test_one_slow_pdf_is_stopped_on_its_own_and_the_others_still_come():
    import multiprocessing
    got = texts([make_pdf("before"), slow_pdf(), make_pdf("after")], per_file=0.5)
    assert got[0][0] == "ok" and got[2] == ("ok", "after\f") and got[1][0] == "error" and "没读完" in got[1][1]
    assert multiprocessing.active_children() == []                          # the stopped reader is gone


def test_a_stream_that_inflates_past_five_megabytes_is_refused_fast():
    # within pypdf's own 75 MB defaults this one inflates to 17.5 MB of text operators and took 25 s and ~830 MB to stop
    import time
    import zlib
    bomb = zlib.compress(b"BT /F1 12 Tf 72 720 Td (abc) Tj ET\n" * 200_000, 9)          # 7 MB inflated, ~10 KB stored
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    data = _pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                 b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                 font, _stream(b"/Filter /FlateDecode", bomb)])
    t = time.monotonic()
    [(kind, _)] = texts([data])
    assert kind == "error" and time.monotonic() - t < 5


def test_the_excerpt_ends_at_a_sentence_and_never_inside_a_number():
    assert excerpt("甲" * 30 + "。" + "乙" * 40, size=50) == "甲" * 30 + "。…"
    e = excerpt("回购" * 20 + "金额不低于 1,234.56 万元", size=47)                  # the cut falls inside the number
    assert e.endswith("…") and not e[:-1].rstrip().endswith(("1", ",", "2"))
