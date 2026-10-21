"""The prod profile's realtime sources (spec §4.8) give the day so far as a snapshot — open, high, low, the price now,
volume and turnover since the open, the previous close — not minute bars. Tushare rt_k (CN) answers for every stock
asked in one call; without its separate permission it says so. Tachibana (JP) is read-only: login, the price snapshot
and logout are the only requests it can make (spec §1.6), and it never asks for the second password."""
from datetime import date, datetime

import pytest

from wb.realtime.providers.tushare_rt import TushareRealtime, ts_code

DAY = date(2026, 10, 9)


def test_listing_ids_become_tushare_codes():
    assert [ts_code(x) for x in ("CN:600519", "CN:000001", "CN:300750", "CN:688001", "CN:920001", "CN:830799")] == [
        "600519.SH", "000001.SZ", "300750.SZ", "688001.SH", "920001.BJ", "830799.BJ"]


def test_rt_k_gives_each_stocks_day_so_far_in_one_call():
    # fields as the rt_k documentation lists them: prices in yuan, vol in shares, amount in yuan
    calls = []

    def call(api, fields, params):
        calls.append((api, params))
        return [["600519.SH", 1250.0, 1255.0, 1268.5, 1249.0, 1262.3, 2312345.0, 2915432100.0, "2026-10-09 10:31:05"],
                ["000001.SZ", 11.5, 11.52, 11.7, 11.48, 11.66, 85600000.0, 995000000.0, "2026-10-09 10:31:03"]]
    snap = TushareRealtime("CN", call=call).snapshot(["CN:600519", "CN:000001", "CN:IDX:000001"], DAY)
    assert calls == [("rt_k", {"ts_code": "600519.SH,000001.SZ"})]                     # indexes need another permission
    row = snap.filter(snap["listing_id"] == "CN:600519").row(0, named=True)
    assert (row["at"], row["open"], row["high"], row["low"], row["last"], row["volume"], row["pre_close"]) == (
        datetime(2026, 10, 9, 10, 31, 5), 1255.0, 1268.5, 1249.0, 1262.3, 2312345.0, 1250.0)


def test_a_stock_not_traded_yet_has_no_price_and_another_day_is_left_out():
    def call(api, fields, params):
        return [["600519.SH", 1250.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "2026-10-09 09:20:00"],
                ["000001.SZ", 11.5, 11.52, 11.7, 11.48, 11.66, 85600000.0, 995000000.0, "2026-10-08 15:00:00"]]
    snap = TushareRealtime("CN", call=call).snapshot(["CN:600519", "CN:000001"], DAY)
    assert snap.is_empty()                                     # before the first trade, and yesterday's close: no day yet


def test_a_trade_time_without_its_date_is_the_days():
    # the documentation lists trade_time without its format: both shapes are read (to check once the permission is on)
    def call(api, fields, params):
        return [["600519.SH", 1250.0, 1255.0, 1268.5, 1249.0, 1262.3, 2312345.0, 2915432100.0, "10:31:05"]]
    assert TushareRealtime("CN", call=call).snapshot(["CN:600519"], DAY)["at"].to_list() == [datetime(2026, 10, 9, 10, 31, 5)]


def test_without_the_rt_permission_it_says_so():
    from wb.data.providers.tushare import PermissionDenied

    def call(api, fields, params):
        raise PermissionDenied(40203, "抱歉，您没有接口(rt_k)访问权限，权限的具体详情访问：https://tushare.pro/document/1?doc_id=108。")
    with pytest.raises(PermissionDenied, match="rt_k"):
        TushareRealtime("CN", call=call).snapshot(["CN:600519"], DAY)


# ---- Tachibana e支店 (v4r9), against a stand-in server shaped like the official samples (no account yet: spec §4.8) ----
import base64  # noqa: E402
import json  # noqa: E402
from urllib.parse import unquote, urlsplit  # noqa: E402

