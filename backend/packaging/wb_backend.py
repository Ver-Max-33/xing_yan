"""The packaged backend (spec §3.3): the same `wb` command line, frozen by PyInstaller into one directory that the
desktop app carries as extraResources/backend/."""
import multiprocessing
import os
import sys
from pathlib import Path


def _numba_cache() -> Path:
    """numba's compiled kernels go to the user's cache, never into the app's own files (they may be read-only, and
    writing into a macOS bundle breaks its signature)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "xingyan-workbench" / "numba"


if __name__ == "__main__":
    if where := os.environ.get("WB_STACKS_DIR"):   # a hang or a crash looked into (packaging/scan_check.py)
        import faulthandler
        out = open(Path(where) / f"stacks-{os.getpid()}.txt", "w", encoding="utf-8")
        faulthandler.enable(out, all_threads=True)   # a crash writes where each thread was
        if "--multiprocessing-fork" in sys.argv:     # a pool worker: what native code says on its way down too
            if sys.platform == "win32":
                import ctypes
                import msvcrt
                ctypes.windll.kernel32.SetStdHandle(-12, msvcrt.get_osfhandle(out.fileno()))   # STD_ERROR_HANDLE
            os.dup2(out.fileno(), 2)
        if every := os.environ.get("WB_STACKS_EVERY"):   # every N s, the threads' stacks: a watchdog reading other
            faulthandler.dump_traceback_later(int(every), repeat=True, file=out)   # threads, so only for a hang
    multiprocessing.freeze_support()  # the structure computation's process pool starts this executable again
    if getattr(sys, "frozen", False):
        os.environ.setdefault("NUMBA_CACHE_DIR", str(_numba_cache()))  # before numba is imported
    from wb.cli import main

    main()
