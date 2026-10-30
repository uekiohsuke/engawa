"""データ（会話DB）のバックアップ。プロンプトやモデルを試すためにリセットし、いつでも戻せるようにする。

バックアップは DB ファイルの丸ごとのコピーで、`data/backups/` に置く。名前は
`engawa-YYYYmmdd-HHMMSS-<きっかけ>.db`。戻すときは、戻す前の状態もバックアップしておくので、戻したこと自体も取り消せる。
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from engawa.core.db import Database

REASON_LABELS = {"manual": "手動", "reset": "リセット前", "restore": "復元前"}
NAME_PATTERN = re.compile(r"^[\w.-]+\.db$")
PREVIEW_EPISODES = 30


class BackupNotFoundError(Exception):
    pass


class BackupStore:
    def __init__(self, directory: Path):
        self._dir = directory

    def path(self, name: str) -> Path:
        path = self._dir / name
        if not NAME_PATTERN.match(name) or not path.is_file():
            raise BackupNotFoundError(name)
        return path

    def create(self, db: Database, reason: str, now: datetime) -> dict[str, Any]:
        self._dir.mkdir(parents=True, exist_ok=True)
        stem = f"engawa-{now:%Y%m%d-%H%M%S}-{reason}"
        path = self._dir / f"{stem}.db"
        n = 1
        while path.exists():  # 同じ秒に続けて作ったとき
            n += 1
            path = self._dir / f"{stem}-{n}.db"
        db.backup_to(path)
        return self._describe(path)

    def list(self) -> list[dict[str, Any]]:
        if not self._dir.is_dir():
            return []
        paths = [p for p in self._dir.iterdir() if p.is_file() and NAME_PATTERN.match(p.name)]
        return [self._describe(p) for p in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)]

    def delete(self, name: str) -> None:
        self.path(name).unlink()

    def _describe(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        reason = next((r for r in REASON_LABELS if f"-{r}" in path.stem), None)
        return {
            "name": path.name,
            "reason": reason,
            "reason_label": REASON_LABELS.get(reason, "その他"),
            "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            "size": stat.st_size,
        }

    def preview(self, name: str) -> dict[str, Any]:
        """バックアップの中身を読み取り専用で要約する。古い形式で無いテーブルは空として扱う。"""
        path = self.path(name)
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

            def rows(table: str, sql: str, *args: Any) -> list[dict[str, Any]]:
                if table not in tables:
                    return []
                try:
                    return [dict(r) for r in conn.execute(sql, args)]
                except sqlite3.OperationalError:  # 記憶v1の同名テーブルなど、列が違う
                    return []

            characters = {r["character_id"] for r in rows("sessions", "SELECT DISTINCT character_id FROM sessions")}
            result: dict[str, Any] = {**self._describe(path), "characters": {}}
            for cid in sorted(characters):
                [counts] = rows(
                    "messages",
                    "SELECT COUNT(*) AS messages, MIN(m.created_at) AS first_at, MAX(m.created_at) AS last_at "
                    "FROM messages m JOIN sessions s ON s.id = m.session_id WHERE s.character_id = ?",
                    cid,
                ) or [{"messages": 0, "first_at": None, "last_at": None}]
                result["characters"][cid] = {
                    **counts,
                    "sessions": len(rows("sessions", "SELECT id FROM sessions WHERE character_id = ?", cid)),
                    "profiles": {
                        r["subject"]: r["impression"]
                        for r in rows("profiles", "SELECT subject, impression FROM profiles WHERE character_id = ?", cid)
                    },
                    "episodes": rows(
                        "episodes",
                        "SELECT happened_at, content FROM episodes WHERE character_id = ? ORDER BY happened_at DESC LIMIT ?",
                        cid,
                        PREVIEW_EPISODES,
                    ),
                    "episode_count": len(rows("episodes", "SELECT id FROM episodes WHERE character_id = ?", cid)),
                    "memo_count": len(rows("impression_memos", "SELECT id FROM impression_memos WHERE character_id = ?", cid)),
                    "seeds": [
                        r["content"]
                        for r in rows(
                            "seeds", "SELECT content FROM seeds WHERE character_id = ? AND used_at IS NULL ORDER BY id", cid
                        )
                    ],
                }
            return result
