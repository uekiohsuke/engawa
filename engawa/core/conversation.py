"""会話処理：プロンプト組立 → 生成 → 保存・配信。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.expression import ExpressionFilter, expression_section
from engawa.core.llm import INSTRUCTION_PREFIX, ChatMessage, LLMClient
from engawa.core.memory import RECALL_SESSION_KINDS, MemoryService
from engawa.core.state_service import StateService
from engawa.core.timewords import describe_elapsed, message_stamp

log = logging.getLogger(__name__)

ROLE_TO_LLM = {"user": "user", "character": "assistant"}


class SessionBusyError(Exception):
    pass


FAREWELL_WINDOW = timedelta(minutes=15)
SUB_ARCHIVE_AFTER = timedelta(days=3)
ELAPSED_NOTICE_AFTER = timedelta(minutes=30)  # これ以上間が空いたら「〜ぶり」を伝える


def time_section(elapsed: timedelta | None) -> str:
    lines = [
        "## 時間の流れ",
        "- マスターの発言の先頭の（月/日 時:分）は、その発言をした日時。返事には書かないこと。",
    ]
    if elapsed is not None and elapsed >= ELAPSED_NOTICE_AFTER:
        lines.append(f"- この会話での前回のやりとりから{describe_elapsed(elapsed)}ぶり。間が空いたことを踏まえて話すこと。")
    return "\n".join(lines)


def asleep_notice(character: Character) -> str:
    return f"（{character.name}は眠っている。返事はない……）"


def should_say_goodnight(sleep_at: datetime, last_user_at: datetime | None) -> bool:
    """眠りに落ちた時刻の前後にマスターと話していたなら、おやすみを言って切り上げる（仕様6-3）。

    「前後」なのは、定期更新の合間に話しかけられて、その発言で眠りに落ちたと判明する場合があるため。
    """
    return last_user_at is not None and abs(sleep_at - last_user_at) < FAREWELL_WINDOW


class ConversationService:
    def __init__(
        self,
        db: Database,
        llm: LLMClient,
        hub: EventHub,
        state: StateService,
        history_window: int,
        memory: MemoryService | None = None,
    ):
        self._db = db
        self._llm = llm
        self._hub = hub
        self._state = state
        self._history_window = history_window
        self._memory = memory
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def is_busy(self, session_id: int) -> bool:
        return session_id in self._busy

    # --- サブスレッド（サブチャンネル） ---

    async def create_sub_session(self, character_id: str, title: str) -> dict[str, Any]:
        session = self._db.create_session(character_id, "sub", title, self._state.now().isoformat())
        await self._hub.publish("session.created", session=session)
        return session

    async def update_session(self, session_id: int, *, title: str | None = None, archived: bool | None = None) -> dict[str, Any]:
        self._db.update_session(session_id, title=title, archived=archived)
        session = self._db.get_session(session_id)
        await self._hub.publish("session.updated", session=session)
        return session

    async def archive_stale_sessions(self, character_id: str) -> int:
        """最後の発言から一定期間たったサブスレッドを一覧から隠す（削除はしない）。"""
        now = self._state.now()
        archived = 0
        for session in self._db.list_sessions(character_id):
            if session["kind"] != "sub" or session["archived"]:
                continue
            last = max(
                datetime.fromisoformat(t) for t in (session["created_at"], session["last_message_at"]) if t
            )
            if now - last >= SUB_ARCHIVE_AFTER:
                await self.update_session(session["id"], archived=True)
                archived += 1
        return archived

    def build_system_prompt(
        self,
        character: Character,
        related: list[dict[str, Any]] | None = None,
        elapsed: timedelta | None = None,
    ) -> str:
        sections = [character.system_prompt()]
        if self._memory:
            sections.append(self._memory.ltm_section(character.id))
        sections.append(self._state.prompt_section(character.id))
        sections.append(time_section(elapsed))
        if self._memory and related:
            sections.append(self._memory.related_section(character, related))
        sections.append(expression_section(character.images.standing))
        return "\n\n".join(s for s in sections if s)

    def build_messages(
        self,
        character: Character,
        session_id: int,
        instruction: str | None = None,
        related: list[dict[str, Any]] | None = None,
    ) -> list[ChatMessage]:
        history = self._db.recent_messages(session_id, self._history_window)
        uses_tags = expression_section(character.images.standing) is not None
        messages: list[ChatMessage] = [
            {"role": "system", "content": self.build_system_prompt(character, related, self._elapsed(history, instruction))}
        ]
        for m in history:
            role = ROLE_TO_LLM.get(m["role"])
            if role == "user":
                messages.append({"role": role, "content": message_stamp(m["created_at"]) + m["content"]})
            elif role == "assistant":
                # 過去の返事にもタグを戻しておく（履歴にタグがないと、付けなくなっていくため）
                # 今は登録されていない表情のタグは戻さない（真似して使わないように）
                tag = f"[{m['expression']}] " if uses_tags and m.get("expression") in character.images.standing else ""
                messages.append({"role": role, "content": tag + m["content"]})
        if instruction:
            # 能動発話など、ユーザー発言ではない最終ターン。履歴には保存しない
            messages.append({"role": "user", "content": INSTRUCTION_PREFIX + instruction})
        return messages

    def _elapsed(self, history: list[dict[str, Any]], instruction: str | None) -> timedelta | None:
        """前回のやりとりからの経過時間。返事なら「今の発言とその前の発言」の間、話しかけなら「今と最後の発言」の間。"""
        if instruction is not None:
            if not history:
                return None
            return self._state.now() - datetime.fromisoformat(history[-1]["created_at"])
        if len(history) < 2:
            return None
        return datetime.fromisoformat(history[-1]["created_at"]) - datetime.fromisoformat(history[-2]["created_at"])

    def _add_message(self, session_id: int, role: str, content: str, expression: str | None = None) -> dict[str, Any]:
        return self._db.add_message(session_id, role, content, self._state.now().isoformat(), expression)

    async def post_user_message(self, character: Character, session_id: int, content: str) -> dict[str, Any]:
        """ユーザー発言を保存し、キャラクターの応答生成をバックグラウンドで開始する。"""
        if session_id in self._busy:
            raise SessionBusyError(session_id)
        self._busy.add(session_id)
        if self._db.get_session(session_id)["archived"]:
            await self.update_session(session_id, archived=False)  # アーカイブ済みのチャンネルで話したら戻す
        message = self._add_message(session_id, "user", content)
        await self._hub.publish("message.created", message=message)
        was_asleep = self._state.state(character.id).asleep
        if await self._state.is_asleep(character.id):
            if not was_asleep and self._just_fell_asleep_while_talking(character.id, message):
                # おやすみの一言（能動発話側が生成する）をこの発言への返事にする
                self._busy.discard(session_id)
                await self._remember(character, session_id, message)
                return message
            # 睡眠中は無反応。定型文の表示のみ（仕様6-3）
            notice = self._add_message(session_id, "system", asleep_notice(character))
            await self._hub.publish("message.created", message=notice)
            await self._remember(character, session_id, message)
            self._busy.discard(session_id)
            return message
        await self._state.on_user_message(character.id)
        task = asyncio.create_task(self._generate(character, session_id, query=message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return message

    def _just_fell_asleep_while_talking(self, character_id: str, message: dict[str, Any]) -> bool:
        sleep = next((e for e in self._db.recent_state_events(character_id, 10) if e["kind"] == "sleep"), None)
        return sleep is not None and should_say_goodnight(
            datetime.fromisoformat(sleep["created_at"]), datetime.fromisoformat(message["created_at"])
        )

    async def speak(
        self, character: Character, session_id: int, instruction: str, topic: str | None = None
    ) -> dict[str, Any] | None:
        """キャラクターから話しかける（能動発話）。生成し終えたメッセージを返す。セッションが使用中なら何もしない。

        topic（会話の種など）があれば、それに関連する記憶も思い出す。
        """
        if session_id in self._busy:
            return None
        self._busy.add(session_id)
        query = {"id": None, "content": topic} if topic else None
        return await self._generate(character, session_id, instruction, query=query)

    async def post_character_message(self, session_id: int, content: str) -> dict[str, Any]:
        """生成を伴わずにキャラクターの発言を置く（対話→メッセージの送り直しなど）。"""
        message = self._add_message(session_id, "character", content)
        await self._hub.publish("message.created", message=message)
        return message

    async def _remember(
        self, character: Character, session_id: int, message: dict[str, Any], embedding: list[float] | None = None
    ) -> None:
        if self._memory:
            await self._memory.record_message(character.id, message, embedding)

    async def _recall(
        self, character: Character, session_id: int, query: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """query（ユーザー発言や会話の種）に関連するSTMを探す。ユーザー発言ならSTMへの登録もここで行う。"""
        if not self._memory or query is None:
            return []
        # サブスレッドは STM を参照しない（仕様4-1）。発言の記録は行う
        uses_stm = self._db.get_session(session_id)["kind"] in RECALL_SESSION_KINDS
        vector = await self._memory.embed_query(query["content"]) if uses_stm else None
        if query.get("id"):  # ユーザー発言
            await self._remember(character, session_id, query, vector)
        if not uses_stm:
            return []
        # 直近の会話履歴としてそのまま渡す発言は、思い出す対象から外す
        window = [m["id"] for m in self._db.recent_messages(session_id, self._history_window)]
        return await self._memory.retrieve(character.id, vector, exclude_message_ids=window)

    async def _generate(
        self,
        character: Character,
        session_id: int,
        instruction: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            await self._hub.publish("generation.started", session_id=session_id)
            related = await self._recall(character, session_id, query)
            chunks: list[str] = []
            messages = self.build_messages(character, session_id, instruction, related)
            tags = ExpressionFilter(character.images.standing)
            announced = False

            async def emit(text: str) -> None:
                nonlocal announced
                if tags.expression and not announced:
                    # 表情は本文より先に届ける（言い始めと同時に立ち絵が変わるように）
                    announced = True
                    await self._hub.publish(
                        "expression.changed", session_id=session_id, character_id=character.id, expression=tags.expression
                    )
                if text:
                    chunks.append(text)
                    await self._hub.publish("message.delta", session_id=session_id, delta=text)

            async for delta in self._llm.stream_chat(messages):
                await emit(tags.feed(delta))
            await emit(tags.flush())
            reply = "".join(chunks).strip()
            if not reply:
                raise RuntimeError("LLM returned an empty reply")
            message = self._add_message(session_id, "character", reply, tags.expression)
            await self._hub.publish("message.completed", message=message)
            await self._remember(character, session_id, message)
            return message
        except Exception as e:
            log.exception("generation failed (session %s)", session_id)
            await self._hub.publish("error", session_id=session_id, detail=f"{type(e).__name__}: {e}")
            return None
        finally:
            self._busy.discard(session_id)
