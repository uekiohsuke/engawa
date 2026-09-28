"""コアサーバーとの通信（REST＋WebSocket）。Qt のイベントループ上で非同期に動くのでスレッドは使わない。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket, QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

JsonCallback = Callable[[Any], None]
ErrorCallback = Callable[[int, str], None]

RECONNECT_INTERVAL_MS = 3000


class CoreClient(QObject):
    event_received = Signal(dict)
    connection_changed = Signal(bool)
    request_failed = Signal(str)

    def __init__(self, base_url: str, parent: QObject | None = None):
        super().__init__(parent)
        self._base_url = base_url
        self._nam = QNetworkAccessManager(self)
        self._ws = QWebSocket(parent=self)
        self._ws.connected.connect(lambda: self.connection_changed.emit(True))
        self._ws.disconnected.connect(self._on_ws_disconnected)
        self._ws.textMessageReceived.connect(self._on_ws_message)
        self._reconnect_timer = QTimer(self, singleShot=True, interval=RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self.connect_events)

    # --- REST ---

    def get(self, path: str, on_success: JsonCallback, on_error: ErrorCallback | None = None) -> None:
        reply = self._nam.get(QNetworkRequest(QUrl(self._base_url + path)))
        reply.finished.connect(lambda: self._handle_reply(reply, on_success, on_error))

    def post(
        self,
        path: str,
        body: dict[str, Any],
        on_success: JsonCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._send_json("POST", path, body, on_success, on_error)

    def put(
        self,
        path: str,
        body: dict[str, Any],
        on_success: JsonCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._send_json("PUT", path, body, on_success, on_error)

    def patch(
        self,
        path: str,
        body: dict[str, Any],
        on_success: JsonCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> None:
        self._send_json("PATCH", path, body, on_success, on_error)

    def _send_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        on_success: JsonCallback | None,
        on_error: ErrorCallback | None,
    ) -> None:
        request = QNetworkRequest(QUrl(self._base_url + path))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        data = json.dumps(body).encode("utf-8")
        if method == "POST":
            reply = self._nam.post(request, data)
        elif method == "PUT":
            reply = self._nam.put(request, data)
        else:
            reply = self._nam.sendCustomRequest(request, method.encode("ascii"), data)
        reply.finished.connect(lambda: self._handle_reply(reply, on_success, on_error))

    def _handle_reply(self, reply: QNetworkReply, on_success: JsonCallback | None, on_error: ErrorCallback | None) -> None:
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        data = bytes(reply.readAll().data())
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            detail = reply.errorString()
            try:
                detail = json.loads(data).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            if on_error:
                on_error(status, detail)
            else:
                self.request_failed.emit(detail)
            return
        if on_success:
            on_success(json.loads(data) if data else None)

    # --- WebSocket ---

    def connect_events(self) -> None:
        if self._ws.state() == QAbstractSocket.SocketState.UnconnectedState:
            self._ws.open(QUrl(self._base_url.replace("http", "ws", 1) + "/ws"))

    def _on_ws_disconnected(self) -> None:
        self.connection_changed.emit(False)
        self._reconnect_timer.start()

    def _on_ws_message(self, text: str) -> None:
        try:
            self.event_received.emit(json.loads(text))
        except ValueError:
            pass
