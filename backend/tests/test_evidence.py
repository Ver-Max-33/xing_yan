"""证据包 (spec §10.3): the facts about a stock on a day, each with an id a research must cite — prices and rules as of
that day's close (§5.8), reports and announcements published by that day (a research is read that evening, and its
outcome is counted from the next open), prices in that day's exchange prices, and only from sources whose 发送给 LLM
is on (§4.10)."""
from datetime import date, timedelta

import polars as pl
import pytest

from tests.test_validate_engine import boxes_market
from wb.data.store import Store
from wb.datasets import conform
from wb.ai.evidence import pack

LID = "CN:600000"


@pytest.fixture
def market(paths):
    boxes_market(paths)
    st = Store(paths, "CN")
    days = st.read("bars_1d").filter(pl.col("listing_id") == LID)["date"].sort().to_list()
    return paths, days


def test_every_fact_is_known_by_the_days_close_and_prices_are_that_days(market):
    paths, days = market
    day = days[600]
    p = pack(lambda m: paths, LID, day)
    facts = {f["id"]: f for f in p["facts"]}
    close = Store(paths, "CN").read("bars_1d").filter((pl.col("listing_id") == LID) & (pl.col("date") == day))["close"][0]
    assert facts["px.close"]["value"] == pytest.approx(close) and str(day) in facts["px.close"]["text"]
    assert all(f["as_of"] <= str(day) for f in p["facts"]) and p["day"] == str(day)
    boxes = [facts[k] for k in ("box.long", "box.mid", "box.short") if k in facts]        # 宏中微: each scale's latest
    assert boxes and all("上沿" in b["text"] and b["as_of"] <= str(day) for b in boxes)
    assert p["hash"] == pack(lambda m: paths, LID, day)["hash"]                       # the same day, the same pack
    assert pack(lambda m: paths, LID, days[601])["facts"] != p["facts"]                # a later day knows more


def test_a_report_is_in_the_pack_once_it_was_published(market):
    # a report announced that day (after the close: the formulas read it from the next day) is in that evening's pack
    paths, days = market
    day = days[600]
    fin = conform(pl.DataFrame({
        "listing_id": LID, "period_end": [day - timedelta(days=60), day - timedelta(days=5), day - timedelta(days=4)],
        "period_type": ["H1", "Q3", "FY"], "ann_date": [day - timedelta(days=30), day, days[601]],
        "known_at": [day - timedelta(days=29), days[601], days[602]],
        "revenue": [1e9, 1.5e9, 2e9], "net_profit": [1e8, 1.6e8, 2e8], "profit_yoy": [12.0, 15.0, 9.0], "source": "t"}), "financials")
    st = Store(paths, "CN")
    st.publish({"financials": fin}, as_of=st.latest().as_of)
    facts = {f["id"]: f for f in pack(lambda m: paths, LID, day)["facts"]}
    assert sorted(k for k in facts if k.startswith("fin.")) == sorted(f"fin.{(day - timedelta(days=d)).year}{t}" for d, t in ((60, "H1"), (5, "Q3")))
    assert all(f["as_of"] <= str(day) for f in facts.values())


def test_announcements_up_to_the_day_and_your_own_notes_are_facts(market):
    from wb.docs.announcements import SCHEMA, _file
    from wb.workspace.watch import Watch
    paths, days = market
    day = days[600]
    f = _file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([(LID, "9", day - timedelta(days=3), "关于回购股份的公告", "http://x/9.PDF", 10.0, "cninfo"),
                  (LID, "10", day, "当天的公告", "http://x/10.PDF", 10.0, "cninfo"),
                  (LID, "11", days[601], "第二天的公告", "http://x/11.PDF", 10.0, "cninfo")], schema=SCHEMA, orient="row").write_parquet(f)
    w = Watch(lambda m: paths)
    w.add(LID, reason="箱体很干净，放量突破")
    facts = {x["id"]: x for x in pack(lambda m: paths, LID, day)["facts"]}
    assert "ann.9" in facts and "ann.10" in facts and "ann.11" not in facts and "回购" in facts["ann.9"]["text"]
    assert facts["ann.9"]["url"] == "http://x/9.PDF"                                       # one click to the original
    assert facts["note.reason"]["text"].endswith("箱体很干净，放量突破") and facts["note.reason"]["source"] == "user"


def test_a_source_kept_from_llms_gives_no_facts_and_is_named(market):
    from wb.workspace.store import Workspace
    paths, days = market
    Workspace(paths).set_setting("datause.tushare.llm", False)
    p = pack(lambda m: paths, LID, days[600])
    assert not any(f["source"] == "tushare" for f in p["facts"]) and p["left_out"] == ["Tushare"]


