"""Daily copy of shared.sqlite (spec §4.3): labels, drawings and notes cannot be re-downloaded."""
import sqlite3
from datetime import date
from pathlib import Path

from wb.config import Paths
from wb.db import connect


def backup_shared(paths: Paths, keep: int = 30, today: date | None = None) -> Path | None:
    """Copy shared.sqlite once per day using SQLite's online backup; keep the newest `keep` copies.

    Returns the new backup path, or None when today's copy already exists.
    """
    today = today or date.today()
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    dest = paths.backup_dir / f"shared-{today:%Y%m%d}.sqlite"
    if dest.exists():
        return None
    src = connect(paths.shared_db, "shared")
    try:
        # a damaged file is never copied: day by day its copies would push every good one out of the 30 kept
        if (check := src.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise sqlite3.DatabaseError(f"shared.sqlite 已损坏（{check}）：今天不备份，之前的备份都留着")
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    for old in sorted(paths.backup_dir.glob("shared-*.sqlite"))[:-keep]:
        old.unlink()
    return dest
