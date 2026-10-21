"""你导入的 IR 资料 (spec §10.3 文档片段): a document you add to a stock — its text read once and kept page by page; the
excerpt a research gets is yours to choose (by default the first page with something to say), with its page, so the
research's citation leads back to the original's place; yours: always in the pack, until you delete it."""
import pytest

from tests.test_docs_pdf import make_pdf
from wb.docs import ir

LID = "JP:7203"


def test_a_pdf_you_import_is_kept_page_by_page_with_an_excerpt_from_the_first_page_with_something_to_say(paths):
    d = ir.add(paths, LID, "2026年3月期 決算説明資料.pdf", make_pdf("Q1", "Operating profit 1.2 trillion yen, up 8 percent on the year"))
    assert d["title"] == "2026年3月期 決算説明資料" and d["pages"] == 2 and d["page"] == 2
    assert d["excerpt"] == "Operating profit 1.2 trillion yen, up 8 percent on the year"
    assert ir.page_texts(paths, d["id"])[1].startswith("Operating profit") and [x["id"] for x in ir.docs(paths, LID)] == [d["id"]]
    assert ir.docs(paths, "JP:6758") == []


def test_a_text_file_is_one_page_in_utf8_or_gbk(paths):
    a = ir.add(paths, LID, "notes.txt", "电话会纪要：管理层预计下半年订单回暖。".encode())
    b = ir.add(paths, LID, "旧笔记.txt", "调研纪要：产能利用率约八成。".encode("gb18030"))
    assert (a["pages"], a["excerpt"]) == (1, "电话会纪要：管理层预计下半年订单回暖。") and b["excerpt"] == "调研纪要：产能利用率约八成。"


def test_what_cannot_be_read_is_said_and_nothing_is_kept(paths):
    for data, why in ((b"PK\x03\x04\x00\x00binary", "不是 PDF"), (make_pdf(""), "没有读出文字"), (b"%PDF-" + b"0" * 64, "PDF")):
        with pytest.raises(ValueError, match=why):
            ir.add(paths, LID, "x.bin", data)
    with pytest.raises(ValueError, match="太大"):
        ir.add(paths, LID, "big.txt", b"a" * (ir.MAX_BYTES + 1))
    assert ir.docs(paths, LID) == []


def test_the_excerpt_is_yours_to_change_and_a_document_to_delete(paths):
    d = ir.add(paths, LID, "deck.pdf", make_pdf("Cover", "Guidance raised to 4.9 trillion yen"))
    ir.set_excerpt(paths, d["id"], "Cover", 1)
    assert (ir.get(paths, d["id"])["excerpt"], ir.get(paths, d["id"])["page"]) == ("Cover", 1)
    with pytest.raises(ValueError, match="第 3 页"):
        ir.set_excerpt(paths, d["id"], "x", 3)
    ir.remove(paths, d["id"])
    assert ir.docs(paths, LID) == [] and not list((paths.root / "docs" / "ir").glob(f"{d['id']}*"))


def test_japanese_text_files_are_read_in_their_own_encoding_and_a_bom_is_not_text(paths):
    # every Shift_JIS or EUC-JP byte pair is also valid GB18030: read that way it is silent garbage sent to the model
    jp = "2026年3月期の売上高は1兆2,345億円となりました。"
    for enc in ("cp932", "euc_jp"):
        assert ir.add(paths, LID, f"{enc}.txt", jp.encode(enc))["excerpt"] == jp
    assert ir.add(paths, LID, "gbk.txt", "电话会纪要：订单回暖。".encode("gb18030"))["excerpt"] == "电话会纪要：订单回暖。"
    assert ir.add(paths, LID, "bom.txt", "﻿调研纪要：产能利用率约八成".encode("utf-8"))["excerpt"] == "调研纪要：产能利用率约八成"


def test_form_feeds_in_a_text_file_are_its_pages(paths):
    # pdftotext writes one; the viewer and the record must agree on the pages
    d = ir.add(paths, LID, "deck.txt", "Cover\fGuidance raised to 4.9 trillion yen for the year\f".encode())
    assert d["pages"] == 2 and d["page"] == 2 and ir.page_texts(paths, d["id"]) == ["Cover", "Guidance raised to 4.9 trillion yen for the year"]
    assert ir.set_excerpt(paths, d["id"], "Cover", 1)["page"] == 1


def test_a_pdf_whose_only_text_is_page_numbers_is_refused(paths):
    # a scanned deck with numbered pages: nothing for a research to read
    with pytest.raises(ValueError, match="没有读出文字"):
        ir.add(paths, LID, "scan.pdf", make_pdf("1", "2", "3"))


def test_your_excerpt_is_at_most_three_thousand_characters(paths):
    d = ir.add(paths, LID, "notes.txt", "电话会纪要：管理层预计下半年订单回暖。".encode())
    with pytest.raises(ValueError, match="3000"):
        ir.set_excerpt(paths, d["id"], "字" * 3001, 1)