def test_a_rules_verdict_comes_with_its_conditions_and_their_values(market):
    from wb.engine.l3.run import run_rule
    from wb.workspace.rules import Rules
    paths, days = market
    Rules(paths).save("""
id: up_vol
name: 放量上涨
markets: [CN]
stages:
  - id: today
    kind: event
    when: "C > REF(C, 1)"
    at:
      - {id: vol, expr: "VOL_RATIO(5) >= 0.5"}
signal_at: today.at
""", create=True)
    rule, _, version = Rules(paths).get("up_vol")
    run = run_rule(paths, rule, "CN", day=days[600], version=version)
    lid = run.rows.filter(pl.col("status") == "signal")["listing_id"][0]
    fact = {f["id"]: f for f in pack(lambda m: paths, lid, days[600])["facts"]}["rule.up_vol"]
    assert "放量上涨" in fact["text"] and "出信号" in fact["text"] and "VOL_RATIO(5) >= 0.5：" in fact["text"] and "门槛 0.5，通过" in fact["text"]


def test_the_newest_announcements_text_comes_as_an_excerpt_a_research_can_cite(market):
    # spec §10.3 文档片段：A 股巨潮公告正文 — what follows the board's standard assurance, with the link to the original
    from wb.docs.announcements import SCHEMA, _file, _text
    paths, days = market
    day = days[600]
    f = _file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([(LID, "21", day - timedelta(days=2), "关于回购股份的公告", "http://x/21.PDF", 10.0, "cninfo"),
                  (LID, "22", day - timedelta(days=3), "2026年半年度报告", "http://x/22.PDF", 900.0, "cninfo"),
                  (LID, "23", day - timedelta(days=4), "关于董事辞职的公告", "http://x/23.PDF", 10.0, "cninfo")],
                 schema=SCHEMA, orient="row").write_parquet(f)
    texts = {"21": "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，并对其内容的真实性、"
                   "准确性和完整性承担法律责任。\n公司拟以 1.2 亿元回购股份。", "22": "半年报正文", "23": ""}
    for doc, text in texts.items():
        t = _text(paths, doc)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(text, encoding="utf-8")
    facts = {x["id"]: x for x in pack(lambda m: paths, LID, day)["facts"]}
    assert facts["doc.21"]["text"] == "《关于回购股份的公告》摘录：公司拟以 1.2 亿元回购股份。" and facts["doc.21"]["url"] == "http://x/21.PDF"
    assert facts["doc.21"]["as_of"] == str(day - timedelta(days=2)) and facts["doc.21"]["source"] == "cninfo"
    assert "doc.22" not in facts and "doc.23" not in facts            # a periodic report is not excerpted; no text, no fact


def test_a_busy_stocks_announcement_titles_are_the_newest_thirty(market):
    # real: 宁德时代 had 58 announcements in the 90 days before 2026-09-30
    from wb.docs.announcements import SCHEMA, _file
    paths, days = market
    day = days[600]
    _file(paths).parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([(LID, str(100 + k), day - timedelta(days=k), f"公告{k}", None, 1.0, "cninfo") for k in range(40)],
                 schema=SCHEMA, orient="row").write_parquet(_file(paths))
    ids = [f["id"] for f in pack(lambda m: paths, LID, day)["facts"] if f["kind"] == "ann"]
    assert ids == [f"ann.{100 + k}" for k in range(30)]


def test_documents_you_imported_are_in_the_pack_with_the_page_of_their_excerpt(market):
    # spec §10.3 文档片段：用户导入的 IR 资料 — yours, like your notes: in the pack whatever the data sources' switches
    from wb.docs import ir
    from wb.workspace.store import Workspace
    paths, days = market
    d = ir.add(paths, LID, "调研纪要.txt", "调研纪要：产能利用率约八成，订单排到明年一季度。".encode())
    Workspace(paths).set_setting("datause.tushare.llm", False)
    f = {x["id"]: x for x in pack(lambda m: paths, LID, days[600])["facts"]}[f"ir.{d['id']}"]
    assert f["text"] == "你导入的资料《调研纪要》第 1 页：调研纪要：产能利用率约八成，订单排到明年一季度。"
    assert (f["source"], f["kind"], f["doc"], f["page"]) == ("user", "ir", d["id"], 1)


def test_every_document_you_imported_is_in_the_pack_on_one_line(market):
    # yours, always (the 资料 column says so): not only the newest five; the prompt is one line a fact
    from wb.docs import ir
    paths, days = market
    made = [ir.add(paths, LID, f"纪要{k}.txt", f"第{k}次调研纪要：产能利用率约八成。".encode()) for k in range(6)]
    ir.set_excerpt(paths, made[0]["id"], "第一行\n第二行", 1)
    facts = {x["id"]: x for x in pack(lambda m: paths, LID, days[600])["facts"]}
    assert all(f"ir.{d['id']}" in facts for d in made) and facts[f"ir.{made[0]['id']}"]["text"].endswith("第 1 页：第一行 第二行")
