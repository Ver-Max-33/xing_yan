"""立花证券 e支店 API (v4r9) as the JP realtime source of the prod profile (spec §4.8, §1.6). Read-only by construction:
`_request` refuses every sCLMID but the three in ALLOWED — log in, the price snapshot, log out — and the second
password an order needs is never asked for, stored or sent (a test keeps order request IDs out of the codebase).

Login (public-key authentication): your auth id goes to <base>/auth/ as CLMAuthLoginRequest; the reply carries the
day's virtual URLs encrypted with your public key, opened here with your private key (RSA-OAEP, SHA-256). While the
phone-authentication transition lasts, phone first and log in within 3 minutes — so a login is only ever made when
you ask for it. The URLs serve until the night close and stay in memory only: whoever holds them can use the API.

A request is its URL, "?", then the request JSON (percent-quoted where a URL needs it, as the samples' urllib3 does),
numbered (p_no, rising) and timed (p_sd_date, JST "YYYY.MM.DD-hh:mm:ss.sss"); replies are JSON in Shift-JIS, and
p_errno and sResultCode 0 mean success. The snapshot answers up to 120 stocks a call with the day so far; push (the
EVENT I/F over WebSocket) is not used yet. Unchecked against the live service until an account is open (§4.8)."""
import base64
import json
import threading
from collections.abc import Iterable
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from wb.listing import parse_id
from wb.realtime.providers.tushare_rt import SNAPSHOT
from wb.registry import REALTIME
from wb.secrets import get_secret

DEMO_URL = "https://demo-kabuka.e-shiten.jp/e_api_v4r9/"   # production: set tachibana_url (a separate auth id and key)
ALLOWED = frozenset({"CLMAuthLoginRequest", "CLMAuthLogoutRequest", "CLMMfdsGetMarketPrice"})
COLUMNS = ("pDPP", "tDPP:T", "pDOP", "pDHP", "pDLP", "pDV", "pDJ", "pPRP")   # now, its time, open, high, low, volume, value, prev
URL_KEYS = ("sUrlRequest", "sUrlMaster", "sUrlPrice", "sUrlEvent", "sUrlEventWebSocket")
MAX_ISSUES = 120
JST = ZoneInfo("Asia/Tokyo")
SESSIONS: dict[str, dict] = {}   # base URL -> {day, urls, p_no}: the day's session, shared by every Tachibana object
_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None   # one for the process: a source object is made every 15 s


def _base(url: str | None) -> str:
    return (url or DEMO_URL).strip().rstrip("/") + "/"


def _client() -> httpx.Client:
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(timeout=15)
        return _CLIENT


class TachibanaError(Exception):
    pass


class NotLoggedIn(TachibanaError):
    pass


def check_url(url: str) -> str:
    """The address your auth id goes to: an e支店 one over HTTPS (demo or production), ending in "/"."""
    u = urlparse(url.strip())
    if u.scheme != "https" or not (u.hostname or "").endswith(".e-shiten.jp"):
        raise TachibanaError(f"立花证券的接入地址必须是 https://….e-shiten.jp/… 的 e支店 地址（演示环境 {DEMO_URL}）")
    return _base(url)


