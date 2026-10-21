"""价格行为分析 (design 2026-10-20 §2, §11): PA_Agent's analysis core is vendored under backend/pa at upstream commit
53149a3. Its prompts are byte-identical; its code is byte-identical except the audited a_share.patch (the four A-share
changes and the package trims). Run `python -m tests.test_pa_vendor --write` to regenerate the patch after an edit."""
import difflib
import hashlib
import importlib
import sys
from pathlib import Path

PA = Path(__file__).resolve().parents[1] / "pa"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest() -> dict[str, str]:
    """Upstream's files at the pinned commit: relative path -> sha256 (written once, from the upstream clone)."""
    out = {}
    for line in (PA / "UPSTREAM.sha256").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split(None, 1)
        out[rel.strip()] = digest
    return out


def patched() -> list[str]:
    """The files we edited: each has its pristine upstream copy under backend/pa/upstream."""
    return sorted(p.relative_to(PA / "upstream").as_posix() for p in (PA / "upstream").rglob("*") if p.is_file())


def patch_text() -> str:
    """The unified diff from every pristine copy to our file, in path order."""
    parts = []
    for rel in patched():
        pristine = (PA / "upstream" / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        ours = (PA / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        parts.extend(difflib.unified_diff(pristine, ours, f"a/{rel}", f"b/{rel}"))
    return "".join(parts)


def test_every_manifest_file_is_vendored_and_unpatched_ones_are_byte_identical():
    for rel, digest in manifest().items():
        assert (PA / rel).is_file(), rel
        if not (PA / "upstream" / rel).exists():
            assert sha256((PA / rel).read_bytes()) == digest, rel


def test_patched_files_differ_from_upstream_only_by_the_audited_patch():
    for rel in patched():
        assert sha256((PA / "upstream" / rel).read_bytes()) == manifest()[rel], rel   # the pristine copy is upstream's
    assert patch_text() == (PA / "a_share.patch").read_text(encoding="utf-8")


def test_no_python_file_outside_the_manifest():
    vendored = {p.relative_to(PA).as_posix() for p in (PA / "pa_agent").rglob("*.py")}
    assert vendored == {r for r in manifest() if r.endswith(".py")}


def test_prompt_files_are_all_there_and_unchanged():
    txt = {r for r in manifest() if r.startswith("prompt_engineering/")}
    assert len(txt) == 29 and not any((PA / "upstream" / r).exists() for r in txt)


def test_the_core_imports_without_gui_or_sdk():
    for m in ("pa_agent.ai.prompt_assembler", "pa_agent.ai.json_validator", "pa_agent.ai.router",
              "pa_agent.orchestrator.validation_retry", "pa_agent.ai.decision_tree", "pa_agent.data.snapshot",
              "pa_agent.config.settings", "pa_agent.util.threading"):
        importlib.import_module(m)
    assert "PyQt6" not in sys.modules and "openai" not in sys.modules
    import pa_agent.config.paths
    assert pa_agent.config.paths.PROMPT_DIR == PA / "prompt_engineering"


if __name__ == "__main__":
    if "--write" in sys.argv:
        (PA / "a_share.patch").write_text(patch_text(), encoding="utf-8")
        print(f"wrote {PA / 'a_share.patch'} ({len(patched())} patched files)")
