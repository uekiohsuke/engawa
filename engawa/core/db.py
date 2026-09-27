"""SQLite による会話履歴の永続化。

会話履歴はセッション単位で完全に分離する（仕様4-1）。セッション種別：
- dialogue：対話（単一・継続、同期的）
- main：メッセージのメインスレッド（単一・継続）
- sub：メッセージのサブスレッド（話題ごと。タスク専用セッションもこの一種。次段階で使用）
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_KINDS = ("dialogue", "main", "sub")
DEFAULT_SESSIONS = {"dialogue": "対話", "main": "main"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def ensure_default_sessions(self, character_id: str) -> None:
        """キャラクターごとに対話セッションとメインスレッドを1つずつ確保する。"""
        with self._lock, self._conn:
            for kind, title in DEFAULT_SESSIONS.items():
                row = self._conn.execute(
                    "SELECT id FROM sessions WHERE character_id = ? AND kind = ?",
                    (character_id, kind),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO sessions (character_id, kind, title, created_at) VALUES (?, ?, ?, ?)",
                        (character_id, kind, title, _now()),
                    )

    def list_sessions(self, character_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE character_id = ? ORDER BY id", (character_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def add_message(self, session_id: int, role: str, content: str) -> dict[str, Any]:
        created_at = _now()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, created_at),
            )
        return {
            "id": cur.lastrowid,
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": created_at,
        }

    def recent_messages(self, session_id: int, limit: int) -> list[dict[str, Any]]:
        """直近 limit 件を古い順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
