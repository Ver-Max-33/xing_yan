"""股票资料 (spec §10.3 公司): a company's profile for a person to read, from Tushare, kept a day, as of a day."""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from wb.config import get_paths
from wb.data.providers.tushare import PermissionDenied
from wb.docs.profile import group_of, profile
from wb.workspace.store import Workspace

CODE = "688682.SH"


def df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, infer_schema_length=None)


def holders(ann: str, end: str, rows: list[tuple]) -> list[dict]:
    return [{"ts_code": CODE, "ann_date": ann, "end_date": end, "holder_name": n, "hold_amount": a, "hold_ratio": r,
             "hold_float_ratio": r, "hold_change": c, "holder_type": t} for n, a, r, c, t in rows]


class Fake:
    """Tushare's answers for 霍莱沃, shaped as its API gives them (checked 2026-10-07)."""

    def __init__(self, denied=()):
        self.calls: list[str] = []
        self.denied = set(denied)

    def query(self, api: str, fields: str, **params) -> pl.DataFrame:
        self.calls.append(api)
        if api in self.denied:
            raise PermissionDenied(40203, f"抱歉，您没有接口({api})访问权限")
        return {
            "stock_company": df([{"ts_code": CODE, "com_name": "上海霍莱沃电子系统技术股份有限公司", "chairman": "李吉龙", "manager": "李吉龙",
                                  "reg_capital": 10183.8895, "setup_date": "20070710", "province": "上海", "city": "上海市",
                                  "introduction": "公司致力于电磁场与微波技术各类应用技术的测试和仿真。", "website": "www.holly-wave.com",
                                  "employees": 173, "main_business": "电磁场的测试和仿真，相控阵校准测试。"}]),
            "fina_mainbz": df([
                {"ts_code": CODE, "end_date": "20260630", "bz_item": "主营业务", "bz_sales": 1.18e8, "bz_profit": 3.1e7, "bz_cost": 8.7e7, "curr_type": "CNY"},
                {"ts_code": CODE, "end_date": "20260630", "bz_item": "其他业务", "bz_sales": 3.5e5, "bz_profit": -2.3e4, "bz_cost": 3.8e5, "curr_type": "CNY"},
                {"ts_code": CODE, "end_date": "20251231", "bz_item": "电磁测量系统业务", "bz_sales": 2.6e8, "bz_profit": 4.3e7, "bz_cost": 2.2e8, "curr_type": "CNY"},
                {"ts_code": CODE, "end_date": "20251231", "bz_item": "相控阵产品业务", "bz_sales": 4.0e7, "bz_profit": 2.9e7, "bz_cost": 1.1e7, "curr_type": "CNY"},
                {"ts_code": CODE, "end_date": "20251231", "bz_item": "主营业务", "bz_sales": 3.0e8, "bz_profit": 7.2e7, "bz_cost": 2.3e8, "curr_type": "CNY"}]),
            "top10_holders": df(holders("20260826", "20260630", [("周建华", 2.8e7, 27.49, 0.0, "自然人")])
                                + holders("20260415", "20260331", [("周建华", 2.8e7, 27.49, 0.0, "自然人")])),
            "top10_floatholders": df(
                holders("20260826", "20260630", [("周建华", 2.8e7, 27.49, 0.0, "自然人"), ("上海莱珍投资合伙企业(有限合伙)", 7.7e6, 7.54, 0.0, "投资公司"),
                                                 ("香港中央结算有限公司", 4.9e5, 0.48, 1.2e5, "一般企业"), ("UBSAG", 3.0e5, 0.29, 3.0e5, "金融机构—银行"),
                                                 ("沛县国有资产经营有限公司", 2.0e5, 0.2, -5.0e4, "国资局"),
                                                 ("中国农业银行-华夏平稳增长混合型证券投资基金", 1.5e5, 0.15, 1.5e5, "开放式投资基金")])
                + holders("20260415", "20260331", [("周建华", 2.8e7, 27.49, 0.0, "自然人"), ("香港中央结算有限公司", 3.7e5, 0.36, 0.0, "一般企业"),
                                                   ("沛县国有资产经营有限公司", 2.5e5, 0.25, 0.0, "国资局"), ("某某", 2.0e5, 0.2, 0.0, "自然人")])),
            "stk_holdernumber": df([{"ts_code": CODE, "ann_date": a, "end_date": e, "holder_num": n} for a, e, n in
                                    (("20260826", "20260630", 8117), ("20260415", "20260331", 7620), ("20260415", "20251231", 7566),
                                     ("20251028", "20250930", 5848))]),
            "hk_hold": df([{"code": "30682", "trade_date": d, "ts_code": CODE, "name": "霍萊沃", "vol": v, "ratio": r, "exchange": "SH"}
                           for d, v, r in (("20260630", 485974, 0.47), ("20260331", 371000, 0.36))]),
            "pledge_stat": df([{"ts_code": CODE, "end_date": "20260710", "pledge_count": 1, "unrest_pledge": 98.0, "rest_pledge": 0.0,
                                "total_share": 10183.89, "pledge_ratio": 0.96}]),
            "share_float": df([{"ts_code": CODE, "ann_date": "20260920", "float_date": "20261020", "float_share": 2.0e6, "float_ratio": 1.96,
                                "holder_name": "周建华", "share_type": "首发原始股"},
                               {"ts_code": CODE, "ann_date": "20260920", "float_date": "20261020", "float_share": 1.0e6, "float_ratio": 0.98,
                                "holder_name": "周菡清", "share_type": "首发原始股"},
                               {"ts_code": CODE, "ann_date": "20240413", "float_date": "20240422", "float_share": 2.0e7, "float_ratio": 27.48,
                                "holder_name": "周建华", "share_type": "首发原始股"}]),
            "stk_holdertrade": df([{"ts_code": CODE, "ann_date": "20260310", "holder_name": "方卫中", "holder_type": "P", "in_de": "DE",
                                    "change_vol": 5.0e5, "change_ratio": 0.49, "after_ratio": 6.19, "avg_price": 45.1,
                                    "begin_date": "20260201", "close_date": "20260305"}]),
            "fina_indicator": df([{"ts_code": CODE, "ann_date": a, "end_date": e, "grossprofit_margin": g, "netprofit_margin": 2.36, "roe": 0.73,
                                   "or_yoy": 15.89, "netprofit_yoy": 1194.5, "debt_to_assets": 26.0}
                                  for a, e, g in (("20260826", "20260630", 26.19), ("20260415", "20251231", 24.77))]),
        }[api]


