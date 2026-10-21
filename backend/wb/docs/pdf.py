"""公告正文 (spec §10.3 文档片段): text out of announcement PDFs, and the excerpt a research gets. A PDF from the internet is
untrusted input, and pypdf's own advisories are a list of inputs that run for minutes or take gigabytes: the parsing
runs in a separate process, each file within its own time (a file past it is stopped with the process, and the rest
are read by a new one), with pypdf's resource limits lowered and no outside program ever called (`jbig2dec` decodes
JBIG2 images; text needs no images); only the first pages are read, only so much text kept, and what comes back is
plain UTF-8 sent as JSON. A file that cannot be read says why; the others still come."""
import io
import json
import multiprocessing
import re
import time

MAX_BYTES = 20_000_000   # an announcement is a few hundred KB; periodic reports are not read for excerpts
PAGES = 10
CHARS = 20_000
TIMEOUT = 30.0           # seconds, for the whole batch
PER_FILE = 15.0          # seconds for one file (real announcements take 0.02–0.6 s)
STARTUP = 10.0           # more for a reading process's first file: starting it (the packaged app starts itself again)
LIMIT = 5_000_000        # decompressed bytes; a page of text is tens of KB (within pypdf's 75 MB defaults a 26 KB file
                         # inflated to 17.5 MB of text operators and took ~830 MB before it was stopped)
LIMITS = {"jbig2dec_binary": None, "maximum_declared_stream_length": LIMIT, "zlib_maximum_output_length": LIMIT,
          "lzw_maximum_output_length": LIMIT, "run_length_maximum_output_length": LIMIT,
          "array_based_stream_maximum_output_length": LIMIT, "image_maximum_buffer_size": LIMIT,
          "xform_maximum_invocations_per_extraction": 1_000}


def _clean(s: str) -> str:
    """UTF-8 and nothing else: a broken ToUnicode map gives lone surrogates, which no file, hash or database takes."""
    return s.encode("utf-8", "replace").decode("utf-8")


def _read(conn, pdfs: list[bytes], pages: int, chars: int) -> None:
    """In the separate process: each PDF's text, each page ending in a form feed (\\f, so a page can be found again),
    or why not; sent as JSON (never a pickle) as soon as it is read."""
    try:
        from pypdf import PdfReader, apply_configuration
        with apply_configuration(**LIMITS):
            for data in pdfs:
                try:
                    text = ""
                    for page in PdfReader(io.BytesIO(data)).pages[:pages]:
                        text += (page.extract_text() or "") + "\f"
                        if len(text) >= chars:
                            break
                    got = ("ok", _clean(text[:chars]))
                except Exception as e:  # noqa: BLE001 - a damaged or hostile file: said, and the next one read
                    got = ("error", _clean(f"{type(e).__name__}: {e}"[:300]))
                conn.send_bytes(json.dumps(got).encode())
    except Exception as e:  # noqa: BLE001 - the reader itself could not start: said for each file, not read as a timeout
        for _ in pdfs:
            conn.send_bytes(json.dumps(("error", _clean(f"读取进程起不来：{type(e).__name__}: {e}"[:300]))).encode())
    finally:
        conn.close()


def texts(pdfs: list[bytes], *, timeout: float = TIMEOUT, per_file: float = PER_FILE, pages: int = PAGES, chars: int = CHARS,
          max_bytes: int = MAX_BYTES) -> list[tuple[str, str]]:
    """Each PDF's text — ("ok", text) or ("error", why) — in order. One reading process reads the batch; a file that runs
    past its own time is stopped with that process, and the files after it are read by a new one."""
    # ponytail: no memory ceiling of its own (none that works on Windows too); the lowered limits and the time bound it
    out: list[tuple[str, str] | None] = [("error", f"PDF 太大（{len(d) / 1e6:.1f} MB），没读") if len(d) > max_bytes else None
                                         for d in pdfs]
    todo = [i for i, o in enumerate(out) if o is None]
    deadline = time.monotonic() + timeout
    while todo and time.monotonic() < deadline:
        ctx = multiprocessing.get_context("spawn")   # the packaged app calls freeze_support (packaging/wb_backend.py)
        recv, send = ctx.Pipe(duplex=False)
        p = ctx.Process(target=_read, args=(send, [pdfs[i] for i in todo], pages, chars), daemon=True)
        p.start()
        send.close()
        allowed, read = per_file + STARTUP, 0
        for i in todo:
            left = min(allowed, deadline - time.monotonic())
            try:
                got = tuple(json.loads(recv.recv_bytes())) if left > 0 and recv.poll(left) else None
            except (EOFError, OSError):   # the process ended early (out of memory, killed)
                got = ("error", "读取进程意外退出（内存不够？），没读出来")
            read += 1
            out[i] = got or ("error", "超时，没读完（文件可能有问题）")
            if got is None or got[1].startswith("读取进程意外退出"):
                break                  # this process is done for: the rest go to a new one
            allowed = per_file
        if p.is_alive():
            p.terminate()
            p.join(5)
        if p.is_alive():
            p.kill()
        p.join(5)
        recv.close()
        todo = todo[read:]
    return [o or ("error", "超时，没来得及读") for o in out]


PAGE_NO = re.compile(r"^[-—\s]*\d+[-—\s]*$|^\d+\s*/\s*\d+$")   # "1", "- 2 -", "2/110"
CJK = re.compile(r"[　-〿一-鿿＀-￯]")
ASSURANCE = re.compile(r"保证[^。]{0,200}?(?:法律责任|重大遗漏)。")
GAP = re.compile(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])")   # "高 鹏 先 生": spaces a PDF put between characters


def excerpt(text: str, size: int = 600) -> str:
    """What an announcement says, for the evidence pack: its key points (重要内容提示) when it has them, else what follows
    the board's standard assurance; page numbers dropped, lines broken by the PDF joined; at most `size` characters."""
    joined = ""
    for line in (x.strip() for x in text.splitlines()):
        if not line or PAGE_NO.match(line):
            continue
        if joined and not (CJK.match(joined[-1]) or CJK.match(line[0])):
            joined += " "
        joined += line
    k = joined.find("重要内容提示")
    if 0 <= k < 800:
        start = k
    else:
        m = ASSURANCE.search(joined[:800])
        start = m.end() if m else 0
    body = GAP.sub("", joined[start:])
    if len(body) <= size:
        return body
    head = body[:size]   # cut at a sentence's end near the limit; else never inside a number (the research's
    k = max(head.rfind(c, max(0, size - 120)) for c in "。；！？")   # number check would vouch for a cut-off one)
    if k >= 0:
        return head[:k + 1] + "…"
    return re.sub(r"[\d,.%\-]+$", "", head).rstrip() + "…"
