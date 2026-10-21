"""One industry scheme across the app (design 2026-10-12-market-page-redesign §6.3): an A share's 申万 一级 · 二级 on a
day by its membership spells (the rules read the same), a 日股 listing's 33 業種; the members of an industry; where a
Tushare label sits in 申万; and the instrument and the profile saying both levels."""
from datetime import date

import polars as pl
import pytest
from fastapi.testclient import TestClient

from wb.api.app import create_app
from wb.config import get_paths
from wb.data.store import Store
from wb.datasets import conform
from wb.devdata import write
from wb.docs.profile import _industry
from wb.market import industry

H = {"X-WB-Token": "t"}


@pytest.fixture
def store_with_spells(tmp_path):
    """CN:000001 in 银行 / 股份制银行Ⅱ until 2026-06-30, then 非银金融 / 证券Ⅱ; CN:600519 in 食品饮料 / 白酒Ⅱ."""
    st = Store(get_paths("test", tmp_path), "CN")
    ind = conform(pl.DataFrame({
        "listing_id": ["CN:000001", "CN:000001", "CN:600519"], "scheme": "SW2021", "code": ["801780", "801790", "801120"],
        "name": ["银行", "非银金融", "食品饮料"], "in_date": [date(2020, 1, 2), date(2026, 7, 1), date(2020, 1, 2)],
        "out_date": [date(2026, 6, 30), None, None], "source": "test", "l2_code": ["801782", "801193", "801125"],
        "l2_name": ["股份制银行Ⅱ", "证券Ⅱ", "白酒Ⅱ"]}), "industry")
    inst = conform(pl.DataFrame({"listing_id": ["CN:000001", "CN:600519"], "market": "CN", "code": ["000001", "600519"],
                                 "name": ["平安银行", "贵州茅台"], "industry": ["银行", "白酒"], "source": "test"}), "instruments")
    st.publish({"industry": ind, "instruments": inst}, as_of=date(2026, 9, 30))
    return st, st.latest()


@pytest.fixture
def demo(tmp_path):
    p = get_paths("test", tmp_path)
    write(p, 320)
    return tmp_path, p


def test_a_stock_is_in_the_spell_covering_the_day(store_with_spells):
    st, snap = store_with_spells
    def on(d):
        return industry.at(st, snap, "CN", d).filter(pl.col("listing_id") == "CN:000001").row(0, named=True)
    assert (on(date(2026, 6, 30))["l1"], on(date(2026, 6, 30))["l2"]) == ("银行", "股份制银行Ⅱ")     # the last day in it
    assert (on(date(2026, 9, 1))["l1"], on(date(2026, 9, 1))["l2"]) == ("非银金融", "证券Ⅱ")


def test_members_by_level_and_what_a_bad_ref_says(store_with_spells):
    st, snap = store_with_spells
    assert industry.members(st, snap, "CN", "l2:证券Ⅱ", date(2026, 9, 1)) == {"CN:000001"}
    assert industry.members(st, snap, "CN", "l1:银行", date(2026, 6, 1)) == {"CN:000001"}
    for bad, why in (("证券", "l1:名称 或 l2:名称"), ("l3:x", "l1:名称 或 l2:名称"), ("l2:没有这个", "没有叫「没有这个」的申万二级行业")):
        with pytest.raises(ValueError, match=why):
            industry.members(st, snap, "CN", bad, date(2026, 9, 1))
    with pytest.raises(ValueError, match="日股只有一级"):
        industry.parse("l2:銀行業", "JP")


def test_a_tushare_label_has_a_home_in_shenwan(store_with_spells):
    st, snap = store_with_spells
    assert industry.tushare_home(st, snap, "白酒") == "食品饮料" and industry.tushare_home(st, snap, "不存在") is None


def test_japan_reads_its_33_industries(demo):
    _, p = demo
    st = Store(p, "JP")
    got = industry.at(st, st.latest(), "JP", date(2026, 9, 30))
    assert dict(zip(got["listing_id"], got["l1"], strict=True))["JP:7203"] == "輸送用機器" and got["l2"].null_count() == got.height


def test_the_demo_data_has_both_levels_and_tushare_labels(demo):
    _, p = demo
    st = Store(p, "CN")
    got = industry.at(st, st.latest(), "CN", date(2026, 9, 30)).sort("listing_id")
    assert got.rows() == [("CN:000001", "银行", "股份制银行Ⅱ"), ("CN:600519", "食品饮料", "白酒Ⅱ")]
    assert dict(st.read("instruments").select("listing_id", "industry").iter_rows()) == {"CN:600519": "白酒", "CN:000001": "银行"}


def test_the_instrument_and_the_profile_say_both_levels(demo, store_with_spells):
    root, _ = demo
    c = TestClient(create_app(root, token="t"))
    cn = c.get("/api/instrument/CN:600519", headers=H).json()
    assert (cn["ind_l1"], cn["ind_l2"]) == ("食品饮料", "白酒Ⅱ")
    jp = c.get("/api/instrument/JP:7203", headers=H).json()
    assert (jp["ind_l1"], jp["ind_l2"]) == ("輸送用機器", None)
    st, _ = store_with_spells
    assert _industry(st.paths, "CN:000001", date(2026, 9, 1)) == ("非银金融", "证券Ⅱ")