import httpx  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from wb.realtime.providers import tachibana as tb  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Server:
    """The e支店 API as the samples show it: JSON in the query, Shift-JIS JSON back, the session's URLs encrypted."""
    def __init__(self, unread="0"):
        self.seen, self.unread = [], unread

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        req = json.loads(unquote(url.query))
        self.seen.append((request.method, url.path, req))
        if req["sCLMID"] == "CLMAuthLoginRequest":
            enc = lambda u: base64.b64encode(KEY.public_key().encrypt(u.encode(), padding.OAEP(   # noqa: E731
                mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()
            body = {"p_no": req["p_no"], "p_errno": "0", "sResultCode": "0", "sKinsyouhouMidokuFlg": self.unread,
                    **{k: enc(f"https://demo-kabuka.e-shiten.jp/e_api_v4r9/{k}/SESSION/") for k in tb.URL_KEYS}}
        else:
            codes = req["sTargetIssueCode"].split(",")
            body = {"p_no": req["p_no"], "p_errno": "0", "sResultCode": "0", "aCLMMfdsMarketPrice": [
                {"sIssueCode": c, "pDPP": "2861.5", "tDPP:T": "10:31", "pDOP": "2850", "pDHP": "2870", "pDLP": "2841",
                 "pDV": "1234500", "pDJ": "3532100000", "pPRP": "2845.5"} if c not in ("6758", "6861") else
                {"sIssueCode": c, "pDPP": "", "tDPP:T": "", "pDOP": "", "pDHP": "", "pDLP": "", "pDV": "0", "pDJ": "0", "pPRP": "3412"}
                if c == "6758" else   # 6861: yesterday's price and time before today's first trade (no open today)
                {"sIssueCode": c, "pDPP": "66320", "tDPP:T": "15:30", "pDOP": "", "pDHP": "", "pDLP": "", "pDV": "0", "pDJ": "0", "pPRP": "66320"}
                for c in codes]}
        return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode("shift_jis"))


@pytest.fixture
def server(tmp_path, monkeypatch):
    pem = tmp_path / "e_api_private_key.pem"
    pem.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    secrets = {"tachibana_auth_id": "AUTH-ID", "tachibana_key_file": str(pem)}
    monkeypatch.setattr(tb, "get_secret", lambda name: secrets.get(name))
    tb.SESSIONS.clear()
    s = Server()
    return s, lambda: tb.Tachibana("JP", transport=httpx.MockTransport(s))


def test_login_opens_the_days_urls_with_your_private_key_and_asks_for_nothing_else(server):
    s, make = server
    make().login()
    method, path, req = s.seen[0]
    assert (method, path, req["sCLMID"], req["sAuthId"], req["sJsonOfmt"]) == ("POST", "/e_api_v4r9/auth/", "CLMAuthLoginRequest", "AUTH-ID", "5")
    assert set(req) == {"p_no", "p_sd_date", "sCLMID", "sAuthId", "sJsonOfmt"}                   # no password of any kind
    assert tb.SESSIONS[tb.DEMO_URL]["urls"]["sUrlPrice"] == "https://demo-kabuka.e-shiten.jp/e_api_v4r9/sUrlPrice/SESSION/"


def test_the_snapshot_asks_the_price_url_for_the_days_quotes_numbered_and_timed(server):
    s, make = server
    make().login()
    snap = make().snapshot(["JP:7203", "JP:6758", "JP:6861", "JP:IDX:N225"], date(2026, 10, 9))
    method, path, req = s.seen[1]
    assert (method, path, req["sCLMID"], req["sTargetIssueCode"]) == ("GET", "/e_api_v4r9/sUrlPrice/SESSION/", "CLMMfdsGetMarketPrice", "7203,6758,6861")
    assert req["sTargetColumn"] == "pDPP,tDPP:T,pDOP,pDHP,pDLP,pDV,pDJ,pPRP" and int(req["p_no"]) > int(s.seen[0][2]["p_no"])
    assert len(req["p_sd_date"]) == 23 and req["p_sd_date"][4] == "." and req["p_sd_date"][10] == "-"   # YYYY.MM.DD-hh:mm:ss.sss
    assert snap.select("listing_id", "at", "open", "high", "low", "last", "volume", "amount", "pre_close").rows() == [
        ("JP:7203", datetime(2026, 10, 9, 10, 31), 2850.0, 2870.0, 2841.0, 2861.5, 1234500.0, 3532100000.0, 2845.5)]   # 6758, 6861: no trade yet


def test_without_a_login_today_or_with_unread_documents_it_says_what_to_do(server):
    s, make = server
    with pytest.raises(tb.NotLoggedIn, match="登录"):
        make().snapshot(["JP:7203"], date(2026, 10, 9))
    s.unread = "1"
    with pytest.raises(tb.TachibanaError, match="未读"):
        make().login()


def test_only_the_market_data_requests_can_be_made():
    # spec §1.6: login, the price snapshot and logout; anything else is refused before it leaves
    assert tb.ALLOWED == {"CLMAuthLoginRequest", "CLMAuthLogoutRequest", "CLMMfdsGetMarketPrice"}
    with pytest.raises(tb.TachibanaError, match="不在只读白名单"):
        tb.Tachibana("JP", transport=httpx.MockTransport(lambda r: httpx.Response(500)))._request("https://x/", "CLMMfdsGetMasterData")



def test_an_http_error_never_carries_the_session_url_or_the_auth_id(server):
    # spec §3.2: a key never shows or reaches a log; the day's URLs work as a login for whoever holds them
    s, make = server
    make().login()
    refuse = lambda request: httpx.Response(403, content=b"<html>forbidden</html>")  # noqa: E731
    with pytest.raises(tb.TachibanaError) as e:
        tb.Tachibana("JP", transport=httpx.MockTransport(refuse)).snapshot(["JP:7203"], date(2026, 10, 9))
    assert "403" in str(e.value) and "SESSION" not in str(e.value) and "?" not in str(e.value)
    tb.SESSIONS.clear()
    with pytest.raises(tb.TachibanaError) as e:
        tb.Tachibana("JP", transport=httpx.MockTransport(lambda r: httpx.Response(503))).login()
    assert "AUTH-ID" not in str(e.value) and "503" in str(e.value)
    with pytest.raises(tb.TachibanaError, match="不是 JSON"):
        tb.Tachibana("JP", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html/>"))).login()


def test_a_field_cannot_rename_the_request():
    with pytest.raises(tb.TachibanaError, match="不在只读白名单"):
        tb.Tachibana("JP", transport=httpx.MockTransport(lambda r: httpx.Response(500)))._request(
            "https://x.e-shiten.jp/", "CLMMfdsGetMarketPrice", sCLMID="CLM" + "Other")


def test_an_address_without_its_last_slash_is_the_same_session(server, monkeypatch):
    s, make = server
    secrets = {"tachibana_auth_id": "AUTH-ID", "tachibana_key_file": tb.get_secret("tachibana_key_file"),
               "tachibana_url": "https://demo-kabuka.e-shiten.jp/e_api_v4r9"}
    monkeypatch.setattr(tb, "get_secret", lambda name: secrets.get(name))
    make().login()
    assert tb.status()["logged_in"] is True and make().snapshot(["JP:7203"], date(2026, 10, 9)).height == 1


def test_a_key_that_does_not_open_says_why_in_words(server, tmp_path, monkeypatch):
    s, make = server
    locked = tmp_path / "locked.pem"
    locked.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.BestAvailableEncryption(b"pw")))
    other = tmp_path / "other.pem"
    other.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for path, want in ((locked, "tachibana_key_pass"), (other, "公开键")):
        monkeypatch.setattr(tb, "get_secret", lambda name, p=path: {"tachibana_auth_id": "AUTH-ID", "tachibana_key_file": str(p)}.get(name))
        with pytest.raises(tb.TachibanaError, match=want):
            make().login()


def test_more_than_120_stocks_are_asked_in_turns_and_one_not_traded_today_has_no_price_yet(server):
    # no cap of its own: the snapshot asks 120 at a time; a stock with no open today is not given yesterday's price
    s, make = server
    make().login()
    codes = [f"JP:{1000 + k}" for k in range(130)]
    make().snapshot(codes, date(2026, 10, 9))
    asked = [len(r["sTargetIssueCode"].split(",")) for _, _, r in s.seen[1:]]
    assert asked == [120, 10] and tb.Tachibana.max_listings is None


def test_every_tachibana_object_shares_one_http_client():
    # a source is made every 15 s: one client a process, not a new open one each time
    assert tb.Tachibana("JP").http is tb.Tachibana("JP").http
