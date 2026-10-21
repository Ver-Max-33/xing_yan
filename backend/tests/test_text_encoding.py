"""Every text file the backend opens says its encoding: Windows reads one without it as cp1252 (GitHub's runners, and
many PCs), which refuses or garbles Chinese (10-21: the build check's stand-in model read its canned reply that way and
the first Windows build in xing_yan failed; cp1252 had stopped Windows runs twice before)."""
import ast
import warnings
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def unspecified(path: Path) -> list[int]:
    """The lines of open() / read_text() / write_text() calls with neither an encoding nor a binary mode."""
    out = []
    with warnings.catch_warnings():   # PA_Agent's own "\d" in a docstring (incremental_shift.py) is no concern here
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))):
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        if name not in ("open", "read_text", "write_text") or "encoding" in {k.arg for k in n.keywords}:
            continue
        modes = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        modes += [k.value.value for k in n.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
        if not any("b" in m for m in modes if len(m) <= 3):
            out.append(n.lineno)
    return out


def test_every_text_file_opened_says_its_encoding():
    files = [p for d in ("wb", "pa/pa_agent", "packaging", "tests") for p in (BACKEND / d).rglob("*.py")]
    assert len(files) > 100
    assert [f"{p.relative_to(BACKEND)}:{n}" for p in sorted(files) for n in unspecified(p)] == []


def test_the_scan_would_see_one(tmp_path):
    f = tmp_path / "x.py"
    f.write_text('a = p.read_text()\nb = open(p, "rb")\nc = p.read_text(encoding="utf-8")\nopen(p, "w").write("x")\n',
                 encoding="utf-8")
    assert unspecified(f) == [1, 4]
