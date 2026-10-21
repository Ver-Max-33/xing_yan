"""Feishu custom bot (spec §7.1, the only channel in V1): a signed webhook, interactive cards in lark_md. Only a
Feishu or Lark custom-bot URL is ever posted to — notices go to your own bot (§4.2, J-Quants terms)."""
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

from wb.data.http import RateLimiter
from wb.registry import CHANNELS

HOSTS = ("open.feishu.cn", "open.larksuite.com")
PER_CARD, MAX_BYTES = 20, 20_000                   # Feishu takes cards up to 30 KB
TAG = {"signal": "新信号", "advance": "潜伏推进", "invalid": "失效", "delay": "数据", "summary": "扫描摘要", "test": "测试",
       "watch": "观察提醒", "live": "盘中", "brief": "开盘前简报"}
# Feishu's own codes; whether trying again later can help
ERRORS = {19021: ("签名校验失败：飞书机器人的签名密钥不对（设置 → 通知）", False),
          19024: ("消息没有通过机器人的关键词校验：在机器人设置里去掉关键词，或加上「形研」", False),
          19007: ("机器人已停用或已被移出群", False),
          9499: ("发送太频繁，被飞书限流", True), 11232: ("发送太频繁，被飞书限流", True)}


class FeishuError(Exception):
    def __init__(self, message: str, retry: bool):
        super().__init__(message)
        self.retry = retry


def check_webhook(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme != "https" or u.hostname not in HOSTS or not u.path.startswith("/open-apis/bot/v2/hook/"):
        raise ValueError("只支持飞书（或 Lark）自定义机器人的 webhook：https://open.feishu.cn/open-apis/bot/v2/hook/…")
    return url.strip()


def sign(timestamp: int, secret: str) -> str:
    """Feishu's signature: HMAC-SHA256 keyed by "timestamp\\nsecret" over nothing, base64."""
    return base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()).decode()


def _block(item: dict) -> dict:
    head = f"【{TAG.get(item.get('kind', ''), '提醒')}】**{item.get('title', '')}**"
    return {"tag": "div", "text": {"tag": "lark_md", "content": f"{head}\n{item.get('line', '')}".strip()}}


def chunks(items: list[dict]) -> list[list[dict]]:
    """Items in groups that each fit one card."""
    out: list[list[dict]] = [[]]
    size = 0
    for it in items:
        n = len(json.dumps(_block(it), ensure_ascii=False).encode())
        if out[-1] and (len(out[-1]) >= PER_CARD or size + n > MAX_BYTES):
            out.append([])
            size = 0
        out[-1].append(it)
        size += n
    return [c for c in out if c]


def cards(items: list[dict], title: str) -> list[dict]:
    groups = chunks(items)
    out = []
    for i, g in enumerate(groups):
        part = f"（{i + 1}/{len(groups)}）" if len(groups) > 1 else ""
        out.append({"config": {"wide_screen_mode": True},
                    "header": {"title": {"tag": "plain_text", "content": f"{title} · {len(g)} 条{part}"}, "template": "blue"},
                    "elements": [_block(it) for it in g]})
    return out


@CHANNELS.register("feishu")
class Feishu:
    NAME = "飞书"
    FIELDS = ({"name": "feishu_webhook", "label": "Webhook 地址", "required": True,
               "hint": "飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人，复制它的 webhook 地址"},
              {"name": "feishu_secret", "label": "签名密钥", "required": False,
               "hint": "机器人的安全设置里勾了「签名校验」才填"})

    def __init__(self, webhook: str, secret: str | None = None, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.url, self.secret, self.clock, self.sleep = check_webhook(webhook), secret, clock, sleep
        self.http = httpx.Client(transport=transport, timeout=15)
        self.limiter = RateLimiter(90, sleep=sleep)   # Feishu: 100 a minute and 5 a second; stay under both
        self._last = 0.0

    def send(self, card: dict) -> None:
        self.limiter.acquire()
        gap = 0.25 - (time.monotonic() - self._last)
        if gap > 0:
            self.sleep(gap)
        body: dict = {"msg_type": "interactive", "card": card}
        if self.secret:
            ts = int(self.clock())
            body |= {"timestamp": str(ts), "sign": sign(ts, self.secret)}
        try:
            r = self.http.post(self.url, json=body)
        except httpx.HTTPError as e:
            raise FeishuError(f"连不上飞书（{type(e).__name__}）", retry=True) from None
        finally:
            self._last = time.monotonic()
        if r.status_code != 200:
            raise FeishuError(f"飞书返回 HTTP {r.status_code}", retry=r.status_code == 429 or r.status_code >= 500)
        try:
            data = r.json()
        except ValueError:
            raise FeishuError("飞书的回复不是 JSON", retry=True) from None
        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, None):
            reason, retry = ERRORS.get(code, (f"飞书返回 {code}：{data.get('msg')}", True))
            raise FeishuError(reason, retry)


# ---- 开盘前简报 (spec §7.1, 1.34): one card ------------------------------------------------------------------
# lark_md's own marks shown as text, escaped the way Feishu's card docs do it (HTML entities)
ESCAPE = str.maketrans({"*": "&#42;", "_": "&#95;", "~": "&sim;", "`": "&#96;", "[": "&#91;", "]": "&#93;", "<": "&#60;", ">": "&#62;"})


def quote_url(listing_id: str) -> str:
    """The stock's quote page on a phone: 东方财富 for A 股, 株探 for 日股."""
    market, code = listing_id.split(":", 1)
    if market == "JP":
        return f"https://kabutan.jp/stock/chart?code={code}"
    return f"https://quote.eastmoney.com/{'sh' if code[:1] in '69' else 'bj' if code[:1] in '48' else 'sz'}{code}.html"


def _md(s: str) -> str:
    return s.translate(ESCAPE)


def _line(x: dict) -> str:
    if x["kind"] == "stock":
        # a link, not bold: Feishu does not say a link may sit inside bold
        return f"[{_md(x['name'])}]({quote_url(x['listing_id'])}) {x['code']}  {_md(x['text'])}".rstrip()
    return f"**{_md(x['text'])}**" if x["kind"] == "group" else _md(x["text"])


def brief_card(b: dict) -> dict:
    """The briefing as a card: what is said first (a warning in bold, the header orange), each section after a rule,
    the footer in grey; cut to fit from the longest section, saying so."""
    first = ([f"**{_md(b['warning'])}**"] if b["warning"] else []) + [_md(h) for h in b["head"]]
    els: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(first)}}]
    for s in b["sections"]:
        els += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join([f"**{_md(s['title'])}**", *map(_line, s["lines"])])}}]
    els.append({"tag": "note", "elements": [{"tag": "plain_text", "content": b["footer"]}]})
    card = {"config": {"wide_screen_mode": True}, "elements": els,
            "header": {"title": {"tag": "plain_text", "content": b["title"]}, "template": "orange" if b["warning"] else "blue"}}
    cut = False
    while len(json.dumps(card, ensure_ascii=False).encode()) > MAX_BYTES:
        divs = [e for e in els[1:] if e["tag"] == "div" and "\n" in e["text"]["content"]]
        if not divs:
            break
        longest = max(divs, key=lambda e: len(e["text"]["content"]))
        longest["text"]["content"] = longest["text"]["content"].rsplit("\n", 1)[0]
        cut = True
    if cut:
        els[-1]["elements"][0]["content"] += " · 太长，余下的见应用「今日」"
    return card
