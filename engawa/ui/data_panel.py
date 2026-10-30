"""データの管理パネル（状態確認ビューの「データ」タブ）。

プロンプトやモデルを試すために、会話・記憶・内部状態をまとめてリセットする。リセットや復元の前の状態は
自動でバックアップされ、いつでもその状態に戻せる。バックアップの中身は戻す前に読み取り専用で見られる。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engawa.ui.client import CoreClient
from engawa.ui.memory_panel import SUBJECT_LABELS
from engawa.ui.widgets import COLORS, format_time


def _size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def format_preview(preview: dict, names: dict[str, str]) -> str:
    lines = [f"{format_time(preview['created_at'])}　{preview['reason_label']}　{_size(preview['size'])}", ""]
    if not preview["characters"]:
        lines.append("（会話も記憶もありません）")
    for character_id, data in preview["characters"].items():
        period = f"　{format_time(data['first_at'])} 〜 {format_time(data['last_at'])}" if data["messages"] else ""
        lines += [
            f"■ {names.get(character_id, character_id)}",
            f"会話 {data['messages']}件（チャンネル {data['sessions']}）{period}",
            f"エピソード {data['episode_count']}件　印象メモ {data['memo_count']}件　未使用の会話の種 {len(data['seeds'])}件",
            "",
        ]
        for subject, label in SUBJECT_LABELS.items():
            lines += [f"【{label}】", data["profiles"].get(subject) or "（まだ無い）", ""]
        if data["episodes"]:
            shown = len(data["episodes"])
            more = f"（新しい順に{shown}件）" if shown < data["episode_count"] else "（新しい順）"
            lines.append(f"【エピソード】{more}")
            lines += [f"- {format_time(e['happened_at'])}  {e['content']}" for e in data["episodes"]]
            lines.append("")
        if data["seeds"]:
            lines.append("【会話の種】")
            lines += [f"- {s}" for s in data["seeds"]]
            lines.append("")
    return "\n".join(lines).rstrip()


class DataPanel(QWidget):
    def __init__(self, client: CoreClient, character: dict):
        super().__init__()
        self._client = client
        self._names = {character["id"]: character["name"]}

        note = QLabel(
            "会話・記憶・内部状態をまとめて扱う（全キャラクター共通）。"
            "リセットや復元の前の状態は自動でバックアップされ、いつでも戻せる。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['text_muted']};")
        self._backup_button = QPushButton("今の状態をバックアップ")
        self._backup_button.clicked.connect(self._backup)
        self._reset_button = QPushButton("リセット…")
        self._reset_button.setToolTip("今の状態をバックアップしてから、会話・記憶・内部状態をすべて消す")
        self._reset_button.clicked.connect(self._reset)
        header = QHBoxLayout()
        header.addWidget(note, 1)
        header.addWidget(self._backup_button)
        header.addWidget(self._reset_button)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selected)
        self._restore_button = QPushButton("この状態に戻す…")
        self._restore_button.clicked.connect(self._restore)
        self._delete_button = QPushButton("削除…")
        self._delete_button.clicked.connect(self._delete)
        actions = QHBoxLayout()
        actions.addWidget(self._restore_button)
        actions.addWidget(self._delete_button)
        actions.addStretch(1)
        list_pane = QWidget()
        list_layout = QVBoxLayout(list_pane)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addWidget(QLabel("バックアップ（新しい順）"))
        list_layout.addWidget(self._list, 1)
        list_layout.addLayout(actions)

        self._preview = QPlainTextEdit(readOnly=True)
        self._preview.setPlaceholderText("バックアップを選ぶと中身が表示されます")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(list_pane)
        splitter.addWidget(self._preview)
        splitter.setSizes([260, 500])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addLayout(header)
        layout.addWidget(splitter, 1)

        client.event_received.connect(self._on_event)
        self._update_actions()
        self.reload()

    def reload(self, select: str | None = None) -> None:
        current = self._list.currentItem()
        select = select or (current.data(Qt.ItemDataRole.UserRole)["name"] if current else None)
        self._client.get("/backups", lambda backups: self._apply(backups, select))

    def _apply(self, backups: list[dict], select: str | None) -> None:
        self._list.clear()
        for backup in backups:
            item = QListWidgetItem(f"{format_time(backup['created_at'])}　{backup['reason_label']}　{_size(backup['size'])}")
            item.setData(Qt.ItemDataRole.UserRole, backup)
            item.setToolTip(backup["name"])
            self._list.addItem(item)
            if backup["name"] == select:
                self._list.setCurrentItem(item)
        if not backups:
            self._preview.clear()
        self._update_actions()

    def _selected(self) -> dict | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_actions(self) -> None:
        selected = self._selected() is not None
        self._restore_button.setEnabled(selected)
        self._delete_button.setEnabled(selected)

    def _on_selected(self, item: QListWidgetItem | None) -> None:
        self._update_actions()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)["name"]
        self._preview.setPlainText("読み込み中…")
        self._client.get(f"/backups/{name}", self._show_preview, self._on_error)

    def _show_preview(self, preview: dict) -> None:
        selected = self._selected()
        if selected and selected["name"] == preview["name"]:
            self._preview.setPlainText(format_preview(preview, self._names))

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "data.replaced":
            self.reload()

    def _on_error(self, status: int, detail: str) -> None:
        QMessageBox.warning(self, "データ", f"できませんでした（{status}）：{detail}")

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(self, title, text, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Ok

    def _backup(self) -> None:
        self._client.post("/backups", {}, lambda backup: self.reload(select=backup["name"]), self._on_error)

    def _reset(self) -> None:
        if self._confirm(
            "リセット",
            "会話・記憶・内部状態をすべて消して、最初からにします。\n今の状態はバックアップされ、あとから戻せます。",
        ):
            self._client.post("/data/reset", {}, None, self._on_error)

    def _restore(self) -> None:
        backup = self._selected()
        if backup and self._confirm(
            "この状態に戻す",
            f"{format_time(backup['created_at'])}（{backup['reason_label']}）の状態に戻します。\n"
            "今の状態はバックアップされ、あとから戻せます。",
        ):
            self._client.post(f"/backups/{backup['name']}/restore", {}, None, self._on_error)

    def _delete(self) -> None:
        backup = self._selected()
        if backup and self._confirm(
            "バックアップの削除", f"{format_time(backup['created_at'])}（{backup['reason_label']}）のバックアップを削除します。"
        ):
            self._client.delete(f"/backups/{backup['name']}", lambda _: self.reload(), self._on_error)
