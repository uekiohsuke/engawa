"""会話処理：プロンプト組立 → 生成 → 保存・配信。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import INSTRUCTION_PREFIX, ChatMessage, LLMClient
from engawa.core.state_service import StateService

log = logging.getLogger(__name__)

ROLE_TO_LLM = {"user": "user", "character": "assistant"}


class SessionBusyError(Exception):
    pass


FAREWELL_WINDOW = timedelta(minutes=15)


def asleep_notice(character: Character) -> str:
    return f"（{character.name}は眠っている。返事はない……）"


def should_say_goodnight(sleep_at: datetime, last_user_at: datetime | None) -> bool:
    """眠りに落ちた時刻の前後にマスターと話していたなら、おやすみを言って切り上げる（仕様6-3）。

    「前後」なのは、定期更新の合間に話しかけられて、その発言で眠りに落ちたと判明する場合があるため。
    """
    return last_user_at is not None and abs(sleep_at - last_user_at) < FAREWELL_WINDOW


class ConversationService:
    def __init__(self, db: Database, llm: LLMClient, hub: EventHub, state: StateService, history_window: int):
        self._db = db
        self._llm = llm
        self._hub = hub
        self._state = state
        self._history_window = history_window
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def is_busy(self, session_id: int) -> bool:
        return session_id in self._busy

    def build_system_prompt(self, character: Character) -> str:
        # 次段階：LTM もここで追記する（仕様8章）
        return f"{character.system_prompt()}\n\n{self._state.prompt_section(character.id)}"

    def build_messages(
        self, character: Character, session_id: int, instruction: str | None = None
    ) -> list[ChatMessage]:
        messages: list[ChatMessage] = [{"role": "system", "content": self.build_system_prompt(character)}]
        for m in self._db.recent_messages(session_id, self._history_window):
            role = ROLE_TO_LLM.get(m["role"])
            if role:
                messages.append({"role": role, "content": m["content"]})
        if instruction:
            # 能動発話など、ユーザー発言ではない最終ターン。履歴には保存しない
            messages.append({"role": "user", "content": INSTRUCTION_PREFIX + instruction})
        return messages

    def _add_message(self, session_id: int, role: str, content: str) -> dict[str, Any]:
        return self._db.add_message(session_id, role, content, self._state.now().isoformat())

    async def post_user_message(self, character: Character, session_id: int, content: str) -> dict[str, Any]:
        """ユーザー発言を保存し、キャラクターの応答生成をバックグラウンドで開始する。"""
        if session_id in self._busy:
            raise SessionBusyError(session_id)
        self._busy.add(session_id)
        message = self._add_message(session_id, "user", content)
        await self._hub.publish("message.created", message=message)
        was_asleep = self._state.state(character.id).asleep
        if await self._state.is_asleep(character.id):
            if not was_asleep and self._just_fell_asleep_while_talking(character.id, message):
                # おやすみの一言（能動発話側が生成する）をこの発言への返事にする
                self._busy.discard(session_id)
                return message
            # 睡眠中は無反応。定型文の表示のみ（仕様6-3）
            notice = self._add_message(session_id, "system", asleep_notice(character))
            await self._hub.publish("message.created", message=notice)
            self._busy.discard(session_id)
            return message
        await self._state.on_user_message(character.id)
        task = asyncio.create_task(self._generate(character, session_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return message

    def _just_fell_asleep_while_talking(self, character_id: str, message: dict[str, Any]) -> bool:
        sleep = next((e for e in self._db.recent_state_events(character_id, 10) if e["kind"] == "sleep"), None)
        return sleep is not None and should_say_goodnight(
            datetime.fromisoformat(sleep["created_at"]), datetime.fromisoformat(message["created_at"])
        )

    async def speak(self, character: Character, session_id: int, instruction: str) -> dict[str, Any] | None:
        """キャラクターから話しかける（能動発話）。生成し終えたメッセージを返す。セッションが使用中なら何もしない。"""
        if session_id in self._busy:
            return None
        self._busy.add(session_id)
        return await self._generate(character, session_id, instruction)

    async def post_character_message(self, session_id: int, content: str) -> dict[str, Any]:
        """生成を伴わずにキャラクターの発言を置く（対話→メッセージの送り直しなど）。"""
        message = self._add_message(session_id, "character", content)
        await self._hub.publish("message.created", message=message)
        return message

    async def _generate(
        self, character: Character, session_id: int, instruction: str | None = None
    ) -> dict[str, Any] | None:
        try:
            await self._hub.publish("generation.started", session_id=session_id)
            chunks: list[str] = []
            async for delta in self._llm.stream_chat(self.build_messages(character, session_id, instruction)):
                chunks.append(delta)
                await self._hub.publish("message.delta", session_id=session_id, delta=delta)
            reply = "".join(chunks).strip()
            if not reply:
                raise RuntimeError("LLM returned an empty reply")
            message = self._add_message(session_id, "character", reply)
            await self._hub.publish("message.completed", message=message)
            return message
        except Exception as e:
            log.exception("generation failed (session %s)", session_id)
            await self._hub.publish("error", session_id=session_id, detail=f"{type(e).__name__}: {e}")
            return None
        finally:
            self._busy.discard(session_id)
