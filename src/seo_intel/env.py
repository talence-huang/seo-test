"""讀取專案根目錄的 .env（KEY=VALUE 格式），只補上尚未設定的環境變數。

不額外依賴 python-dotenv；支援註解、空行、`export` 前綴、引號與 BOM。
空值視為「尚未填寫」，不會寫入環境變數。
"""

from __future__ import annotations

import os
from pathlib import Path

from seo_intel.config import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / ".env"


def load_env_file(path: Path | None = None) -> list[str]:
    """回傳本次載入的變數名稱（不含值）。"""
    p = path or ENV_FILE
    if not p.is_file():
        return []
    loaded: list[str] = []
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def has_anthropic_credentials() -> bool:
    return any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))
