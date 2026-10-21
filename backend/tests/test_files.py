"""Files written beside a reader (a scan, a page, the command line at the same moment): each writer has its own
temporary file, so two of them never move each other's half-written file into place."""
import threading

from wb.files import tmp_for


def test_each_writer_has_its_own_temporary_file(tmp_path):
    path = tmp_path / "x.parquet"
    mine, theirs = tmp_for(path), []
    t = threading.Thread(target=lambda: theirs.append(tmp_for(path)))
    t.start()
    t.join()
    assert mine != theirs[0] and mine.parent == path.parent and mine.name.startswith("x.parquet.") and mine.suffix == ".tmp"


def test_a_file_put_in_place_waits_out_a_moment_windows_holds_it(tmp_path, monkeypatch):
    # WinError 5 / 32: another writer putting the same file in place, a reader, the virus scanner (2026-10-10)
    import os

    import pytest

    import wb.files as files
    real, refused = os.replace, []

    def held_twice(a, b):
        if len(refused) < 2:
            refused.append(b)
            raise PermissionError(5, "Access is denied")
        real(a, b)

    tmp, dst = tmp_path / "a.txt.tmp", tmp_path / "a.txt"
    tmp.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    monkeypatch.setattr(files.os, "replace", held_twice)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    monkeypatch.setattr(files, "WINDOWS", True)
    files.move_into_place(tmp, dst)
    assert dst.read_text(encoding="utf-8") == "new" and len(refused) == 2
    monkeypatch.setattr(files, "WINDOWS", False)                 # elsewhere a refusal is a real one: said at once
    refused.clear()
    tmp.write_text("newer", encoding="utf-8")
    with pytest.raises(PermissionError):
        files.move_into_place(tmp, dst)