@pytest.fixture
def paths(tmp_path):
    return get_paths("test", tmp_path)


def test_a_profile_says_what_was_known_on_the_day(paths):
    p = profile(paths, "CN:688682", date(2026, 9, 30), source=Fake())
    f = p["holders"]["float10"]
    assert f["period"] == "2026-06-30" and f["known"] == "2026-08-26"
    rows = {r["name"]: r for r in f["rows"]}
    assert rows["香港中央结算有限公司"]["group"] == "外资" and rows["香港中央结算有限公司"]["state"] == "增持"
    assert rows["UBSAG"]["group"] == "外资" and rows["UBSAG"]["state"] == "新进"
    assert rows["沛县国有资产经营有限公司"]["group"] == "国资" and rows["沛县国有资产经营有限公司"]["state"] == "减持"
    assert rows["周建华"]["group"] == "个人" and rows["周建华"]["state"] == "不变"
    assert f["left"] == ["某某"]                                       # in the period before, not now
    early = profile(paths, "CN:688682", date(2026, 8, 25), source=Fake())   # the half-year report was out on 08-26
    assert early["holders"]["float10"]["period"] == "2026-03-31"
    assert [n["number"] for n in p["holders"]["numbers"]] == [5848, 7566, 7620, 8117]
    assert p["holders"]["foreign"]["hk"]["ratio"] == 0.47 and p["holders"]["foreign"]["hk"]["date"] == "2026-06-30"


def test_a_profile_has_the_business_risk_and_figures(paths):
    p = profile(paths, "CN:688682", date(2026, 9, 30), source=Fake())
    seg = p["company"]["segments"]
    assert seg["period"] == "2025-12-31" and [s["name"] for s in seg["items"]] == ["电磁测量系统业务", "相控阵产品业务"]   # not the subtotals
    assert seg["items"][0]["share"] == pytest.approx(2.6e8 / 3.0e8)
    assert p["risk"]["pledge"]["ratio"] == 0.96
    assert p["risk"]["unlocks"] == [{"date": "2026-10-20", "shares": 3.0e6, "ratio": pytest.approx(2.94), "holders": 2, "types": ["首发原始股"]}]
    assert p["risk"]["trades"][0]["direction"] == "减持"
    assert p["finance"]["period"] == "2026-06-30" and p["finance"]["gross_margin"] == 26.19
    s = p["summary"]
    assert "周建华" in s["holders"] and "外资" in s["holders"] and "股东户数" in s["holders"]
    assert "解禁" in s["risk"] and "毛利率" in s["finance"]


