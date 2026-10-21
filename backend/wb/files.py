"""Files written beside readers: write a temporary file, then move it into place. Each writer (process and thread: the
daily scan, a page, the command line at the same moment) has its own temporary name, so two never move each other's
half-written file."""
import os
import threading
import time
from pathlib import Path


def tmp_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


WINDOWS = os.name == "nt"


def move_into_place(tmp: Path, path: Path, tries: int = 10) -> None:
    """os.replace, retried on Windows: there it is refused while something holds the file for a moment — a reader, another
    writer putting the same file in place, the virus scanner or the search indexer opening a new file (WinError 5 / 32;
    2026-10-10: two reads of one stock's announcements at once). Up to about 2 s in all."""
    for k in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not WINDOWS or k == tries - 1:
                raise
            time.sleep(0.05 * (k + 1))
