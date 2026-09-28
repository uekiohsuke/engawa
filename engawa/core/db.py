"""SQLite による会話履歴の永続化。

会話履歴はセッション単位で完全に分離する（仕様4-1）。セッション種別：
- dialogue：対話（単一・継続、同期的）
- main：メッセージのメインスレッド（単一・継続）
- sub：メッセージのサブスレッド（話題ごと。タスク専用セッションもこの一種。次段階で使用）
"""

from __future__ import annotations

import json
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
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS character_state (
    character_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_events_character ON state_events(character_id, id);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- conversation（会話由来）/ self（自己言及・生活由来）
    speaker TEXT,                    -- conversation のとき user / character
    content TEXT NOT NULL,
    message_id INTEGER,
    created_at TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0,  -- 日をまたいで累積する参照回数
    distilled INTEGER NOT NULL DEFAULT 0,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_stm_character ON stm(character_id, id);
CREATE TABLE IF NOT EXISTS seeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS ltm (
    character_id TEXT PRIMARY KEY,
    conversation TEXT NOT NULL,
    self TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ltm_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id TEXT NOT NULL,
    conversation TEXT NOT NULL,
    self TEXT NOT NULL,
    created_at TEXT NOT NULL
);
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
            self._migrate()

    def _migrate(self) -> None:
        """既存のDBに、後から追加した列を足す。"""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "archived" not in columns:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

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

    _SESSION_SELECT = (
        "SELECT s.*, (SELECT MAX(m.created_at) FROM messages m WHERE m.session_id = s.id) AS last_message_at "
        "FROM sessions s"
    )

    def list_sessions(self, character_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"{self._SESSION_SELECT} WHERE s.character_id = ? ORDER BY s.id", (character_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(f"{self._SESSION_SELECT} WHERE s.id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def create_session(self, character_id: str, kind: str, title: str, created_at: str) -> dict[str, Any]:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO sessions (character_id, kind, title, created_at) VALUES (?, ?, ?, ?)",
                (character_id, kind, title, created_at),
            )
        return self.get_session(cur.lastrowid)

    def update_session(self, session_id: int, *, title: str | None = None, archived: bool | None = None) -> None:
        with self._lock, self._conn:
            if title is not None:
                self._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
            if archived is not None:
                self._conn.execute("UPDATE sessions SET archived = ? WHERE id = ?", (int(archived), session_id))

    def add_message(self, session_id: int, role: str, content: str, created_at: str | None = None) -> dict[str, Any]:
        created_at = created_at or _now()
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

    def load_state(self, character_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM character_state WHERE character_id = ?", (character_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def save_state(self, character_id: str, data: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO character_state (character_id, data) VALUES (?, ?) "
                "ON CONFLICT(character_id) DO UPDATE SET data = excluded.data",
                (character_id, json.dumps(data, ensure_ascii=False)),
            )

    def add_state_event(self, character_id: str, kind: str, detail: str, created_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO state_events (character_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (character_id, kind, detail, created_at),
            )

    def recent_state_events(self, character_id: str, limit: int) -> list[dict[str, Any]]:
        """直近 limit 件を新しい順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_events WHERE character_id = ? ORDER BY id DESC LIMIT ?",
                (character_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def last_message(self, character_id: str, roles: tuple[str, ...] = ("user", "character")) -> dict[str, Any] | None:
        """キャラクターの全セッションを通じた最新のメッセージ。"""
        placeholders = ",".join("?" * len(roles))
        with self._lock:
            row = self._conn.execute(
                f"SELECT m.* FROM messages m JOIN sessions s ON m.session_id = s.id "
                f"WHERE s.character_id = ? AND m.role IN ({placeholders}) ORDER BY m.id DESC LIMIT 1",
                (character_id, *roles),
            ).fetchone()
        return dict(row) if row else None

    def has_user_message_after(self, session_id: int, message_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND id > ? AND role = 'user' LIMIT 1",
                (session_id, message_id),
            ).fetchone()
        return row is not None

    # --- STM ---

    def add_stm(
        self,
        character_id: str,
        kind: str,
        content: str,
        created_at: str,
        speaker: str | None = None,
        message_id: int | None = None,
        embedding: bytes | None = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO stm (character_id, kind, speaker, content, message_id, created_at, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (character_id, kind, speaker, content, message_id, created_at, embedding),
            )
        return cur.lastrowid

    def set_stm_embedding(self, stm_id: int, embedding: bytes) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE stm SET embedding = ? WHERE id = ?", (embedding, stm_id))

    def list_stm(
        self, character_id: str, since: str | None = None, only_undistilled: bool = False, after_id: int = 0
    ) -> list[dict[str, Any]]:
        """古い順。embedding を含む。"""
        sql = "SELECT * FROM stm WHERE character_id = ? AND id > ?"
        params: list[Any] = [character_id, after_id]
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        if only_undistilled:
            sql += " AND distilled = 0"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

    def increment_stm_refs(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._conn:
            self._conn.executemany("UPDATE stm SET ref_count = ref_count + 1 WHERE id = ?", [(i,) for i in ids])

    def mark_stm_distilled(self, ids: list[int]) -> None:
        with self._lock, self._conn:
            self._conn.executemany("UPDATE stm SET distilled = 1 WHERE id = ?", [(i,) for i in ids])

    def delete_stm(self, ids: list[int]) -> None:
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM stm WHERE id = ?", [(i,) for i in ids])

    def delete_stm_before(self, character_id: str, before: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM stm WHERE character_id = ? AND created_at < ?", (character_id, before)
            )
        return cur.rowcount

    # --- 会話の種 ---

    def add_seed(self, character_id: str, content: str, created_at: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO seeds (character_id, content, created_at) VALUES (?, ?, ?)",
                (character_id, content, created_at),
            )
        return cur.lastrowid

    def list_seeds(self, character_id: str, since: str | None = None, unused_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM seeds WHERE character_id = ?"
        params: list[Any] = [character_id]
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        if unused_only:
            sql += " AND used_at IS NULL"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

    def mark_seed_used(self, seed_id: int, used_at: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE seeds SET used_at = ? WHERE id = ?", (used_at, seed_id))

    def delete_seeds(self, character_id: str, ids: list[int]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "DELETE FROM seeds WHERE character_id = ? AND id = ?", [(character_id, i) for i in ids]
            )

    def delete_seeds_before(self, character_id: str, before: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM seeds WHERE character_id = ? AND created_at < ?", (character_id, before)
            )
        return cur.rowcount

    # --- LTM ---

    def get_ltm(self, character_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM ltm WHERE character_id = ?", (character_id,)).fetchone()
        return dict(row) if row else None

    def save_ltm(self, character_id: str, conversation: str, self_text: str, updated_at: str) -> None:
        """LTM を更新し、履歴にも残す（蒸留の結果を後から比較できるように）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO ltm (character_id, conversation, self, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(character_id) DO UPDATE SET conversation = excluded.conversation, "
                "self = excluded.self, updated_at = excluded.updated_at",
                (character_id, conversation, self_text, updated_at),
            )
            self._conn.execute(
                "INSERT INTO ltm_versions (character_id, conversation, self, created_at) VALUES (?, ?, ?, ?)",
                (character_id, conversation, self_text, updated_at),
            )

    def recent_messages(self, session_id: int, limit: int) -> list[dict[str, Any]]:
        """直近 limit 件を古い順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
