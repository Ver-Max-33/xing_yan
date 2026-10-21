"""Checks a PyInstaller build of the backend against this checkout (spec §3.5, §13 P4 acceptance): the frozen binary
writes demo data, computes the structures through its process pool (the frozen executable started again), runs a
rule, serves, its registries hold as many entries as the source's, it reads a PDF you import (pypdf in the
bundle, in its own process: the frozen executable started again), and it runs a 价格行为 analysis against a stand-in
model (PA_Agent's code, its prompts and our patch in the bundle; the model answers with the tests' canned replies,
both stages, and PA checks its answers with jsonschema). Nothing outside a temporary data directory is touched and
nothing reaches the network.

    cd backend && uv run python packaging/check_build.py [dist/wb-backend]
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


def _exe(folder: Path) -> Path:
    return folder / ("wb-backend.exe" if sys.platform == "win32" else "wb-backend")


def _run(*args: str) -> None:
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"$ {' '.join(Path(a).name if i == 0 else a for i, a in enumerate(args))}\n{(out.stdout + out.stderr).strip()[-600:]}")
    if out.returncode:
        sys.exit(f"失败（退出码 {out.returncode}）")


def _pdf(text: str) -> bytes:
    """A one-page PDF with a line of ASCII text (Helvetica)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for k, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _stand_in_model() -> tuple[ThreadingHTTPServer, dict]:
    """A model with the replies the backend's tests use (tests.pa_fixtures.answers): stage 1 goes on, stage 2 a 限价单
    under K1's close — `k1["close"]`, set before the analysis starts. The frozen app reads both stages' prompts, asks
    twice and runs PA's checks on a plan."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.pa_fixtures import answers
    k1, calls = {}, []

    class Answer(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append(1)
            reply = answers(SimpleNamespace(bars=(SimpleNamespace(close=k1["close"]),)))[min(len(calls), 2) - 1]
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}],
                               "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, k1


def _serve(exe: Path, data: str) -> dict:
    """Serve: the health answer (its registries), and a PDF imported through the API."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    model, k1 = _stand_in_model()
    proc = subprocess.Popen([str(exe), "--data-dir", data, "serve", "--port", str(port)],
                            env={**os.environ, "WB_TOKEN": "check", "WB_SECRET_DEEPSEEK_API_KEY": "stand-in",
                                 "WB_AI_BASE_URL": f"http://127.0.0.1:{model.server_address[1]}"},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    health = json.loads(r.read())
                break
            except OSError:
                time.sleep(0.5)
        else:
            sys.exit("打包后的后端 90 秒内没有就绪")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/docs/ir?listing_id=CN:600519&filename=check.pdf",
                                     data=_pdf("Frozen app reads this page"), method="POST",
                                     headers={"X-WB-Token": "check", "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=60) as r:
            doc = json.loads(r.read())
        print(f"导入的 PDF：{doc['pages']} 页，摘录「{doc['excerpt']}」")
        if doc["excerpt"] != "Frozen app reads this page":
            sys.exit("打包后读不出 PDF 的文字（pypdf 或读取进程）")
        get = lambda path: json.loads(urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            f"http://127.0.0.1:{port}{path}", headers={"X-WB-Token": "check"}), timeout=60).read())
        built_in = [r for r in get("/api/indicators")["indicators"] if r["builtin"]]
        sar = get("/api/indicator-series?listing_id=CN:600519&id=sar")   # the YAML files and a compiled (numba) loop
        print(f"内置指标 {len(built_in)} 个；SAR 在演示股票上 {len(sar.get('t', []))} 根")
        if len(built_in) < 40 or "error" in sar or not sar.get("t"):
            sys.exit(f"打包后的指标库不对：{sar.get('error') or '内置指标没有打进包里'}")
        k1["close"] = get("/api/bars?listing_id=CN:600519&limit=1")["rows"][-1]["close"]
        start = urllib.request.Request(f"http://127.0.0.1:{port}/api/pa", data=json.dumps({"listing_id": "CN:600519"}).encode(),
                                       method="POST", headers={"X-WB-Token": "check", "Content-Type": "application/json"})
        with urllib.request.urlopen(start, timeout=60) as r:
            aid = json.loads(r.read())["id"]
        deadline = time.time() + 120
        while (rec := get(f"/api/pa/{aid}"))["status"] == "running" and time.time() < deadline:
            time.sleep(0.5)
        print(f"价格行为分析：{rec['status']}{'，' + rec['error'] if rec.get('error') else ''}；两步都问了："
              f"{bool(rec.get('stage1') and rec.get('stage2'))}，最后的方案 {(rec.get('final_plan') or {}).get('order_type')}")
        if rec["status"] != "ok" or not rec.get("stage1") or not rec.get("stage2"):
            sys.exit("打包后的价格行为分析没做成：PA 的代码、提示词或改动记录没有打进包里（wb.spec）")
        log = "".join(f.read_text(encoding="utf-8", errors="replace") for f in Path(data, "logs").glob("backend.log*"))
        if not log:
            sys.exit("打包后的后端没有写日志（logs/backend.log），看不出 PA 有没有检查回答的格式")
        if "jsonschema not installed" in log:   # PA then skips checking the answers' format, silently
            sys.exit("打包后没有 jsonschema：PA 不检查 AI 回答的格式（wb.spec）")
        return health
    finally:
        proc.terminate()
        proc.wait(30)
        model.shutdown()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/redirects default to a legacy code page (as wb.cli)
        stream.reconfigure(encoding="utf-8")
    exe = _exe(Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "dist" / "wb-backend"))
    if not exe.exists():
        sys.exit(f"没有找到 {exe}：先运行 PyInstaller")
    from wb import registry
    want = registry.load_all()  # imports every module of each registry, as the server does at start
    with tempfile.TemporaryDirectory() as data:
        _run(str(exe), "--data-dir", data, "devdata")
        _run(str(exe), "--data-dir", data, "structures", "--market", "CN", "--workers", "2")
        _run(str(exe), "--data-dir", data, "rule", "run", "tpl_shang_heng_shang", "--market", "CN", "--workers", "2")
        got = _serve(exe, data)["registries"]
    print(f"注册表：源码 {want}，打包后 {got}")
    if got != want:
        sys.exit("注册表条目数不一致：有模块没有被打进包里（wb.spec 的 collect_submodules）")
    print("打包检查通过")


if __name__ == "__main__":
    main()
