from datetime import date

import pytest

from wb.backup import backup_shared
from wb.config import get_paths
from wb.db import connect, tx


def test_paths_layout(tmp_path):
    p = get_paths("prod", tmp_path)
    assert p.market_dir("JP") == tmp_path / "profiles/prod/market/JP"
    assert p.app_db == tmp_path / "profiles/prod/app.sqlite"
    assert p.shared_db == tmp_path / "shared.sqlite"


def test_unknown_profile_rejected(tmp_path):
    with pytest.raises(ValueError):
        get_paths("staging", tmp_path)


def test_migrations_apply_once(paths):
    conn = connect(paths.app_db, "app")
    conn.close()
    conn = connect(paths.app_db, "app")  # second open must not re-run 001
    names = sorted(r[0] for r in conn.execute("SELECT name FROM schema_migrations"))
    from wb.db import MIGRATIONS

    assert names == sorted(f.name for f in (MIGRATIONS / "app").glob("*.sql"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_tx_rolls_back(paths):
    conn = connect(paths.shared_db, "shared")
    with pytest.raises(RuntimeError):
        with tx(conn):
            conn.execute("INSERT INTO setting VALUES ('a', '1')")
            raise RuntimeError
    assert conn.execute("SELECT COUNT(*) FROM setting").fetchone()[0] == 0


def test_backup_daily_and_rotation(paths):
    conn = connect(paths.shared_db, "shared")
    conn.execute("INSERT INTO setting VALUES ('k', 'v')")
    conn.close()
    first = backup_shared(paths, keep=2, today=date(2026, 10, 1))
    assert first is not None and first.exists()
    assert backup_shared(paths, keep=2, today=date(2026, 10, 1)) is None  # once per day
    backup_shared(paths, keep=2, today=date(2026, 10, 2))
    backup_shared(paths, keep=2, today=date(2026, 10, 3))
    names = sorted(p.name for p in paths.backup_dir.iterdir())
    assert names == ["shared-20261002.sqlite", "shared-20261003.sqlite"]
    import sqlite3

    assert sqlite3.connect(paths.backup_dir / names[-1]).execute(
        "SELECT value FROM setting WHERE key='k'"
    ).fetchone()[0] == "v"


def test_env_file_formats(tmp_path, monkeypatch):
    from wb.secrets import get_secret, read_env_file

    env = tmp_path / ".env"
    env.write_text("# comment\nTushare: abc123\nJ-Quants: key-9\nOTHER=1\n", encoding="utf-8")
    assert read_env_file(env) == {"tushare_token": "abc123", "jquants_api_key": "key-9"}
    env.write_text('export TUSHARE_TOKEN="t2"\nJQUANTS_API_KEY=k2\n', encoding="utf-8")
    assert read_env_file(env) == {"tushare_token": "t2", "jquants_api_key": "k2"}
    monkeypatch.setenv("WB_ENV_FILE", str(env))
    monkeypatch.delenv("WB_SECRET_TUSHARE_TOKEN", raising=False)
    assert get_secret("tushare_token") == "t2"
    monkeypatch.setenv("WB_SECRET_TUSHARE_TOKEN", "from-env")
    assert get_secret("tushare_token") == "from-env"  # environment beats the file


def test_a_key_is_changed_where_it_is_read_from(tmp_path, monkeypatch):
    # the .env file outranks the keychain: a new Feishu webhook saved in 设置 went to the keychain while the old one in
    # .env stayed in use (2026-10-10). A key the file gives is rewritten in its own line, as it was written; one the
    # environment gives cannot be changed from the app; any other goes to the keychain.
    import os
    import stat
    from wb.secrets import get_secret, set_secret
    env = tmp_path / ".env"
    env.write_bytes(b"# keys\r\nTushare: t1\r\nFeishu: https://open.feishu.cn/open-apis/bot/v2/hook/old\r\nexport DEEPSEEK_API_KEY='sk-1'\r\n")
    env.chmod(0o600)
    monkeypatch.setenv("WB_ENV_FILE", str(env))
    assert set_secret("feishu_webhook", "https://open.feishu.cn/open-apis/bot/v2/hook/new") == ".env"
    assert set_secret("deepseek_api_key", "sk-2") == ".env"
    assert env.read_bytes() == (b"# keys\r\nTushare: t1\r\nFeishu: https://open.feishu.cn/open-apis/bot/v2/hook/new\r\n"
                                b"export DEEPSEEK_API_KEY='sk-2'\r\n")
    assert get_secret("feishu_webhook").endswith("/new") and get_secret("deepseek_api_key") == "sk-2"
    if os.name != "nt":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert set_secret("openrouter_api_key", "or-1") == "keychain" and get_secret("openrouter_api_key") == "or-1"
    monkeypatch.setenv("WB_SECRET_TUSHARE_TOKEN", "from-env")
    with pytest.raises(ValueError, match="环境变量"):
        set_secret("tushare_token", "t2")
    assert b"Tushare: t1" in env.read_bytes() and sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_the_env_file_carries_tachibanas_four_settings_too(tmp_path):
    # the .env route the settings hint offers (a "Name: value" line, as the user's file has them)
    from wb.secrets import read_env_file

    env = tmp_path / ".env"
    env.write_text("Tachibana Auth ID: ABC\ntachibana_key_file: /k/e_api_private_key.pem\nTACHIBANA_URL=https://x.e-shiten.jp/e/\n"
                   "tachibana-key-pass: pw\n", encoding="utf-8")
    assert read_env_file(env) == {"tachibana_auth_id": "ABC", "tachibana_key_file": "/k/e_api_private_key.pem",
                                  "tachibana_url": "https://x.e-shiten.jp/e/", "tachibana_key_pass": "pw"}


def test_a_unit_test_never_sees_this_machines_keys():
    # tests/conftest.py: no .env, no keychain, no WB_SECRET_* — a test can never reach a real data source, model or
    # bot. Only where a key would come from is compared, so a failure never prints one.
    from wb.secrets import secret_source
    assert [secret_source(n) for n in ("tushare_token", "jquants_api_key", "deepseek_api_key", "feishu_webhook")] == [None] * 4
