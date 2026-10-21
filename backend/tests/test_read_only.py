"""Spec §1.6 不含交易功能: no order request of the broker API, and no field for its second password, anywhere in the
code — so not even a bug can place an order (an order needs the second password, which this system never asks for).
The names are put together here so that this file does not carry them either."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ["CLM" + "Kabu" + x for x in ("NewOrder", "CorrectOrder", "CancelOrder")] + ["sSecond" + "Password", "第二" + "暗証番号"]
SOURCES = ("backend/wb/**/*", "backend/tests/**/*", "backend/packaging/**/*", "backend/*.toml", "frontend/src/**/*",
           "frontend/*.html", "frontend/*.ts", "desktop/*.js", "desktop/*.cjs", "desktop/*.mjs", "desktop/tests/**/*", "scripts/**/*")
TEXT = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".cts", ".mts", ".json", ".yaml", ".yml", ".toml", ".sql", ".html",
        ".css", ".sh", ".ps1", ".spec"}


def test_no_order_request_and_no_second_password_anywhere_in_the_code():
    files = {p for g in SOURCES for p in ROOT.glob(g) if p.is_file() and p.suffix in TEXT}
    assert len(files) > 100                                   # the scan reaches the code
    hits = [f"{p.relative_to(ROOT)}: {f}" for p in sorted(files) for f in FORBIDDEN if f in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == []


def test_the_scan_would_see_one():
    assert any(f in "url = base + '?' + json.dumps({'sCLMID': '" + "CLM" + "Kabu" + "NewOrder'})" for f in FORBIDDEN)



def test_every_request_id_in_the_code_is_one_the_whitelist_allows():
    # an order request this test does not know by name (a new member of the 株式注文 family) is caught too
    import re

    from wb.realtime.providers.tachibana import ALLOWED
    found = {m for p in (ROOT / "backend" / "wb").rglob("*.py") for m in re.findall(r"(?<![A-Za-z])CLM[A-Za-z]+", p.read_text(encoding="utf-8", errors="ignore"))}   # not sCLMID or aCLM… keys
    assert found <= set(ALLOWED) and not any(f.startswith("CLM" + "Kabu") for f in found)