def test_a_holder_count_left_out_of_the_latest_period_says_the_last_one_given(paths, tmp_path):
    # 10-19, the 资料 page: 「TypeError: unsupported format string passed to NoneType.__format__」 — Tushare lists a period's
    # 股东户数 without its number; the summary formatted None (the page itself already shows —)
    class Missing(Fake):
        def query(self, api, fields, **params):
            if api == "stk_holdernumber":
                return df([{"ts_code": CODE, "ann_date": a, "end_date": e, "holder_num": n} for a, e, n in
                           (("20260826", "20260630", None), ("20260415", "20260331", 7620), ("20260415", "20251231", 7566))])
            return super().query(api, fields, **params)
    p = profile(paths, "CN:688682", date(2026, 9, 30), source=Missing())
    assert [n["number"] for n in p["holders"]["numbers"]] == [7566, 7620, None]
    assert "股东户数 7,620（2026-03-31，之后的没有公布），比上期 +0.7%" in p["summary"]["holders"]

    class Never(Fake):
        def query(self, api, fields, **params):
            if api == "stk_holdernumber":
                return df([{"ts_code": CODE, "ann_date": "20260826", "end_date": "20260630", "holder_num": None}])
            return super().query(api, fields, **params)
    never = profile(get_paths("test", tmp_path / "never"), "CN:688682", date(2026, 9, 30), source=Never())   # its own day's answers
    assert "股东户数" not in never["summary"]["holders"]


def test_a_profile_is_kept_a_day_and_asked_again_when_you_say(paths):
    fake = Fake()
    profile(paths, "CN:688682", date(2026, 9, 30), source=fake, now=datetime(2026, 10, 7, 9))
    n = len(fake.calls)
    profile(paths, "CN:688682", date(2026, 9, 30), source=fake, now=datetime(2026, 10, 7, 20))
    assert len(fake.calls) == n
    profile(paths, "CN:688682", date(2026, 9, 30), source=fake, now=datetime(2026, 10, 8, 9, 30))
    assert len(fake.calls) == 2 * n
    profile(paths, "CN:688682", date(2026, 9, 30), source=fake, refresh=True, now=datetime(2026, 10, 8, 9, 31))
    assert len(fake.calls) == 3 * n


def test_a_part_without_permission_says_so_and_the_rest_stays(paths):
    p = profile(paths, "CN:688682", date(2026, 9, 30), source=Fake(denied={"hk_hold"}))
    assert "陆股通持股" in p["errors"] and p["holders"]["foreign"]["hk"] is None   # named for you, by the part
    assert p["holders"]["float10"]["rows"]


def test_nothing_is_asked_when_tushare_may_not_be_kept_here(paths):
    Workspace(paths).set_setting("datause.tushare.store", False)
    fake = Fake()
    p = profile(paths, "CN:688682", date(2026, 9, 30), source=fake)
    assert fake.calls == [] and p["unavailable"]


