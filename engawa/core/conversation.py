"""会話処理：プロンプト組立 → 生成 → 保存・配信。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import ChatMessage, LLMClient

log = logging.getLogger(__name__)

ROLE_TO_LLM = {"user": "user", "character": "assistant"}


class SessionBusyError(Exception):
    pass


class ConversationService:
    def __init__(self, db: Database, llm: LLMClient, hub: EventHub, history_window: int):
        self._db = db
        self._llm = llm
        self._hub = hub
        self._history_window = history_window
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def is_busy(self, session_id: int) -> bool:
        return session_id in self._busy

    def build_system_prompt(self, character: Character) -> str:
        # 次段階：内部状態（暇度・疲労・眠気）の「現在の状態」セクションや LTM をここで追記する（仕様6章・8章）
        return character.system_prompt()

    def build_messages(self, character: Character, session_id: int) -> list[ChatMessage]:
        messages: list[ChatMessage] = [{"role": "system", "content": self.build_system_prompt(character)}]
        for m in self._db.recent_messages(session_id, self._history_window):
            role = ROLE_TO_LLM.get(m["role"])
            if role:
                messages.append({"role": role, "content": m["content"]})
        return messages

    async def post_user_message(self, character: Character, session_id: int, content: str) -> dict[str, Any]:
        """ユーザー発言を保存し、キャラクターの応答生成をバックグラウンドで開始する。"""
        if session_id in self._busy:
            raise SessionBusyError(session_id)
        self._busy.add(session_id)
        message = self._db.add_message(session_id, "user", content)
        await self._hub.publish("message.created", message=message)
        task = asyncio.create_task(self._generate(character, session_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return message

    async def _generate(self, character: Character, session_id: int) -> None:
        try:
            await self._hub.publish("generation.started", session_id=session_id)
            chunks: list[str] = []
            async for delta in self._llm.stream_chat(self.build_messages(character, session_id)):
                chunks.append(delta)
                await self._hub.publish("message.delta", session_id=session_id, delta=delta)
            reply = "".join(chunks).strip()
            if not reply:
                raise RuntimeError("LLM returned an empty reply")
            message = self._db.add_message(session_id, "character", reply)
            await self._hub.publish("message.completed", message=message)
        except Exception as e:
            log.exception("generation failed (session %s)", session_id)
            await self._hub.publish("error", session_id=session_id, detail=f"{type(e).__name__}: {e}")
        finally:
            self._busy.discard(session_id)
