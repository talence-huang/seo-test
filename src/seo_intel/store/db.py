"""SQLite 存取層。研究用途，單一寫入者；開啟 WAL 與外鍵約束。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
MIGRATIONS = {
    2: "DROP INDEX IF EXISTS idx_lessons_status; DROP TABLE IF EXISTS lessons; DROP TABLE IF EXISTS template_stats;",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _to_column(value: Any) -> Any:
    """dict / list 以排序鍵的 JSON 存放，讓相同內容永遠得到相同字串。"""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return int(value)
    return value


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"資料庫版本 {current} 比程式支援的 {SCHEMA_VERSION} 新")
        if current < SCHEMA_VERSION:
            if current == 0:   # 新資料庫：直接建立最新 schema
                script = resources.files("seo_intel.store").joinpath("schema.sql").read_text(encoding="utf-8")
            else:              # 既有資料庫：依序套用遷移
                script = "\n".join(MIGRATIONS[v] for v in range(current + 1, SCHEMA_VERSION + 1))
            with self.transaction():
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def insert(self, table: str, row: Mapping[str, Any]) -> int:
        cols = list(row)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        with self.transaction():
            cur = self.conn.execute(sql, [_to_column(row[c]) for c in cols])
        return cur.lastrowid

    def query(self, sql: str, params: tuple | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def tables(self) -> list[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        return sorted(r["name"] for r in rows)

    def close(self) -> None:
        self.conn.close()
