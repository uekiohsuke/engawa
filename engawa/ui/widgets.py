"""共通ウィジェットとスタイル。"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

# Discord 風のダークテーマ
COLORS = {
    "bg_sidebar": "#1e1f22",
    "bg_channels": "#2b2d31",
    "bg_chat": "#313338",
    "bg_input": "#383a40",
    "bg_hover": "#404249",
    "text": "#dbdee1",
    "text_muted": "#949ba4",
    "accent": "#5865f2",
    "character": "#7fd1c7",
    "user": "#f2f3f5",
    "error": "#f23f43",
}

APP_STYLE = f"""
QWidget {{ color: {COLORS['text']}; font-family: "Yu Gothic UI", "Meiryo UI", sans-serif; font-size: 10.5pt; }}
QMainWindow, #chatPane {{ background: {COLORS['bg_chat']}; }}
#characterPane {{ background: {COLORS['bg_sidebar']}; }}
#channelPane {{ background: {COLORS['bg_channels']}; }}
#paneHeader {{ color: {COLORS['text_muted']}; font-size: 9pt; font-weight: bold; padding: 12px 12px 4px 12px; }}
#chatHeader {{ font-weight: bold; padding: 12px 16px; border-bottom: 1px solid {COLORS['bg_sidebar']}; }}
QListWidget {{ background: transparent; border: none; outline: none; padding: 4px; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {COLORS['bg_hover']}; }}
QListWidget::item:selected {{ background: {COLORS['bg_hover']}; color: white; }}
QScrollArea {{ border: none; background: transparent; }}
QSplitter::handle {{ background: {COLORS['bg_sidebar']}; }}
QPlainTextEdit, QLineEdit {{
    background: {COLORS['bg_input']}; border: none; border-radius: 8px; padding: 8px 10px;
    selection-background-color: {COLORS['accent']};
}}
QPushButton {{
    background: {COLORS['accent']}; color: white; border: none; border-radius: 4px; padding: 6px 14px;
}}
QPushButton:disabled {{ background: {COLORS['bg_hover']}; color: {COLORS['text_muted']}; }}
#focusButton {{ background: {COLORS['bg_hover']}; color: {COLORS['text']}; }}
#focusButton:checked {{ background: #f0b232; color: #1e1f22; font-weight: bold; }}
QStatusBar {{ background: {COLORS['bg_sidebar']}; color: {COLORS['text_muted']}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {COLORS['bg_sidebar']}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


def format_time(iso: str | None) -> str:
    try:
        moment = datetime.fromisoformat(iso).astimezone() if iso else datetime.now()
    except ValueError:
        return ""
    return moment.strftime("%m/%d %H:%M")


class MessageRow(QFrame):
    """1件分のメッセージ表示（名前・時刻・本文）。"""

    def __init__(self, author: str, color: str, content: str, created_at: str | None = None):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(2)
        header = QLabel(
            f'<span style="color:{color}; font-weight:bold;">{author}</span>'
            f'&nbsp;&nbsp;<span style="color:{COLORS["text_muted"]}; font-size:8pt;">{format_time(created_at)}</span>'
        )
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_text(content)
        layout.addWidget(header)
        layout.addWidget(self.body)

    def set_text(self, text: str) -> None:
        self.body.setText(text)

    def append_text(self, delta: str) -> None:
        self.body.setText(self.body.text() + delta)


class MessageView(QScrollArea):
    """メッセージの縦並び表示。生成中のキャラクター発言はストリーム表示する。"""

    def __init__(self, character_name: str):
        super().__init__()
        self._character_name = character_name
        self._streaming: MessageRow | None = None
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("chatPane")
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(0, 8, 0, 8)
        self._layout.setSpacing(0)
        self._layout.addStretch(1)
        self.setWidget(container)

    def set_character_name(self, name: str) -> None:
        self._character_name = name

    def clear(self) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        self._streaming = None

    def add_message(self, message: dict) -> None:
        if message["role"] == "character":
            row = MessageRow(self._character_name, COLORS["character"], message["content"], message["created_at"])
        elif message["role"] == "user":
            row = MessageRow("あなた", COLORS["user"], message["content"], message["created_at"])
        else:
            row = MessageRow("system", COLORS["text_muted"], message["content"], message["created_at"])
        self._append_row(row)

    def add_notice(self, text: str) -> None:
        self._append_row(MessageRow("system", COLORS["error"], text))

    def start_stream(self) -> None:
        self._streaming = MessageRow(self._character_name, COLORS["character"], "")
        self._append_row(self._streaming)

    def append_stream(self, delta: str) -> None:
        if self._streaming is None:
            self.start_stream()
        self._streaming.append_text(delta)
        self._scroll_to_bottom()

    def finish_stream(self, message: dict) -> None:
        if self._streaming is None:
            self.add_message(message)
            return
        self._streaming.set_text(message["content"])
        self._streaming = None
        self._scroll_to_bottom()

    def abort_stream(self) -> None:
        if self._streaming is not None:
            self._streaming.deleteLater()
            self._streaming = None

    def _append_row(self, row: QWidget) -> None:
        self._layout.addWidget(row)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        # レイアウト反映後にスクロールさせる
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))
