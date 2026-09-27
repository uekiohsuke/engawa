"""WebSocket 接続中のクライアントへのイベント配信。

イベント種別：
- message.created   {message}                      保存済みメッセージ（ユーザー発言など）
- generation.started {session_id}                   キャラクターの発話生成開始
- message.delta     {session_id, delta}            生成中のストリーム断片
- message.completed {message}                      生成完了・保存済みのキャラクター発言
- error             {session_id, detail}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)


class EventHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    def add(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def publish(self, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, **payload}
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:
                log.info("drop websocket client")
                self._clients.discard(ws)
