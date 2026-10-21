"""SQLite access: WAL mode, numbered SQL migrations, explicit transactions (spec §3.2)."""
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).with_name("migrations")


_MIGRATE_LOCK = threading.Lock()
_READY: set[tuple[str, str]] = set()


def connect(path: Path, kind: str) -> sqlite3.Connection:
    """Open (creating if needed) an `app` or `shared` database and apply pending migrations.

    The first open of a file in this process switches it to WAL and migrates it while holding a
    lock, so the API's first burst of parallel requests cannot trip over a half-initialised file
    ("database is locked" from the WAL switch, "table already exists" from double migration)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; use tx() for transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    key = (str(path.resolve()), kind)
    if key not in _READY:
        with _MIGRATE_LOCK:
            if key not in _READY:
                conn.execute("PRAGMA journal_mode=WAL")  # persistent: stored in the file itself
                _migrate(conn, kind)
                _READY.add(key)
    return conn


def _statements(sql: str) -> list[str]:
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    return out


def _migrate(conn: sqlite3.Connection, kind: str) -> None:
    """Apply pending migrations, each in one transaction. Callers hold _MIGRATE_LOCK (threads);
    BEGIN IMMEDIATE plus the re-check inside the transaction also covers a second process (the
    CLI) opening the same fresh database."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for f in sorted((MIGRATIONS / kind).glob("*.sql")):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (f.name,)).fetchone():
                for stmt in _statements(f.read_text(encoding="utf-8")):
                    conn.execute(stmt)
                conn.execute("INSERT INTO schema_migrations VALUES (?, datetime('now'))", (f.name,))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
