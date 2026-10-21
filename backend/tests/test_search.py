import polars as pl

from wb.datasets import conform
from wb.workspace.search import build_index, search


def idx():
    return build_index(conform(pl.DataFrame({
        "listing_id": ["CN:600519", "CN:000001", "JP:7203", "JP:130A", "CN:600000"],
        "market": ["CN", "CN", "JP", "JP", "CN"],
        "code": ["600519", "000001", "7203", "130A", "600000"],
        "name": ["贵州茅台", "平安银行", "トヨタ自動車", "Ｖｅｒｉｔａｓ　Ｉｎ　Ｓｉｌｉｃｏ", "浦发银行"],
        "name_en": [None, "Ping An Bank", "TOYOTA MOTOR CORPORATION", "Veritas In Silico", None],
        "status": ["L", "L", "L", "L", "D"]}), "instruments"))


def ids(q, **kw):
    return [r["listing_id"] for r in search(idx(), q, **kw)]


def test_code_prefix_and_exact():
    assert ids("600")[:2] == ["CN:600519", "CN:600000"]  # delisted ranks after listed
    assert ids("7203") == ["JP:7203"]
    assert ids("130a") == ["JP:130A"]


def test_pinyin_initials_and_names():
    assert ids("gzmt") == ["CN:600519"]
    assert ids("银行") == ["CN:000001", "CN:600000"]
    assert ids("トヨタ") == ["JP:7203"]
    assert ids("toyota") == ["JP:7203"]
    assert ids("ｖｅｒｉｔａｓ") == ["JP:130A"]  # full-width input folded
    assert search(idx(), "veri")[0]["listing_id"] == "JP:130A"  # full-width name folded too


def test_empty_query():
    assert search(idx(), "  ") == []
