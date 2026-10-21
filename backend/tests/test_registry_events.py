import pytest

from wb import events
from wb.registry import Registry, autoload, counts


def test_register_and_duplicate():
    reg = Registry("X")

    @reg.register("a")
    class A: ...

    assert reg["a"] is A
    with pytest.raises(KeyError, match="duplicate"):
        reg.register("a")(object)


def test_autoload_imports_submodules(tmp_path, monkeypatch):
    pkg = tmp_path / "plugpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "one.py").write_text("LOADED = True\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    autoload("plugpkg")
    import sys

    assert sys.modules["plugpkg.one"].LOADED


def test_counts_lists_providers():
    assert "PROVIDERS" in counts()


def test_publish_isolates_failing_consumer():
    events.clear()
    seen = []

    @events.on("x")
    def bad(_):
        raise RuntimeError("boom")

    @events.on("x")
    def good(p):
        seen.append(p["n"])

    errors = events.publish("x", {"n": 1})
    assert seen == [1]
    assert len(errors) == 1 and str(errors[0]) == "boom"
    events.clear()
