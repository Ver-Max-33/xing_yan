import threading

from wb.config import get_paths
from wb.db import connect


def test_concurrent_first_open_migrates_once(tmp_path):
    # Many request threads open a brand-new database at the same moment (the API's first requests).
    paths = get_paths("test", tmp_path)
    errors = []

    def open_it():
        try:
            connect(paths.shared_db, "shared").close()
            connect(paths.app_db, "app").close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_it) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    conn = connect(paths.shared_db, "shared")
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 1


def test_concurrent_first_open_and_write(tmp_path):
    # The API's first burst: many threads open a fresh shared.sqlite and write immediately.
    paths = get_paths("test", tmp_path)
    errors = []
    barrier = threading.Barrier(16)

    def work(i):
        try:
            barrier.wait()
            conn = connect(paths.shared_db, "shared")
            conn.execute("INSERT OR REPLACE INTO setting VALUES (?, ?)", (f"k{i}", '"v"'))
            conn.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert connect(paths.shared_db, "shared").execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 16
