"""API tokens (spec §3.2). Lookup order, first hit wins:
  1. environment variable WB_SECRET_<NAME>               (CI, live tests)
  2. the .env file at the repository root, or WB_ENV_FILE (git-ignored; never committed or logged)
  3. the OS keychain via `keyring`                        (`wb secret set <name>`, 设置; a key the .env file or
                                                          the environment gives is changed there instead: set_secret)
Known names: tushare_token, jquants_api_key, tickflow_api_key, deepseek_api_key, openrouter_api_key, feishu_webhook,
feishu_secret, github_token (检查更新: the app's private repository's releases).

The .env file accepts `KEY=value` or `KEY: value`; keys are matched loosely, so
`TUSHARE_TOKEN=...`, `Tushare: ...`, `JQUANTS_API_KEY=...` and `J-Quants: ...` all work.
"""
import os
import re
import shutil
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from wb.files import move_into_place, tmp_for

SERVICE = "stock-workbench"
ALIASES = {"tushare": "tushare_token", "tusharetoken": "tushare_token", "github": "github_token", "githubtoken": "github_token",
           "jquants": "jquants_api_key", "jquantsapikey": "jquants_api_key", "jquantskey": "jquants_api_key",
           "tickflow": "tickflow_api_key", "tickflowapikey": "tickflow_api_key", "tickflowkey": "tickflow_api_key",
           "deepseek": "deepseek_api_key", "deepseekapikey": "deepseek_api_key", "deepseekkey": "deepseek_api_key",
           "openrouter": "openrouter_api_key", "openrouterapikey": "openrouter_api_key", "openrouterkey": "openrouter_api_key",
           "feishu": "feishu_webhook", "feishuwebhook": "feishu_webhook", "feishusecret": "feishu_secret", "feishusign": "feishu_secret",
           "tachibanaauthid": "tachibana_auth_id", "tachibanakeyfile": "tachibana_key_file", "tachibanaurl": "tachibana_url",
           "tachibanakeypass": "tachibana_key_pass"}
_LINE = re.compile(r"^(?:export\s+)?([^=:#]+?)\s*[=:]\s*(.*)$")
_PARTS = re.compile(r"^(\s*(?:export\s+)?([^=:#]+?)\s*[=:]\s*)(.*?)(\s*)$")   # a line: up to its value, its name, the value, the rest


def env_path() -> Path:
    return Path(os.environ.get("WB_ENV_FILE") or Path(__file__).resolve().parents[2] / ".env")


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        key = re.sub(r"[^a-z0-9]", "", m.group(1).lower()).removeprefix("wbsecret")
        value = m.group(2).strip().strip("\"'")
        if key in ALIASES and value:
            out[ALIASES[key]] = value
    return out


def get_secret(name: str) -> str | None:
    found = os.environ.get(f"WB_SECRET_{name.upper()}") or read_env_file(env_path()).get(name)
    if found:
        return found
    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:  # no keychain backend (headless Linux/CI)
        return None


def secret_source(name: str) -> str | None:
    """Where a key would be read from: "env", ".env", "keychain" or None. Never the value."""
    if os.environ.get(f"WB_SECRET_{name.upper()}"):
        return "env"
    if read_env_file(env_path()).get(name):
        return ".env"
    try:
        return "keychain" if keyring.get_password(SERVICE, name) else None
    except KeyringError:
        return None


def set_secret(name: str, value: str) -> str:
    """Store a key where it is read from, so the new one is the one used (the lookup order above): one the environment
    gives cannot be changed from here (ValueError); one the .env file gives is rewritten in its own line, as it was
    written (its name, separator, quotes and line ending kept); any other goes to the keychain. -> ".env" or "keychain"."""
    if os.environ.get(f"WB_SECRET_{name.upper()}"):
        raise ValueError(f"这个密钥来自环境变量 WB_SECRET_{name.upper()}，在应用里改不了：改那个环境变量，或者去掉它再存")
    path = env_path()
    if not read_env_file(path).get(name):
        keyring.set_password(SERVICE, name, value)
        return "keychain"
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.read().splitlines(keepends=True)
    out = []
    for line in lines:
        body = line.rstrip("\r\n")
        m = _PARTS.match(body)
        if m and ALIASES.get(re.sub(r"[^a-z0-9]", "", m.group(2).lower()).removeprefix("wbsecret")) == name:
            old = m.group(3)
            q = old[0] if len(old) >= 2 and old[0] == old[-1] and old[0] in "\"'" else ""
            body = f"{m.group(1)}{q}{value}{q}{m.group(4)}"
        out.append(body + line[len(line.rstrip("\r\n")):])
    tmp = tmp_for(path)
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write("".join(out))
    shutil.copymode(path, tmp)   # the file's own permissions (a keys file is often readable by its owner only)
    move_into_place(tmp, path)
    return ".env"