def status() -> dict:
    """configured (auth id and key file set), base, demo, logged_in (a session for today), day."""
    base = _base(get_secret("tachibana_url"))
    s = SESSIONS.get(base)
    today = datetime.now(JST).date()
    return {"configured": bool(get_secret("tachibana_auth_id") and get_secret("tachibana_key_file")), "base": base,
            "demo": "demo" in base, "logged_in": bool(s and s["day"] == today), "day": s["day"].isoformat() if s and s["day"] == today else None}


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@REALTIME.register("tachibana")
class Tachibana:
    name = "tachibana"
    kind = "snapshot"
    delay = 0
    poll_seconds = 60
    max_listings = None   # the snapshot asks MAX_ISSUES at a time, as many times as needed
    approx_volume = False

    def __init__(self, market: str = "JP", transport: httpx.BaseTransport | None = None):
        self.market = market
        self.base = _base(get_secret("tachibana_url"))
        self.http = httpx.Client(transport=transport, timeout=15) if transport else _client()

    # ---- the wire ---------------------------------------------------------------------------------------------
    def _request(self, url: str, clmid: str, method: str = "GET", **fields) -> dict:
        if clmid not in ALLOWED or "sCLMID" in fields:
            raise TachibanaError(f"{fields.get('sCLMID', clmid)} 不在只读白名单里（只能登录、取时价快照、登出）")
        with _LOCK:
            session = SESSIONS.get(self.base)
            p_no = (session["p_no"] + 1) if session and clmid != "CLMAuthLoginRequest" else 1
            if session and clmid != "CLMAuthLoginRequest":
                session["p_no"] = p_no
        now = datetime.now(JST)
        req = {**fields, "p_no": str(p_no), "p_sd_date": now.strftime("%Y.%m.%d-%H:%M:%S.") + f"{now.microsecond // 1000:03d}",
               "sCLMID": clmid, "sJsonOfmt": "5"}
        query = quote(json.dumps(req, ensure_ascii=False, separators=(",", ":")), safe="!$&'()*+,;=:@/?")
        # an error never repeats the URL: the day's URLs work as a login, and a login's carries your auth id (§3.2)
        try:
            r = self.http.request(method, f"{url}?{query}")
        except httpx.HTTPError as e:
            raise TachibanaError(f"连不上立花证券（{clmid}：{type(e).__name__}）") from None
        if r.status_code != 200:
            raise TachibanaError(f"立花证券返回 HTTP {r.status_code}（{clmid}）")
        try:
            body = json.loads(r.content.decode("cp932", errors="replace"))
        except ValueError:
            raise TachibanaError(f"立花证券的回复不是 JSON（{clmid}；可能在维护）") from None
        if str(body.get("p_errno", "0")) != "0" or str(body.get("sResultCode", "0")) != "0":
            raise TachibanaError(f"立花证券拒绝了 {clmid}：p_errno={body.get('p_errno')} sResultCode={body.get('sResultCode')} "
                                 f"{body.get('p_err') or body.get('sResultText') or ''}".strip())
        return body

    # ---- login --------------------------------------------------------------------------------------------------
    def login(self) -> dict:
        """The day's session (asked for by you: during the transition, phone authentication must come first)."""
        self.base = check_url(self.base)   # one set in .env is checked here as the settings page checks it
        auth_id, key_file = get_secret("tachibana_auth_id"), get_secret("tachibana_key_file")
        if not auth_id or not key_file:
            raise TachibanaError("立花证券还没配置：在 .env 或钥匙串里放认证 ID（tachibana_auth_id）和私钥文件的路径（tachibana_key_file）")
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        password = get_secret("tachibana_key_pass")
        try:
            key = serialization.load_pem_private_key(Path(key_file).expanduser().read_bytes(), password.encode() if password else None)
        except TypeError:   # a passphrase missing, or given for a key that has none
            raise TachibanaError("私钥加了口令就在 .env 或钥匙串里填 tachibana_key_pass；没加口令就把它删掉") from None
        except ValueError:
            raise TachibanaError("私钥文件读不出来：要的是 PEM 格式的私钥（口令不对也是这样）") from None
        body = self._request(self.base.rstrip("/") + "/auth/", "CLMAuthLoginRequest", method="POST", sAuthId=auth_id)
        if str(body.get("sKinsyouhouMidokuFlg", "0")) == "1":
            raise TachibanaError("立花证券有未读的契约前书面：先在 e支店 网站上读完再登录")
        oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        try:
            urls = {k: key.decrypt(base64.b64decode(str(body[k]).strip().replace('"', "")), oaep).decode("utf-8-sig").strip()
                    for k in URL_KEYS if body.get(k)}
        except ValueError:
            raise TachibanaError("私钥和账户登记的公开键不匹配（演示环境和正式环境各有一套认证 ID 和私钥）") from None
        if "sUrlPrice" not in urls:
            raise TachibanaError("立花证券的登录回复里没有时价的地址")
        with _LOCK:
            SESSIONS[self.base] = {"day": datetime.now(JST).date(), "urls": urls, "p_no": int(body.get("p_no") or 1)}
        return {"day": SESSIONS[self.base]["day"], "base": self.base}

    def _session(self) -> dict:
        s = SESSIONS.get(self.base)
        if s is None or s["day"] != datetime.now(JST).date():
            raise NotLoggedIn("立花证券今天还没登录：在 设置 → 数据 里点「登录立花证券」（过渡期内先打电话认证，3 分钟内登录）")
        return s

    # ---- the day so far -------------------------------------------------------------------------------------------
    def snapshot(self, listing_ids: Iterable[str], day: date) -> pl.DataFrame:
        """listing_id, at (the time of the price now), open, high, low, last, volume and amount since the open,
        pre_close — stocks that have traded today only. Indexes are not asked for (their codes are not stock codes)."""
        codes = [parse_id(x)[1] for x in listing_ids if ":IDX:" not in x]
        if not codes:
            return pl.DataFrame(schema=SNAPSHOT)
        url = self._session()["urls"]["sUrlPrice"]
        out = []
        for k in range(0, len(codes), MAX_ISSUES):
            body = self._request(url, "CLMMfdsGetMarketPrice", sTargetIssueCode=",".join(codes[k:k + MAX_ISSUES]),
                                 sTargetColumn=",".join(COLUMNS))
            for q in body.get("aCLMMfdsMarketPrice") or []:
                last, at = _num(q.get("pDPP")), q.get("tDPP:T")
                if last is None or not at or _num(q.get("pDOP")) is None:   # no trade yet today (no open: not yesterday's price)
                    continue
                hh, mm = (int(x) for x in str(at).split(":")[:2])
                out.append((f"JP:{q['sIssueCode']}", datetime.combine(day, time(hh, mm)), _num(q.get("pDOP")), _num(q.get("pDHP")),
                            _num(q.get("pDLP")), last, _num(q.get("pDV")), _num(q.get("pDJ")), _num(q.get("pPRP"))))
        return pl.DataFrame(out, schema=SNAPSHOT, orient="row")
