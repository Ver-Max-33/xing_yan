"""Record the 市场 page's states from real data (design 2026-10-12-market-page-redesign §9.2): what the backend answers
in each state of §5 — the board, the day's record, the industries' standings — into fixtures for the end-to-end tests.

    http <base_url> <state> [--token T]   GET them from a running backend: the states that only happen in the session
    derive <data_dir> <state>              an app in this process over a copy of the data, its clock frozen at the
                                           state's moment, its tape ticked by the real TickFlow source or one that fails

Run from backend/: `uv run --frozen python -m scripts.market.record_states …`. The backend reads its keys itself
(WB_ENV_FILE); nothing here prints one. `derive` writes into the data folder it is given (the tape's rows, caches):
a copy only (cp -c -R), never your own folder — it refuses ~/WorkbenchData."""
import argparse
import json
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "desktop" / "tests" / "fixtures" / "market"
H = {"X-WB-Token": "rec"}
# public large caps that stand in for your own stocks (only the old page's check carries any)
PUBLIC = [("CN:600519", "贵州茅台"), ("CN:601398", "工商银行"), ("CN:600036", "招商银行"), ("CN:601318", "中国平安"),
          ("CN:000858", "五粮液"), ("CN:300750", "宁德时代"), ("CN:002594", "比亚迪"), ("CN:600900", "长江电力"),
          ("CN:601288", "农业银行"), ("CN:601988", "中国银行"), ("CN:600030", "中信证券"), ("CN:000333", "美的集团"),
          ("CN:601012", "隆基绿能"), ("CN:600276", "恒瑞医药"), ("CN:000651", "格力电器"), ("CN:002415", "海康威视"),
          ("CN:600887", "伊利股份"), ("CN:601899", "紫金矿业"), ("CN:600309", "万华化学"), ("CN:688981", "中芯国际")]

# state: the moment (Beijing), the market, how the tape is ticked before asking (None: not at all; "real": the TickFlow
# source; "fail": one that raises), a second tick (moment, how), and the newest snapshot allowed (stale states)
STATES = {
    "old-1009": {"at": (2026, 10, 11, 20, 0)},                   # the old page as in your screenshots: 10-09's close
    "pre-1009": {"at": (2026, 10, 12, 8, 50)},
    "closed-1011": {"at": (2026, 10, 11, 12, 0)},
    "after-1012": {"at": (2026, 10, 12, 19, 0), "tick": "real"},
    "stale-1012": {"at": (2026, 10, 12, 19, 0), "tick": "real", "before": date(2026, 10, 9)},
    "after-cold-1012": {"at": (2026, 10, 12, 19, 0), "tick": "real"},   # on a copy without the day's record: started after the close
    "down-open": {"at": (2026, 10, 12, 10, 0), "tick": "fail"},
    "down-mid": {"at": (2026, 10, 12, 14, 50), "tick": "real", "then": ((2026, 10, 12, 14, 53), "fail")},
    "down-mid-1013": {"at": (2026, 10, 13, 14, 50), "tick": "real", "then": ((2026, 10, 13, 14, 53), "fail")},   # run at 14:50
    "no-tickflow": {"at": (2026, 10, 12, 10, 0), "key": False},
    "jp-1009": {"at": (2026, 10, 12, 19, 0), "market": "JP"},
    "no-data-cn": {"at": (2026, 10, 12, 10, 0), "empty": True},
    "no-data-jp": {"at": (2026, 10, 12, 10, 0), "empty": True, "market": "JP"},
    "close-1012": {"at": (2026, 10, 12, 20, 0)},
}


def why_not_real(moment: datetime, now: datetime) -> str | None:
    """Why a real tick asked now cannot stand for `moment` (None: it can). TickFlow answers with the quotes of the
    moment it is asked: a moment in the session only from it to two minutes after; after the close (15:05 on), any
    time that evening — the close's quotes stand until the next day."""
    if now.date() != moment.date():
        return f"{moment:%m-%d} 的行情只能在那一天录（现在 {now:%m-%d}）"
    if moment.time() >= time(15, 5):
        return None if now.time() >= time(15, 5) else "收盘后的状态要 15:05 以后录"
    if not moment <= now <= moment + timedelta(minutes=2):
        return f"{moment:%H:%M} 的行情要在 {moment:%H:%M}–{moment + timedelta(minutes=2):%H:%M} 之间录（现在 {now:%H:%M}）"
    return None


class Failing:
    """TickFlow failing: every call raises, as a 500 from the server does."""
    name, delay, poll_seconds, max_listings, approx_volume = "tickflow_rt", 0, 30, 297, False

    def whole_market(self):
        from wb.realtime.providers.tickflow_rt import TickFlowError
        raise TickFlowError("TickFlow 500：Internal Server Error")

    def quotes(self, ids):
        return self.whole_market()


def _head() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()


