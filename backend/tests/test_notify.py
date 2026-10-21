"""Feishu notifications (spec §7.1): an outbox keyed by what the notice is about (sent at least once, never twice,
also across a restart), cards merged per minute and split when long, the signature, the rate limit, retries."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
import pytest

from wb.notify.feishu import MAX_BYTES, Feishu, FeishuError, brief_card, cards, check_webhook, quote_url, sign
from wb.notify.outbox import Outbox, drain

URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
T0 = datetime(2026, 10, 2, 16, 45)


def test_only_a_feishu_or_lark_bot_is_a_webhook():
    assert check_webhook(URL) == URL and check_webhook("https://open.larksuite.com/open-apis/bot/v2/hook/x")
    for bad in ("http://open.feishu.cn/open-apis/bot/v2/hook/abc", "https://example.com/hook", "https://open.feishu.cn.evil.com/open-apis/bot/v2/hook/a"):
        with pytest.raises(ValueError, match="飞书"):
            check_webhook(bad)


def test_the_signature_is_feishus():
    want = base64.b64encode(hmac.new(b"1700000000\nsecret", digestmod=hashlib.sha256).digest()).decode()
    assert sign(1700000000, "secret") == want


def test_a_notice_is_queued_once_whatever_happens_later(paths):
    box = Outbox(paths)
    key = "signal|CN:600519|above_ma@3|b1|2026-10-02"
    assert box.enqueue("signal", key, {"line": "x"}, market="CN", day="2026-10-02", now=T0) is True
    assert box.enqueue("signal", key, {"line": "x"}, market="CN", day="2026-10-02", now=T0) is False    # the same notice
    assert Outbox(paths).enqueue("signal", key, {"line": "x"}, now=T0) is False                       # after a restart too
    assert len(Outbox(paths).pending(T0)) == 1


def test_many_notices_become_few_cards_and_a_long_one_splits():
    items = [{"kind": "signal", "title": f"股票 {i}", "line": "上涨后箱体缩量突破 · 箱体 41 根 · 量比 2.4"} for i in range(45)]
    cs = cards(items, "形研 · 收盘后")
    assert len(cs) == 3 and all(len(json.dumps(c, ensure_ascii=False).encode()) < 25_000 for c in cs)
    assert cs[0]["header"]["title"]["content"].startswith("形研 · 收盘后") and "（1/3）" in cs[0]["header"]["title"]["content"]
    md = cs[0]["elements"][0]["text"]
    assert md["tag"] == "lark_md" and "**股票 0**" in md["content"]


def test_draining_sends_signed_cards_marks_them_and_retries_later(paths):
    box = Outbox(paths)
    for i in range(3):
        box.enqueue("signal", f"k{i}", {"title": f"S{i}", "line": "x"}, now=T0)
    sent, fail = [], {"on": True}

    def route(request):
        body = json.loads(request.content)
        sent.append(body)
        if fail["on"]:
            return httpx.Response(200, json={"code": 11232, "msg": "frequency limited"})
        return httpx.Response(200, json={"code": 0, "msg": "success"})
    bot = Feishu(URL, "secret", transport=httpx.MockTransport(route), clock=lambda: 1700000000)
    assert drain(box, bot, T0) == 0 and len(box.pending(T0)) == 0                           # failed: waits before retrying
    assert len(box.pending(T0 + timedelta(minutes=2))) == 3
    fail["on"] = False
    assert drain(box, bot, T0 + timedelta(minutes=2)) == 3                                 # three notices, one card
    assert len(sent) == 2 and sent[-1]["msg_type"] == "interactive" and sent[-1]["sign"] == sign(1700000000, "secret")
    assert box.pending(T0 + timedelta(hours=1)) == [] and box.recent(10)[0]["sent_at"]


def test_a_wrong_secret_is_reported_and_not_retried_forever(paths):
    bot = Feishu(URL, "secret", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})))
    with pytest.raises(FeishuError, match="签名") as e:
        bot.send(cards([{"kind": "test", "title": "测试", "line": "x"}], "形研")[0])
    assert e.value.retry is False


def test_a_time_with_a_zone_is_queued_on_the_local_clock(paths):
    from zoneinfo import ZoneInfo
    box = Outbox(paths)
    at = datetime(2026, 10, 2, 16, 0, tzinfo=ZoneInfo("America/New_York"))           # whatever zone a caller passes
    box.enqueue("summary", "summary|CN|2026-10-02", {"title": "t", "line": "l"}, "CN", "2026-10-02", now=at)
    local = at.astimezone().replace(tzinfo=None)
    assert box.pending(local - timedelta(minutes=1)) == [] and len(box.pending(local)) == 1


def brief(**kw):
    b = {"market": "CN", "day": "2026-10-08", "title": "A 股 · 10-08 周四 开盘前", "warning": None,
         "head": ["数据截至 09-30 收盘", "大盘：上证指数 · 下降"],
         "sections": [{"title": "新信号（09-30 收盘，每条规则最多 3 只）", "lines": [
             {"kind": "group", "text": "天品 · 7 只"},
             {"kind": "stock", "listing_id": "CN:000002", "name": "*ST 万科", "code": "000002", "text": "收 4.26 +4.4%"},
             {"kind": "note", "text": "另有 1 只，见应用「今日」"}]}],
         "footer": "完整名单在电脑上的应用「今日」页", "line": "新信号 1 只"}
    return {**b, **kw}


def test_each_market_links_a_stock_to_its_quote_page():
    assert quote_url("CN:600519") == "https://quote.eastmoney.com/sh600519.html"
    assert quote_url("CN:000002") == "https://quote.eastmoney.com/sz000002.html"
    assert quote_url("CN:300750") == "https://quote.eastmoney.com/sz300750.html"
    assert quote_url("CN:430047") == "https://quote.eastmoney.com/bj430047.html"
    assert quote_url("JP:7203") == "https://kabutan.jp/stock/chart?code=7203"


def test_the_briefing_is_one_card_its_sections_apart_and_its_names_escaped():
    card = brief_card(brief())
    assert card["header"] == {"title": {"tag": "plain_text", "content": "A 股 · 10-08 周四 开盘前"}, "template": "blue"}
    tags = [e["tag"] for e in card["elements"]]
    assert tags == ["div", "hr", "div", "note"]
    body = card["elements"][2]["text"]["content"]
    assert body.splitlines()[0] == "**新信号（09-30 收盘，每条规则最多 3 只）**" and "**天品 · 7 只**" in body
    assert "[&#42;ST 万科](https://quote.eastmoney.com/sz000002.html) 000002  收 4.26 +4.4%" in body   # *ST stays text; a link, not bold
    assert card["elements"][-1]["elements"][0]["content"] == "完整名单在电脑上的应用「今日」页"


def test_a_warning_turns_the_card_orange_and_is_said_first():
    card = brief_card(brief(warning="09-30 的数据没取到"))
    assert card["header"]["template"] == "orange"
    assert card["elements"][0]["text"]["content"].splitlines()[0] == "**09-30 的数据没取到**"


def test_a_card_too_long_is_cut_from_its_longest_section_and_says_so():
    many = [{"kind": "stock", "listing_id": f"CN:{600000 + i}", "name": "股票", "code": str(600000 + i), "text": "公告：" + "长" * 200}
            for i in range(200)]
    card = brief_card(brief(sections=[{"title": "新信号", "lines": many}]))
    assert len(json.dumps(card, ensure_ascii=False).encode()) <= MAX_BYTES
    assert card["elements"][-1]["elements"][0]["content"].endswith("太长，余下的见应用「今日」")


def test_a_whole_card_goes_alone_and_is_given_up_after_it_expires(paths):
    box = Outbox(paths)
    at = datetime(2026, 10, 1, 8, 50)
    box.enqueue("brief", "brief|CN|2026-10-01", {"title": "t", "line": "l", "card": {"elements": []}, "expires": "2026-10-01T15:00:00"},
                "CN", "2026-10-01", now=at)
    box.enqueue("live", "live|1", {"title": "股票", "line": "盘中"}, "CN", "2026-10-01", now=at)
    sent = []

    class Bot:
        def send(self, card):
            sent.append(card)
    assert drain(box, Bot(), at) == 2 and sent[0] == {"elements": []} and len(sent) == 2
    box.enqueue("brief", "brief|CN|2026-10-02", {"title": "t", "line": "l", "card": {}, "expires": "2026-10-02T15:00:00"},
                "CN", "2026-10-02", now=at)
    assert drain(box, Bot(), datetime(2026, 10, 2, 15, 1)) == 0 and box.stats()["given_up"] == 1


def test_lark_mds_marks_in_a_text_are_escaped_as_feishus_card_docs_say():
    card = brief_card(brief(head=["A~B <c> d_e `f`"]))
    assert card["elements"][0]["text"]["content"].splitlines()[0] == "A&sim;B &#60;c&#62; d&#95;e &#96;f&#96;"
