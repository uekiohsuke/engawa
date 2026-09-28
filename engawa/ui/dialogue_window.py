"""対話ウィンドウ：チャットアプリ本体とは別の、即時性を演出する見せ方（仕様5章）。

枠なし・最前面・ドラッグで移動。キャラクターの最新の発話を吹き出し1つで表示し、読み上げる。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QHideEvent, QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from engawa.ui.client import CoreClient
from engawa.ui.tts import Speaker
from engawa.ui.widgets import COLORS

VOICE_SETTING = "dialogue/voice"

DIALOGUE_STYLE = f"""
#dialoguePanel {{ background: {COLORS['bg_channels']}; border: 1px solid {COLORS['bg_hover']}; border-radius: 14px; }}
#dialogueName {{ color: {COLORS['character']}; font-weight: bold; }}
#bubble {{ background: {COLORS['bg_input']}; border-radius: 12px; padding: 12px 14px; font-size: 11.5pt; }}
#closeButton {{ background: transparent; color: {COLORS['text_muted']}; padding: 0 6px; font-size: 12pt; }}
#closeButton:hover {{ color: {COLORS['text']}; }}
"""

THINKING = "……"


class DialogueWindow(QWidget):
    def __init__(self, client: CoreClient, character: dict, session_id: int, speaker: Speaker | None = None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self._client = client
        self._session_id = session_id
        self._speaker = speaker
        self._drag_offset: QPoint | None = None
        self._busy = False

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(f"{character['name']}と対話")
        self.setStyleSheet(DIALOGUE_STYLE)
        self.resize(380, 220)

        panel = QFrame()
        panel.setObjectName("dialoguePanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)

        name = QLabel(character["name"])
        name.setObjectName("dialogueName")
        close_button = QPushButton("×")
        close_button.setObjectName("closeButton")
        close_button.setToolTip("対話を閉じる")
        close_button.clicked.connect(self.close)
        header = QHBoxLayout()
        header.addWidget(name)
        header.addStretch(1)
        if speaker is not None:
            self._voice_button = QPushButton()
            self._voice_button.setObjectName("closeButton")
            self._voice_button.setCheckable(True)
            self._voice_button.toggled.connect(self._set_voice)
            self._voice_button.setChecked(QSettings().value(VOICE_SETTING, True, type=bool))
            self._set_voice(self._voice_button.isChecked())
            header.addWidget(self._voice_button)
        header.addWidget(close_button)

        self._bubble = QLabel("")
        self._bubble.setObjectName("bubble")
        self._bubble.setWordWrap(True)
        self._bubble.setTextFormat(Qt.TextFormat.PlainText)
        self._bubble.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._bubble.setMinimumHeight(80)

        self._input = QLineEdit()
        self._input.setPlaceholderText("話しかける")
        self._input.returnPressed.connect(self._send)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self._bubble, 1)
        layout.addWidget(self._input)

        client.event_received.connect(self._on_event)
        client.get(f"/sessions/{session_id}/messages?limit=20", self._load_latest)

    def _load_latest(self, messages: list[dict]) -> None:
        latest = next((m for m in reversed(messages) if m["role"] == "character"), None)
        if latest and not self._bubble.text():
            self._bubble.setText(latest["content"])

    def _send(self) -> None:
        content = self._input.text().strip()
        if not content or self._busy:
            return
        self._input.clear()
        self._client.post(
            f"/sessions/{self._session_id}/messages",
            {"content": content},
            on_error=lambda status, detail: self._bubble.setText(f"（送信できませんでした：{detail}）"),
        )

    def _set_voice(self, enabled: bool) -> None:
        self._voice_button.setText("🔊" if enabled else "🔇")
        self._voice_button.setToolTip("読み上げ：ON（クリックでOFF）" if enabled else "読み上げ：OFF（クリックでON）")
        QSettings().setValue(VOICE_SETTING, enabled)
        self._speaker.enabled = enabled
        if not enabled:
            self._speaker.stop()

    def _voice(self) -> Speaker | None:
        """読み上げるのは、見えている対話ウィンドウの発言だけ。"""
        return self._speaker if self._speaker is not None and self.isVisible() else None

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        session_id = event.get("session_id") or event.get("message", {}).get("session_id")
        if session_id != self._session_id:
            return
        voice = self._voice()
        if kind == "message.created":
            message = event["message"]
            self._bubble.setText(message["content"] if message["role"] == "system" else THINKING)
            if voice and message["role"] == "user":
                voice.stop()  # 話しかけたら、読み上げ途中でも止める
        elif kind == "generation.started":
            self._busy = True
            self._bubble.setText(THINKING)
            if voice:
                voice.begin_stream()
        elif kind == "message.delta":
            text = self._bubble.text()
            self._bubble.setText(("" if text == THINKING else text) + event["delta"])
            if voice:
                voice.feed(event["delta"])
        elif kind == "message.completed":
            self._busy = False
            self._bubble.setText(event["message"]["content"])
            if voice:
                voice.end_stream()
        elif kind == "error":
            self._busy = False
            self._bubble.setText(f"（応答に失敗しました：{event.get('detail')}）")
            if voice:
                voice.stop()

    def hideEvent(self, event: QHideEvent) -> None:
        if self._speaker is not None:
            self._speaker.stop()
        super().hideEvent(event)

    # --- ドラッグ移動 ---

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