def _write(out: Path, state: str, recorded: dict, answers: dict) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{state}.json"
    path.write_text(json.dumps({"recorded": recorded, **answers}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def _public(board: dict) -> dict:
    """Your stocks swapped for public large caps (cycling), their industries, changes and distances kept."""
    mine = board.get("mine") or []
    swapped = [{**m, "listing_id": PUBLIC[k % len(PUBLIC)][0], "name": PUBLIC[k % len(PUBLIC)][1]} for k, m in enumerate(mine)]
    return {**board, "mine": swapped}


def _tape_then(c, market: str, board: dict | None, tape: dict | None, now: datetime) -> dict | None:
    """The record as the endpoint gave it at that moment: a copy recorded later already holds the rest of the day, so a
    live state keeps the rows up to its moment; a close (before the open, a closed day, the source down since the open,
    no key) had no rows of today then — the endpoint gives the stored day's."""
    if tape is None or board is None:
        return tape
    if board.get("mode") == "close" and board.get("as_of"):
        r = c.get(f"/api/market/{market}/tape", params={"day": board["as_of"]}, headers=H)
        return r.json() if r.status_code == 200 else tape
    cut = now.strftime("%Y-%m-%dT%H:%M:%S")
    return {**tape, "rows": [r for r in tape["rows"] if r["at"][:19] <= cut]}


def http(base: str, state: str, market: str, token: str, out: Path) -> Path:
    import time as clock

    import httpx
    h = {"X-WB-Token": token}
    with httpx.Client(base_url=base, headers=h, timeout=60) as c:
        for _ in range(3):   # a moment only once a day: a backend busy or restarting is asked again, 10 s apart
            answers = {}
            for key in ("board", "tape", "industries"):
                r = c.get(f"/api/market/{market}/{key}")
                answers[key] = r.json() if r.status_code == 200 else None
            if answers["board"] is not None:
                break
            clock.sleep(10)
        else:
            raise SystemExit(f"{state}: /api/market/{market}/board did not answer three times")
    now = datetime.now(CST).isoformat(timespec="seconds")
    return _write(out, state, {"state": state, "how": "http", "at": now, "clock": answers["board"].get("fetched_at") or now,
                               "code": _head()}, answers)


def derive(data_dir: Path, state: str, out: Path, public_mine: bool) -> Path:
    import os
    if Path(data_dir).resolve() == (Path.home() / "WorkbenchData").resolve():
        raise SystemExit("derive writes into the folder it reads: give it a copy (cp -c -R), not your own data")
    spec = STATES[state]
    market = spec.get("market", "CN")
    if spec.get("key") is False:
        os.environ.pop("WB_SECRET_TICKFLOW_API_KEY", None)
        os.environ["WB_ENV_FILE"] = "/nonexistent/.env"
    from fastapi.testclient import TestClient

    from wb.api.app import create_app
    from wb.data import store as store_mod
    from wb.jobs.live import source_for
    if spec.get("empty"):
        data_dir.mkdir(parents=True, exist_ok=True)
    if spec.get("before"):   # the newest snapshot before that day stands as the latest: yesterday's update failed
        original = store_mod.Store.latest

        def older(self):
            snaps = [s for s in self.history() if s.as_of < spec["before"]]
            return snaps[-1] if snaps else original(self)            # history() is oldest first
        store_mod.Store.latest = older
    app = create_app(data_dir, token="rec")            # no scheduler: no live threads, nothing sent anywhere
    c = TestClient(app)
    lv = app.state.live
    lv.tape.fill = False
    real_session = lv.session

    def freeze(moment):
        now = datetime(*moment, tzinfo=CST)
        lv.session = lambda m, when=None: real_session(m, now)
        return now

    now = freeze(spec["at"])
    for moment, how in [(spec["at"], spec.get("tick"))] + ([spec["then"]] if spec.get("then") else []):
        if how is None:
            continue
        if how == "real" and (why := why_not_real(datetime(*moment, tzinfo=CST), datetime.now(CST))):
            raise SystemExit(f"{state}: {why}")
        now = freeze(moment)
        paths = lv.tape.paths_of(market)
        src = source_for(paths, market) if how == "real" else Failing()
        lv.tape.tick(market, lv.session(market), src, now)
    answers = {}
    for key in ("board", "tape", "industries"):
        r = c.get(f"/api/market/{market}/{key}", headers=H)
        answers[key] = r.json() if r.status_code == 200 else None
    answers["tape"] = _tape_then(c, market, answers["board"], answers["tape"], now)
    if public_mine and answers["board"]:
        answers["board"] = _public(answers["board"])
    return _write(out, state, {"state": state, "how": "derive", "at": datetime(*spec["at"], tzinfo=CST).isoformat(),
                               "clock": now.isoformat(), "market": market, "code": _head()}, answers)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("http")
    h.add_argument("base")
    h.add_argument("state")
    h.add_argument("--market", default="CN")
    h.add_argument("--token", default="")
    d = sub.add_parser("derive")
    d.add_argument("data_dir", type=Path)
    d.add_argument("state", choices=sorted(STATES))
    d.add_argument("--public-mine", action="store_true")
    for p in (h, d):
        p.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    path = http(a.base, a.state, a.market, a.token, a.out) if a.cmd == "http" else derive(a.data_dir, a.state, a.out, a.public_mine)
    print(path)


if __name__ == "__main__":
    main()
