"""チャットアプリ本体（Discord 風）。

左：キャラクター一覧（フレンド一覧）。ダブルクリックで対話ウィンドウを開く（仕様5章）。
中：チャンネル一覧。#main（メインスレッド）と、話題ごとのサブチャンネル（サブスレッド）。
右：メッセージ欄と入力欄。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engawa.ui.activity import ActivityWatcher
from engawa.ui.client import CoreClient
from engawa.ui.dialogue_window import DialogueWindow
from engawa.ui.images import icon_pixmap
from engawa.ui.state_window import StateWindow
from engawa.ui.tts import Speaker
from engawa.ui.widgets import COLORS, MessageView

PRESENCE_MARKS = {"both": "🟢", "message_only": "🟡", "sleeping": "🌙"}
UNREAD_MARK = "●"
WATCH_SETTING = "activity/enabled"
CHARACTER_ICON_SIZE = 32


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
    def __init__(
        self,
        client: CoreClient,
        characters: list[dict],
        sessions: dict[str, list[dict]],
        speaker: Speaker | None = None,
        watcher: ActivityWatcher | None = None,
    ):
        super().__init__()
        self._client = client
        self._speaker = speaker
        self._watcher = watcher
        self._characters = {c["id"]: c for c in characters}
        self._sessions: dict[int, dict] = {s["id"]: s for ss in sessions.values() for s in ss}
        self._current_character: str | None = None
        self._current_session: dict | None = None
        self._busy_sessions: set[int] = set()
        self._unread: set[int] = set()
        self._show_archived = False
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
        if watcher is not None:
            self._watch_button = QPushButton()
            self._watch_button.setObjectName("focusButton")
            self._watch_button.setCheckable(True)
            self._watch_button.setToolTip(
                "前面のウィンドウのタイトルとアプリ名を、会話の種の材料にする\n"
                "（集中モード中・除外アプリ・縁側自身のウィンドウは記録しない）"
            )
            self._watch_button.toggled.connect(self._set_watching)
            self._watch_button.setChecked(QSettings().value(WATCH_SETTING, True, type=bool))
            self._set_watching(self._watch_button.isChecked())
            character_layout.addWidget(self._watch_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        character_layout.addWidget(self._focus_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        character_layout.addWidget(self._state_button, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._channel_list = QListWidget()
        self._channel_list.currentItemChanged.connect(self._on_channel_selected)
        self._channel_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._channel_list.customContextMenuRequested.connect(self._channel_menu)
        new_channel_button = QPushButton("＋ 新しい話題")
        new_channel_button.setToolTip("サブチャンネルを立てる（サブチャンネルでは長期記憶だけを参照する）")
        new_channel_button.clicked.connect(self._new_channel)
        self._archived_button = QPushButton("アーカイブを表示")
        self._archived_button.setObjectName("focusButton")
        self._archived_button.setCheckable(True)
        self._archived_button.setToolTip("3日間発言のないサブチャンネルは自動でアーカイブされる")
        self._archived_button.clicked.connect(self._toggle_archived)
        channel_column = QWidget()
        channel_layout = QVBoxLayout(channel_column)
        channel_layout.setContentsMargins(0, 0, 0, 8)
        channel_layout.addWidget(self._channel_list, 1)
        channel_layout.addWidget(new_channel_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        channel_layout.addWidget(self._archived_button, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._chat_header = QLabel()
        self._chat_header.setObjectName("chatHeader")
        self._message_view = MessageView()
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
        splitter.addWidget(_pane("channelPane", "チャンネル", channel_column))
        splitter.addWidget(chat_pane)
        splitter.setSizes([180, 200, 620])
        splitter.setStretchFactor(2, 1)
        self.setCentralWidget(splitter)

        client.event_received.connect(self._on_event)
        client.connection_changed.connect(self._on_connection_changed)
        client.request_failed.connect(lambda detail: self.statusBar().showMessage(f"通信エラー：{detail}", 5000))
        self.statusBar().showMessage("コアに接続中…")

        self._character_list.setIconSize(QSize(CHARACTER_ICON_SIZE, CHARACTER_ICON_SIZE))
        for character in characters:
            item = QListWidgetItem(character["name"])
            item.setData(Qt.ItemDataRole.UserRole, character["id"])
            item.setIcon(QIcon(icon_pixmap(character.get("icon"), CHARACTER_ICON_SIZE, character["name"], COLORS["character"])))
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
        self._message_view.set_character(self._characters[self._current_character])
        self._current_session = None
        self._render_channels()

    def _session_of_kind(self, character_id: str, kind: str) -> dict:
        return next(s for s in self._sessions.values() if s["character_id"] == character_id and s["kind"] == kind)

    def _render_channels(self, select_id: int | None = None) -> None:
        """チャンネル一覧を描き直す。#main、サブチャンネル（新しい順）、アーカイブ（表示時のみ）の順。"""
        if self._current_character is None:
            return
        select_id = select_id or (self._current_session["id"] if self._current_session else None)
        subs = [s for s in self._sessions.values() if s["character_id"] == self._current_character and s["kind"] == "sub"]
        subs.sort(key=lambda s: s["last_message_at"] or s["created_at"], reverse=True)
        active = [s for s in subs if not s["archived"]]
        archived = [s for s in subs if s["archived"]] if self._show_archived else []

        self._channel_list.blockSignals(True)
        self._channel_list.clear()
        for session in [self._session_of_kind(self._current_character, "main"), *active]:
            self._add_channel_item(session)
        if archived:
            separator = QListWidgetItem("― アーカイブ ―")
            separator.setFlags(Qt.ItemFlag.NoItemFlags)
            separator.setForeground(QColor(COLORS["text_muted"]))
            self._channel_list.addItem(separator)
            for session in archived:
                self._add_channel_item(session)
        self._channel_list.blockSignals(False)

        target = select_id or self._session_of_kind(self._current_character, "main")["id"]
        for row in range(self._channel_list.count()):
            item = self._channel_list.item(row)
            session = item.data(Qt.ItemDataRole.UserRole)
            if session and session["id"] == target:
                if self._current_session and self._current_session["id"] == target:
                    self._channel_list.blockSignals(True)  # 同じチャンネルなら履歴を読み直さない
                    self._channel_list.setCurrentItem(item)
                    self._channel_list.blockSignals(False)
                else:
                    self._channel_list.setCurrentItem(item)
                return
        self._channel_list.setCurrentRow(0)

    def _add_channel_item(self, session: dict) -> None:
        mark = f"{UNREAD_MARK} " if session["id"] in self._unread else ""
        item = QListWidgetItem(f"{mark}# {session['title']}")
        item.setData(Qt.ItemDataRole.UserRole, session)
        item.setToolTip(session["title"] + ("（右クリックで名前変更・アーカイブ）" if session["kind"] == "sub" else ""))
        if session["archived"]:
            item.setForeground(QColor(COLORS["text_muted"]))
        elif session["id"] in self._unread:
            item.setForeground(QColor("white"))
        self._channel_list.addItem(item)

    def _on_channel_selected(self, item: QListWidgetItem | None) -> None:
        if item is None or item.data(Qt.ItemDataRole.UserRole) is None:
            return
        self._current_session = self._sessions[item.data(Qt.ItemDataRole.UserRole)["id"]]
        session_id = self._current_session["id"]
        if session_id in self._unread:
            self._unread.discard(session_id)
            item.setText(f"# {self._current_session['title']}")
        self._update_chat_header()
        self._message_view.clear()
        self._update_input_state()
        self._client.get(f"/sessions/{session_id}/messages", lambda messages: self._load_history(session_id, messages))

    def _update_chat_header(self) -> None:
        session = self._current_session
        suffix = "　（アーカイブ済み：発言すると戻ります）" if session["archived"] else ""
        note = "　— 長期記憶だけを参照" if session["kind"] == "sub" else ""
        self._chat_header.setText(f"# {session['title']}{note}{suffix}")

    # --- サブチャンネルの操作 ---

    def _new_channel(self) -> None:
        if self._current_character is None:
            return
        title, ok = QInputDialog.getText(self, "新しい話題", "チャンネル名：")
        if ok and title.strip():
            self._client.post(
                f"/characters/{self._current_character}/sessions",
                {"title": title.strip()},
                lambda session: self._on_session_changed(session, select=True),
            )

    def _toggle_archived(self) -> None:
        self._show_archived = self._archived_button.isChecked()
        self._archived_button.setText("アーカイブを隠す" if self._show_archived else "アーカイブを表示")
        self._render_channels()

    def _channel_menu(self, pos: QPoint) -> None:
        item = self._channel_list.itemAt(pos)
        session = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not session or session["kind"] != "sub":
            return
        menu = QMenu(self)
        rename = menu.addAction("名前を変更")
        toggle = menu.addAction("アーカイブから戻す" if session["archived"] else "アーカイブする")
        chosen = menu.exec(self._channel_list.mapToGlobal(pos))
        if chosen == rename:
            title, ok = QInputDialog.getText(self, "名前を変更", "チャンネル名：", text=session["title"])
            if ok and title.strip():
                self._client.patch(f"/sessions/{session['id']}", {"title": title.strip()})
        elif chosen == toggle:
            self._client.patch(f"/sessions/{session['id']}", {"archived": not session["archived"]})

    def _on_session_changed(self, session: dict, select: bool = False) -> None:
        self._sessions[session["id"]] = session
        if self._current_session and self._current_session["id"] == session["id"]:
            self._current_session = session
            self._update_chat_header()
        if session["character_id"] == self._current_character:
            self._render_channels(select_id=session["id"] if select else None)

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
            session = self._session_of_kind(character_id, "dialogue")
            window = DialogueWindow(self._client, self._characters[character_id], session["id"], self._speaker)
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
        if self._watcher is not None:
            self._watcher.focus_mode = enabled  # 集中モード中は画面を見ない

    # --- 画面を見る（会話の種の材料） ---

    def _set_watching(self, enabled: bool) -> None:
        self._watch_button.setText(f"画面を見る：{'ON' if enabled else 'OFF'}")
        QSettings().setValue(WATCH_SETTING, enabled)
        self._watcher.enabled = enabled

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
        if kind in ("session.created", "session.updated"):
            self._on_session_changed(event["session"])
            return
        session_id = event.get("session_id") or event.get("message", {}).get("session_id")
        if kind == "generation.started":
            self._busy_sessions.add(session_id)
        elif kind in ("message.completed", "error"):
            self._busy_sessions.discard(session_id)
        self._update_input_state()
        if kind in ("message.created", "message.completed"):
            self._on_new_message(event["message"])

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

    def _on_new_message(self, message: dict) -> None:
        """チャンネルの並び順（新しい順）と未読マークを更新する。対話セッションは一覧に出ないので対象外。"""
        session = self._sessions.get(message["session_id"])
        if session is None or session["kind"] == "dialogue":
            return
        session["last_message_at"] = message["created_at"]
        is_current = self._current_session is not None and self._current_session["id"] == session["id"]
        if not is_current and message["role"] != "user":
            self._unread.add(session["id"])
        if session["character_id"] == self._current_character:
            self._render_channels()

    def closeEvent(self, event) -> None:
        for window in [*self._dialogue_windows.values(), *self._state_windows.values()]:
            window.close()
        super().closeEvent(event)
