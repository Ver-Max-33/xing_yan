"""研究记录 (spec §10.3, §10.4): every research is kept with the evidence pack it was given and its answer; the next one is
told what the last one said and shown what changed since; 20 trading days on, each is settled against the market's
benchmark from the first open after it was made — forward only: a research is never run on the past."""
import json
from datetime import date

import polars as pl
import pytest

from tests.test_ai_research import answer, scripted
from tests.test_validate_engine import boxes_market
from wb.ai import journal
from wb.data.store import Store
from wb.validate.outcomes import outcomes
from wb.workspace.store import Workspace

LID = "CN:600000"


@pytest.fixture
def market(paths):
    boxes_market(paths)
    Workspace(paths).set_setting("datause.cninfo.store", False)   # no announcements fetched in a unit test
    return paths, Store(paths, "CN").read("calendar")["date"].sort().to_list()


def record(day, created, lean="支持", lid=LID, facts=()):
    return {"listing_id": lid, "day": str(day), "trigger": "manual", "status": "ok", "flags": [], "call_id": "c",
            "model": "m", "cost": 0.01, "created_at": created, "output": {**json.loads(answer()), "lean": lean},
            "pack": {"listing_id": lid, "day": str(day), "facts": list(facts), "left_out": [], "hash": "h"}}


def test_a_research_is_run_on_the_latest_day_kept_and_the_next_one_is_told_the_last(market, monkeypatch):
    paths, days = market
    same = {"verdict": "同向", "text": "仍在箱里", "refs": ["px.close"]}
    cite = {"text": "见收盘", "refs": ["px.close"], "inference": False}
    plain = {"support": [cite], "against": [cite], "invalidation": [cite]}         # what this market's pack has
    llm, sent = scripted(paths, monkeypatch, answer(**plain), answer(**plain, continuity=same))
    first = journal.run(llm, lambda m: paths, LID)
    assert first["day"] == str(days[-1]) and first["pack"]["facts"] and first["trigger"] == "manual"
    assert first["status"] in ("ok", "需复核") and journal.get(paths, first["id"])["flags"] == first["flags"]
    second = journal.run(llm, lambda m: paths, LID)
    assert "上次研究" in sent[1]["messages"][1]["content"] and second["output"]["continuity"] == same
    assert [r["id"] for r in journal.history(paths, LID)] == [second["id"], first["id"]]
    assert journal.get(paths, first["id"])["pack"] == first["pack"] and journal.latest(paths, LID)["id"] == second["id"]
    assert journal.recent(paths)[0]["id"] == second["id"] and "pack" not in journal.recent(paths)[0]


def test_what_changed_since_the_last_research():
    old = [{"id": "px.close", "text": "收盘 10.00"}, {"id": "box.mid", "text": "中箱 上沿 11"}, {"id": "ann.1", "text": "回购"}]
    new = [{"id": "px.close", "text": "收盘 10.50"}, {"id": "box.mid", "text": "中箱 上沿 11"}, {"id": "fin.2026Q3", "text": "三季报"}]
    d = journal.diff({"facts": old}, {"facts": new})
    assert [f["id"] for f in d["added"]] == ["fin.2026Q3"] and [f["id"] for f in d["removed"]] == ["ann.1"]
    assert d["changed"] == [{"id": "px.close", "before": "收盘 10.00", "after": "收盘 10.50"}]


def test_a_research_is_settled_20_trading_days_on_from_the_first_open_after_it_was_made(market):
    paths, days = market
    evening = journal.save(paths, record(days[600], f"{days[600]}T20:00:00+08:00"))           # after the close
    session = journal.save(paths, record(days[600], f"{days[601]}T10:30:00+08:00"))           # the next day's session
    fresh = journal.save(paths, record(days[-1], f"{days[-1]}T20:00:00+08:00"))
    assert journal.settle(lambda m: paths) == 2
    o = outcomes(paths, "CN", pl.DataFrame({"listing_id": [LID, LID], "signal_date": [days[600], days[601]]})).sort("signal_date")
    a, b = journal.get(paths, evening["id"]), journal.get(paths, session["id"])
    assert (a["entry_date"], a["excess20"], a["return20"]) == (str(days[601]), pytest.approx(o["x20"][0]), pytest.approx(o["r20"][0]))
    assert (b["entry_date"], b["excess20"]) == (str(days[602]), pytest.approx(o["x20"][1]))    # its day's open came before it
    assert journal.get(paths, fresh["id"])["settled_at"] is None                                 # its 20 days have not passed
    assert journal.settle(lambda m: paths) == 0


