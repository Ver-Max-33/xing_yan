"""packaging/check_build.py on Windows: GitHub's runners give Python a pipe in the ANSI code page (cp1252), and what
the frozen backend says has Chinese in it — the first build there died on the check's own print."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[1] / "packaging" / "check_build.py"


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in backend is a shell script")
def test_the_build_check_prints_the_backends_chinese_on_a_legacy_code_page(tmp_path):
    fake = tmp_path / "wb-backend"   # stands in for the frozen backend: answers in Chinese, then fails
    fake.write_text("#!/bin/sh\necho '上横上：命中 3 只'\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    out = subprocess.run([sys.executable, str(CHECK), str(tmp_path)], capture_output=True,
                         env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert "上横上：命中 3 只" in out.stdout.decode("utf-8", "replace")
    assert "失败（退出码 3）" in out.stderr.decode("utf-8", "replace")