@pytest.mark.parametrize("name,kind,group", [
    ("香港中央结算有限公司", "一般企业", "外资"), ("J.P.MorganSecuritiesplc", "一般企业", "外资"), ("科威特政府投资局-自有资金", "投资公司", "外资"),
    ("中信证券资产管理(香港)有限公司-客户资金", "其他金融产品", "外资"), ("深圳市人民政府国有资产监督管理委员会", "国资局", "国资"),
    ("中央汇金资产管理有限责任公司", "一般企业", "国资"), ("全国社保基金五零三组合", "社保基金、社保机构", "社保养老"),
    ("中国人民人寿保险股份有限公司-传统-普通保险产品", "保险投资组合", "保险"), ("徐新", "自然人", "个人"),
    ("中国农业银行-华夏平稳增长混合型证券投资基金", "开放式投资基金", "公募基金"), ("国投瑞银基金-某产品", "开放式投资基金", "公募基金"),
    ("中国对外经济贸易信托有限公司-外贸信托-睿郡稳享私募证券投资基金", "其他金融产品", "私募资管"), ("青岛盈康医疗投资有限公司", "投资公司", "企业"),
    ("华泰证券股份有限公司", "金融机构—证券公司", "金融机构"),
    ("中国银行股份有限公司-招商中证白酒指数分级证券投资基金", "其他金融产品", "公募基金"),   # Tushare files this index fund oddly
    ("HKSCCNOMINEESLIMITED", "一般企业", "H股代持"), ("香港中央结算(代理人)有限公司", "一般企业", "H股代持"),
    ("香港中央结算代理人有限公司", "一般企业", "H股代持"),   # the nominee without brackets (4 lists of the 2026 半年报), not 陆股通
    # 10-19: one foreign holder counted as several (深赛格) — names from the lists, as Tushare writes them
    ("LISHERYNZHANMING", "自然人", "个人"),                 # a person spelled in Latin letters (深赛格: 139 such in the lists)
    ("陈美林", "自然人", "个人"),                            # a person whose name holds 美林
    ("OPPO广东移动通信有限公司", "一般企业", "企业"),       # a domestic company with a Latin brand
    ("广新集团-中信证券-26GXKEB1担保及信托财产专户", "信托公司集合信托计划", "私募资管"),   # a domestic bond's code
    ("华美国际投资集团有限公司", "投资公司", "企业"),       # 美国 inside a domestic name
    ("J.P.MorganSecuritiesPLC-自有资金", "一般企业", "外资"),   # a foreign company's own form beside Chinese words
    ("铭基国际投资公司-MATTHEWSASIAFUNDS(US)", "其他金融产品", "外资"),
    ("韩国银行-自有资金", "一般企业", "外资"), ("国民年金公团(韩国)-自有资金", "一般企业", "外资"),   # where it is registered
    ("澳门金融管理局-自有资金", "一般企业", "外资"), ("横河电机株式会社", "一般企业", "外资"),
    ("首域投资管理(英国)有限公司-首域中国A股基金", "其他金融产品", "外资"),   # a foreign fund, not a domestic one's product
    ("瑞典第二国家养老基金-自有资金", "一般企业", "外资"),
])
def test_holders_are_grouped_by_their_type_and_name(name, kind, group):
    assert group_of(name, kind) == group


def test_the_api_gives_the_profile_as_of_a_day(tmp_path):
    from fastapi.testclient import TestClient

    from wb.api.app import create_app
    app = create_app(tmp_path, token="t")
    app.state.profile_source = Fake()
    c = TestClient(app)
    r = c.get("/api/profile", params={"listing_id": "CN:688682", "as_of": "2026-09-30"}, headers={"X-WB-Token": "t"}).json()
    assert r["holders"]["float10"]["period"] == "2026-06-30" and r["summary"]["holders"].startswith("第一大股东 周建华")
    jp = c.get("/api/profile", params={"listing_id": "JP:7203"}, headers={"X-WB-Token": "t"}).json()
    assert "EDINET" in jp["unavailable"]


def test_an_a_plus_h_company_s_largest_holder_is_not_the_h_share_nominee(paths):
    # 比亚迪 (002594), 2026 半年报: HKSCC NOMINEES holds the H shares for their owners (40.38% of all shares, no A float ratio)
    class AH(Fake):
        def query(self, api, fields, **params):
            if api == "top10_holders":
                return df(holders("20260829", "20260630", [("HKSCCNOMINEESLIMITED", 3.68e9, 40.38, 2.5e4, "一般企业"),
                                                           ("王传福", 1.54e9, 16.90, 0.0, "自然人")]))
            if api == "top10_floatholders":
                return df(holders("20260829", "20260630", [("融捷投资控股集团有限公司", 4.7e8, 13.35, 0.0, "投资公司")])
                          + [{**holders("20260829", "20260630", [("HKSCCNOMINEESLIMITED", 3.68e9, None, 2.5e4, "一般企业")])[0],
                              "hold_float_ratio": None}])
            return super().query(api, fields, **params)
    p = profile(paths, "CN:002594", date(2026, 9, 30), source=AH())
    assert p["summary"]["holders"].startswith("第一大股东 王传福（16.90%；H 股代持账户 HKSCC 合计 40.38% 不计）")
    assert "外资 1 家" not in p["summary"]["holders"]
    assert p["holders"]["groups"] == {"企业": 13.35}


def test_a_breakdown_leaves_out_its_totals_and_adjustments():
    from wb.docs.profile import _segments
    mb = pl.DataFrame({"end_date": [date(2026, 6, 30)] * 3, "bz_item": ["茅台酒", "其他系列酒", "合计特别调整"],
                       "bz_sales": [7.5e10, 1.25e10, 1.5e9], "bz_profit": [None] * 3})
    seg = _segments(mb, pl.DataFrame({"ann_date": [date(2026, 8, 15)], "end_date": [date(2026, 6, 30)]}), date(2026, 9, 30))
    assert [s["name"] for s in seg["items"]] == ["茅台酒", "其他系列酒"]