def test_the_evaluation_compares_what_followed_each_lean_once_settled(market):
    paths, days = market
    for k in range(6):
        journal.save(paths, record(days[500 + k], f"{days[500 + k]}T20:00:00+08:00", lean="支持" if k % 2 else "反对",
                                   lid=f"CN:{600000 + k}"))
    journal.save(paths, record(days[-1], f"{days[-1]}T20:00:00+08:00"))
    journal.settle(lambda m: paths)
    ev = journal.evaluation(paths)
    by = {g["lean"]: g for g in ev["groups"]}
    assert by["支持"]["settled"] == 3 and by["反对"]["settled"] == 3 and ev["pending"] == 1
    assert by["支持"]["mean_excess"] == pytest.approx(sum(r["excess20"] for r in journal.recent(paths) if r["output"]["lean"] == "支持"
                                                          and r["excess20"] is not None) / 3)
    assert ev["enough"] is False                                                                 # fewer than 30: numbers only


def test_made_up_data_is_never_mixed_with_fetched_announcements_nor_evaluated(paths, monkeypatch):
    from wb.devdata import write
    write(paths, 320)                                                                  # the demo data: made up
    monkeypatch.setattr("wb.docs.announcements.refresh", lambda *a, **k: pytest.fail("asked cninfo for made-up data"))
    cite = {"text": "见收盘", "refs": ["px.close"], "inference": False}
    llm, _ = scripted(paths, monkeypatch, answer(support=[cite], against=[cite], invalidation=[cite]))
    r = journal.run(llm, lambda m: paths, "CN:600519")
    assert r["demo"] is True and journal.evaluation(paths)["pending"] == 0


def test_announcements_that_cannot_be_fetched_are_named_and_the_research_still_runs(market, monkeypatch):
    paths, _ = market
    Workspace(paths).set_setting("datause.cninfo.store", True)

    def offline(*a, **k):
        raise ConnectionError("连不上 www.cninfo.com.cn")
    monkeypatch.setattr("wb.docs.announcements.refresh", offline)
    cite = {"text": "见收盘", "refs": ["px.close"], "inference": False}
    llm, sent = scripted(paths, monkeypatch, answer(support=[cite], against=[cite], invalidation=[cite]))
    r = journal.run(llm, lambda m: paths, LID)
    assert any("巨潮资讯公告（这次没取到" in x for x in r["pack"]["left_out"])
    assert "这次没取到" in sent[0]["messages"][1]["content"] and "资料不足" in sent[0]["messages"][1]["content"]


def test_a_research_reads_the_newest_announcements_and_can_cite_their_excerpt(market, monkeypatch):
    import tests.test_announcements as ta
    from tests.test_docs_pdf import make_pdf
    paths, _ = market
    Workspace(paths).set_setting("datause.cninfo.store", True)
    monkeypatch.setattr(ta, "LIST", {"stockList": [{"code": "600000", "orgId": "gssh0600000"}]})
    cite = {"text": "公司拟回购股份", "refs": ["doc.7"], "inference": False}
    llm, sent = scripted(paths, monkeypatch, answer(summary="公司公告回购股份。", support=[cite], against=[cite], invalidation=[cite],
                                                     next_review=["回购实施之后"]))
    src = ta.source([[ta.ann(7, "2026-09-29", "关于回购股份的公告")]], pdf=make_pdf("Buyback of 120 million yuan"))
    r = journal.run(llm, lambda m: paths, LID, source=src)
    assert "[doc.7] 《关于回购股份的公告》摘录：Buyback of 120 million yuan" in sent[0]["messages"][1]["content"]
    assert r["output"]["support"][0]["refs"] == ["doc.7"] and r["status"] == "ok"
    broken = ta.source([[ta.ann(8, "2026-09-30", "关于董事辞职的公告"), ta.ann(7, "2026-09-29", "关于回购股份的公告")]], pdf=b"broken")
    assert any("公告正文" in x and "辞职" in x for x in journal.fresh_docs(paths, LID, date(2026, 9, 30), broken))
