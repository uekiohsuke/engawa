"""対話ウィンドウ：チャットアプリ本体とは別の、即時性を演出する見せ方（仕様5章）。

枠なし・最前面・背景は透過で、立ち絵がデスクトップの上に直接立つ。その横に最新の発話を吹き出し1つで表示し、読み上げる。
ドラッグで移動し（位置は次回も使う）、Ctrl+ホイールで大きさを変えられる。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QHideEvent, QMouseEvent, QPainter, QPaintEvent, QPolygonF, QWheelEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from engawa.characters import DEFAULT_EXPRESSION
from engawa.ui.client import CoreClient
from engawa.ui.images import standing_path, standing_pixmap
from engawa.ui.tts import Speaker
from engawa.ui.widgets import COLORS

VOICE_SETTING = "dialogue/voice"
HEIGHT_SETTING = "dialogue/standing_height"
POSITION_SETTING = "dialogue/position"  # 右下の角。大きさを変えても足元の位置が動かないようにする

PANEL_WIDTH = 320
STANDING_HEIGHT = 560
STANDING_HEIGHT_RANGE = (280, 1200)
STANDING_HEIGHT_STEP = 40
PANEL_TOP_RATIO = 0.08  # 吹き出しを顔の高さあたりに置く
TAIL_SIZE = (12, 20)
TAIL_TOP = 36  # パネル上端から三角までの距離（名前の行の少し下）
SCREEN_MARGIN = 16
EXPRESSION_HOLD_MS = 30_000  # 言い終えて（読み上げも終えて）から、ふだんの表情に戻すまで

DIALOGUE_STYLE = f"""
#dialoguePanel {{ background: {COLORS['bg_channels']}; border: 1px solid {COLORS['bg_hover']}; border-radius: 14px; }}
#dialogueName {{ color: {COLORS['character']}; font-weight: bold; }}
#bubble {{ background: {COLORS['bg_input']}; border-radius: 12px; padding: 12px 14px; font-size: 11.5pt; }}
#closeButton {{ background: transparent; color: {COLORS['text_muted']}; padding: 0 6px; font-size: 12pt; }}
#closeButton:hover {{ color: {COLORS['text']}; }}
"""

THINKING = "……"


class BubbleTail(QWidget):
    """吹き出しから立ち絵へ向かう小さな三角。"""

    def __init__(self):
        super().__init__()
        width, height = TAIL_SIZE
        self.setFixedSize(width, TAIL_TOP + height)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["bg_channels"]))
        width, height = TAIL_SIZE
        painter.drawPolygon(
            QPolygonF([QPointF(0, TAIL_TOP), QPointF(width, TAIL_TOP + height / 2), QPointF(0, TAIL_TOP + height)])
        )


class DialogueWindow(QWidget):
    def __init__(self, client: CoreClient, character: dict, session_id: int, speaker: Speaker | None = None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self._client = client
        self._character = character
        self._session_id = session_id
        self._speaker = speaker
        self._drag_offset: QPoint | None = None
        self._busy = False
        self._expression = DEFAULT_EXPRESSION
        # 言い終えてしばらくしたら、ふだんの表情に戻す
        self._revert_timer = QTimer(self)
        self._revert_timer.setSingleShot(True)
        self._revert_timer.setInterval(EXPRESSION_HOLD_MS)
        self._revert_timer.timeout.connect(self._revert_expression)
        if speaker is not None:
            speaker.speaking_changed.connect(self._on_speaking_changed)
        self._placed = False
        self._standing_height = min(
            max(QSettings().value(HEIGHT_SETTING, STANDING_HEIGHT, type=int), STANDING_HEIGHT_RANGE[0]),
            STANDING_HEIGHT_RANGE[1],
        )

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(f"{character['name']}と対話")
        self.setStyleSheet(DIALOGUE_STYLE)

        panel = QFrame()
        panel.setObjectName("dialoguePanel")
        panel.setFixedWidth(PANEL_WIDTH)
        self._standing = QLabel()
        self._standing.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
        self._panel_column = QVBoxLayout()
        self._panel_column.setContentsMargins(0, 0, 0, 0)
        self._panel_column.addSpacing(0)  # 吹き出しの高さ。立ち絵の大きさに合わせて変える
        panel_row = QHBoxLayout()
        panel_row.setContentsMargins(0, 0, 0, 0)
        panel_row.setSpacing(0)
        panel_row.addWidget(panel, 0, Qt.AlignmentFlag.AlignTop)
        self._tail = BubbleTail()
        panel_row.addWidget(self._tail, 0, Qt.AlignmentFlag.AlignTop)
        self._panel_column.addLayout(panel_row)
        self._panel_column.addStretch(1)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        outer.addLayout(self._panel_column)
        outer.addWidget(self._standing, 0, Qt.AlignmentFlag.AlignBottom)

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
        self._panel = panel
        self._apply_standing()

        client.event_received.connect(self._on_event)
        client.get(f"/sessions/{session_id}/messages?limit=20", self._load_latest)

    # --- 立ち絵 ---

    def set_expression(self, expression: str) -> None:
        """表情を変える。その表情の立ち絵がなければ既定の表情になる。"""
        if expression != self._expression:
            self._expression = expression
            self._apply_standing()

    def _on_speaking_changed(self, playing: bool) -> None:
        # 戻すのを待っている間に1文読み終えたら、そこから数え直す
        if not playing and self._revert_timer.isActive():
            self._revert_timer.start()

    def _revert_expression(self) -> None:
        if self._busy or (self._speaker is not None and self._speaker.is_busy()):
            self._revert_timer.start()  # まだ話している途中
            return
        self.set_expression(DEFAULT_EXPRESSION)

    def _apply_standing(self) -> None:
        """立ち絵と、それに合わせたウィンドウの大きさ・吹き出しの高さを決める。足元（右下）の位置は保つ。"""
        bottom_right = self.frameGeometry().bottomRight() if self._placed else None
        pixmap = standing_pixmap(standing_path(self._character, self._expression), self._standing_height)
        spacer = self._panel_column.itemAt(0).spacerItem()
        if pixmap is None:
            # 立ち絵がないキャラクターは、吹き出しのパネルだけを出す
            self._standing.hide()
            self._tail.hide()
            spacer.changeSize(0, 0)
            self._panel.setMinimumHeight(220)
            self.setFixedSize(PANEL_WIDTH, 220)
        else:
            self._standing.setPixmap(pixmap)
            self._standing.show()
            self._tail.show()
            self._panel.setMinimumHeight(0)
            spacer.changeSize(0, round(self._standing_height * PANEL_TOP_RATIO))
            self._panel_column.invalidate()
            width = PANEL_WIDTH + TAIL_SIZE[0] + self.layout().spacing() + round(pixmap.width() / pixmap.devicePixelRatio())
            self.setFixedSize(width, self._standing_height)
        if bottom_right is not None:
            self.move(bottom_right - QPoint(self.width() - 1, self.height() - 1))

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier or self._standing.isHidden():
            super().wheelEvent(event)
            return
        step = STANDING_HEIGHT_STEP if event.angleDelta().y() > 0 else -STANDING_HEIGHT_STEP
        height = min(max(self._standing_height + step, STANDING_HEIGHT_RANGE[0]), STANDING_HEIGHT_RANGE[1])
        if height != self._standing_height:
            self._standing_height = height
            QSettings().setValue(HEIGHT_SETTING, height)
            self._apply_standing()
            self._save_position()

    # --- 位置 ---

    def showEvent(self, event) -> None:
        if not self._placed:
            self._place()
            self._placed = True
        super().showEvent(event)

    def _place(self) -> None:
        """前回の位置（右下の角）に置く。その位置が画面外なら、画面の右下に立たせる。"""
        saved = QSettings().value(POSITION_SETTING)
        bottom_right = saved if isinstance(saved, QPoint) else None
        if bottom_right is None or QGuiApplication.screenAt(bottom_right - QPoint(self.width() // 2, self.height() // 2)) is None:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            bottom_right = screen.bottomRight() - QPoint(SCREEN_MARGIN, 0)
        self.move(bottom_right - QPoint(self.width() - 1, self.height() - 1))

    def _save_position(self) -> None:
        QSettings().setValue(POSITION_SETTING, self.frameGeometry().bottomRight())

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
        if kind == "expression.changed":
            self._revert_timer.stop()
            self.set_expression(event["expression"])
        elif kind == "message.created":
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
            if self._expression != DEFAULT_EXPRESSION:
                self._revert_timer.start()
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
        if self._drag_offset is not None:
            self._save_position()
        self._drag_offset = None
