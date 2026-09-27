"""チャットアプリ本体（Discord 風）。

左：キャラクター一覧（フレンド一覧）。ダブルクリックで対話ウィンドウを開く（仕様5章）。
中：チャンネル一覧（メッセージのメイン／サブスレッド。今回は #main のみ）。
右：メッセージ欄と入力欄。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engawa.ui.client import CoreClient
from engawa.ui.dialogue_window import DialogueWindow
from engawa.ui.state_window import StateWindow
from engawa.ui.widgets import MessageView

CHANNEL_KINDS = ("main", "sub")
PRESENCE_MARKS = {"both": "🟢", "message_only": "🟡", "sleeping": "🌙"}


class MessageInput(QPlainTextEdit):
    """Enter で送信、Shift+Enter で改行。"""

    submitted = Signal()

    def __init__(self):
        super().__init__()
        self.setPlaceholderText("メッセージを送信（Shift+Enter で改行）")
        self.setFixedHeight(64)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


def _pane(object_name: str, title: str, widget: QWidget) -> QWidget:
    pane = QWidget()
    pane.setObjectName(object_name)
    pane.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QVBoxLayout(pane)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    header = QLabel(title)
    header.setObjectName("paneHeader")
    layout.addWidget(header)
    layout.addWidget(widget)
    return pane


class MainWindow(QMainWindow):
    def __init__(self, client: CoreClient, characters: list[dict], sessions: dict[str, list[dict]]):
        super().__init__()
        self._client = client
        self._characters = {c["id"]: c for c in characters}
        self._sessions = sessions
        self._current_character: str | None = None
        self._current_session: dict | None = None
        self._busy_sessions: set[int] = set()
        self._dialogue_windows: dict[str, DialogueWindow] = {}
        self._state_windows: dict[str, StateWindow] = {}
        self._character_items: dict[str, QListWidgetItem] = {}

        self.setWindowTitle("縁側")
        self.resize(1000, 680)

        self._character_list = QListWidget()
        self._character_list.setToolTip("ダブルクリックで対話を始める")
        self._character_list.currentItemChanged.connect(self._on_character_selected)
        self._character_list.itemDoubleClicked.connect(lambda item: self.open_dialogue(item.data(Qt.ItemDataRole.UserRole)))

        self._state_button = QPushButton("状態を見る")
        self._state_button.clicked.connect(self._open_current_state)
        self._focus_button = QPushButton()
        self._focus_button.setObjectName("focusButton")
        self._focus_button.setCheckable(True)
        self._focus_button.setToolTip("ONの間は対話で話しかけず、メッセージだけで送ってくる")
        self._focus_button.clicked.connect(self._toggle_focus)
        self._set_focus_button(False)
        client.get("/focus", lambda result: self._set_focus_button(result["enabled"]))
        character_column = QWidget()
        character_layout = QVBoxLayout(character_column)
        character_layout.setContentsMargins(0, 0, 0, 8)
        character_layout.addWidget(self._character_list, 1)
        character_layout.addWidget(self._focus_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        character_layout.addWidget(self._state_button, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._channel_list = QListWidget()
        self._channel_list.currentItemChanged.connect(self._on_channel_selected)

        self._chat_header = QLabel()
        self._chat_header.setObjectName("chatHeader")
        self._message_view = MessageView("")
        self._input = MessageInput()
        self._input.submitted.connect(self._send)
        self._send_button = QPushButton("送信")
        self._send_button.clicked.connect(self._send)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(16, 8, 16, 16)
        input_row.addWidget(self._input)
        input_row.addWidget(self._send_button, alignment=Qt.AlignmentFlag.AlignBottom)

        chat_pane = QWidget()
        chat_pane.setObjectName("chatPane")
        chat_layout = QVBoxLayout(chat_pane)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)
        chat_layout.addWidget(self._chat_header)
        chat_layout.addWidget(self._message_view, 1)
        chat_layout.addLayout(input_row)

        splitter = QSplitter()
        splitter.setHandleWidth(1)
        splitter.addWidget(_pane("characterPane", "キャラクター", character_column))
        splitter.addWidget(_pane("channelPane", "チャンネル", self._channel_list))
        splitter.addWidget(chat_pane)
        splitter.setSizes([180, 200, 620])
        splitter.setStretchFactor(2, 1)
        self.setCentralWidget(splitter)

        client.event_received.connect(self._on_event)
        client.connection_changed.connect(self._on_connection_changed)
        client.request_failed.connect(lambda detail: self.statusBar().showMessage(f"通信エラー：{detail}", 5000))
        self.statusBar().showMessage("コアに接続中…")

        for character in characters:
            item = QListWidgetItem(character["name"])
            item.setData(Qt.ItemDataRole.UserRole, character["id"])
            self._character_list.addItem(item)
            self._character_items[character["id"]] = item
            client.get(f"/characters/{character['id']}/state", self._update_presence)
        if characters:
            self._character_list.setCurrentRow(0)

    # --- 選択 ---

    def _on_character_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        self._current_character = item.data(Qt.ItemDataRole.UserRole)
        self._message_view.set_character_name(self._characters[self._current_character]["name"])
        self._channel_list.clear()
        for session in self._sessions.get(self._current_character, []):
            if session["kind"] in CHANNEL_KINDS:
                channel = QListWidgetItem(f"# {session['title']}")
                channel.setData(Qt.ItemDataRole.UserRole, session)
                self._channel_list.addItem(channel)
        if self._channel_list.count():
            self._channel_list.setCurrentRow(0)

    def _on_channel_selected(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        self._current_session = item.data(Qt.ItemDataRole.UserRole)
        self._chat_header.setText(f"# {self._current_session['title']}")
        self._message_view.clear()
        self._update_input_state()
        session_id = self._current_session["id"]
        self._client.get(f"/sessions/{session_id}/messages", lambda messages: self._load_history(session_id, messages))

    def _load_history(self, session_id: int, messages: list[dict]) -> None:
        if not self._current_session or self._current_session["id"] != session_id:
            return
        for message in messages:
            self._message_view.add_message(message)

    # --- 送信 ---

    def _send(self) -> None:
        content = self._input.toPlainText().strip()
        if not content or not self._current_session or self._current_session["id"] in self._busy_sessions:
            return
        self._input.clear()
        self._client.post(
            f"/sessions/{self._current_session['id']}/messages",
            {"content": content},
            on_error=lambda status, detail: self._message_view.add_notice(f"送信できませんでした（{status}）：{detail}"),
        )

    def _update_input_state(self) -> None:
        busy = bool(self._current_session) and self._current_session["id"] in self._busy_sessions
        self._send_button.setEnabled(not busy)

    # --- 対話ウィンドウ ---

    def open_dialogue(self, character_id: str, activate: bool = True) -> None:
        """対話ウィンドウを開く。キャラクターから話しかけるときは activate=False で、入力中の作業からフォーカスを奪わない。"""
        window = self._dialogue_windows.get(character_id)
        if window is None:
            session = next(s for s in self._sessions[character_id] if s["kind"] == "dialogue")
            window = DialogueWindow(self._client, self._characters[character_id], session["id"])
            self._dialogue_windows[character_id] = window
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activate)
        window.show()
        window.raise_()
        if activate:
            window.activateWindow()

    # --- 集中モード ---

    def _set_focus_button(self, enabled: bool) -> None:
        self._focus_button.setChecked(enabled)
        self._focus_button.setText(f"集中モード：{'ON' if enabled else 'OFF'}")

    def _toggle_focus(self) -> None:
        enabled = self._focus_button.isChecked()
        self._client.put("/focus", {"enabled": enabled}, lambda result: self._set_focus_button(result["enabled"]))

    # --- 状態 ---

    def _open_current_state(self) -> None:
        item = self._character_list.currentItem()
        if item is not None:
            self.open_state(item.data(Qt.ItemDataRole.UserRole))

    def open_state(self, character_id: str) -> None:
        window = self._state_windows.get(character_id)
        if window is None:
            window = StateWindow(self._client, self._characters[character_id])
            self._state_windows[character_id] = window
        else:
            window.reload()
        window.show()
        window.raise_()
        window.activateWindow()

    def _update_presence(self, state: dict) -> None:
        item = self._character_items.get(state["character_id"])
        if item is None:
            return
        name = self._characters[state["character_id"]]["name"]
        item.setText(f"{PRESENCE_MARKS.get(state['availability'], '')} {name}　{state['availability_label']}")

    # --- コアからのイベント ---

    def _on_connection_changed(self, connected: bool) -> None:
        self.statusBar().showMessage("コアに接続しました" if connected else "コアから切断されました（再接続します）", 0 if not connected else 3000)

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "state.updated":
            self._update_presence(event["state"])
            self._set_focus_button(event["state"]["focus_mode"])
            return
        if kind == "proactive.started":
            if event["mode"] == "dialogue":
                self.open_dialogue(event["character_id"], activate=False)
            return
        if kind == "dialogue.fallback":
            window = self._dialogue_windows.get(event["character_id"])
            if window is not None:
                window.hide()
            return
        session_id = event.get("session_id") or event.get("message", {}).get("session_id")
        if kind == "generation.started":
            self._busy_sessions.add(session_id)
        elif kind in ("message.completed", "error"):
            self._busy_sessions.discard(session_id)
        self._update_input_state()

        if not self._current_session or session_id != self._current_session["id"]:
            return
        if kind == "message.created":
            self._message_view.add_message(event["message"])
        elif kind == "generation.started":
            self._message_view.start_stream()
        elif kind == "message.delta":
            self._message_view.append_stream(event["delta"])
        elif kind == "message.completed":
            self._message_view.finish_stream(event["message"])
        elif kind == "error":
            self._message_view.abort_stream()
            self._message_view.add_notice(f"応答に失敗しました：{event.get('detail')}")

    def closeEvent(self, event) -> None:
        for window in [*self._dialogue_windows.values(), *self._state_windows.values()]:
            window.close()
        super().closeEvent(event)
