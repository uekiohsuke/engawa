"""内部状態の管理：定期更新（コードによる定期変化）・永続化・配信。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.state import CharacterState, StateEngine, StateEvent

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
StateListener = Callable[[str, list[StateEvent]], Awaitable[None]]
RECENT_EVENTS = 30
FOCUS_MODE_KEY = "focus_mode"


def system_clock() -> datetime:
    return datetime.now().astimezone()


class StateService:
    def __init__(
        self,
        db: Database,
        hub: EventHub,
        characters: Iterable[Character],
        clock: Clock = system_clock,
        tick_seconds: float = 60.0,
    ):
        self._db = db
        self._hub = hub
        self._clock = clock
        self._tick_seconds = tick_seconds
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[StateListener] = []
        self._focus_mode = bool(db.get_setting(FOCUS_MODE_KEY, False))
        now = clock()
        self._engines: dict[str, StateEngine] = {}
        for character in characters:
            saved = db.load_state(character.id)
            state = CharacterState.from_dict(saved) if saved else None
            self._engines[character.id] = StateEngine(character, state, now)

    def now(self) -> datetime:
        return self._clock()

    def add_listener(self, listener: StateListener) -> None:
        """状態イベント（閾値クロス・就寝・起床）の通知先を登録する。"""
        self._listeners.append(listener)

    # --- ループ ---

    async def start(self) -> None:
        await self.tick_all()  # 停止中に経過した時間を反映する
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            try:
                await self.tick_all()
            except Exception:
                log.exception("state tick failed")

    async def tick_all(self) -> None:
        now = self._clock()
        for character_id, engine in self._engines.items():
            await self._commit(character_id, engine.tick(now))

    async def refresh(self, character_id: str) -> None:
        """定期更新を待たずに現在時刻まで状態を進める。"""
        events = self._engines[character_id].tick(self._clock())
        if events:
            await self._commit(character_id, events)

    # --- 参照 ---

    def state(self, character_id: str) -> CharacterState:
        return self._engines[character_id].state

    def availability(self, character_id: str) -> str:
        return self._engines[character_id].availability(self._clock(), self._focus_mode)

    async def is_asleep(self, character_id: str) -> bool:
        await self.refresh(character_id)
        return self._engines[character_id].state.asleep

    def prompt_section(self, character_id: str) -> str:
        return self._engines[character_id].prompt_section(self._clock())

    def snapshot(self, character_id: str) -> dict[str, Any]:
        snapshot = self._engines[character_id].snapshot(self._clock(), self._focus_mode)
        snapshot["events"] = self._db.recent_state_events(character_id, RECENT_EVENTS)
        return snapshot

    # --- 変化 ---

    async def on_user_message(self, character_id: str) -> None:
        engine = self._engines[character_id]
        await self._commit(character_id, engine.on_user_message(self._clock()))

    async def on_proactive(self, character_id: str) -> None:
        engine = self._engines[character_id]
        await self._commit(character_id, engine.on_proactive(self._clock()))

    @property
    def focus_mode(self) -> bool:
        return self._focus_mode

    async def set_focus_mode(self, enabled: bool) -> None:
        self._focus_mode = enabled
        self._db.set_setting(FOCUS_MODE_KEY, enabled)
        for character_id in self._engines:
            await self._commit(character_id, [])

    async def record(self, character_id: str, kind: str, detail: str) -> None:
        """判定や能動発話など、状態モデルの外で起きた出来事を状態イベントとして記録する。"""
        self._db.add_state_event(character_id, kind, detail, self._clock().isoformat())
        await self._publish(character_id)

    async def _commit(self, character_id: str, events: list[StateEvent]) -> None:
        engine = self._engines[character_id]
        self._db.save_state(character_id, engine.state.to_dict())
        for event in events:
            log.info("state event [%s] %s: %s", character_id, event.kind, event.detail)
            self._db.add_state_event(character_id, event.kind, event.detail, event.at)
        await self._publish(character_id)
        if events:
            for listener in self._listeners:
                try:
                    await listener(character_id, events)
                except Exception:
                    log.exception("state listener failed")

    async def _publish(self, character_id: str) -> None:
        await self._hub.publish("state.updated", character_id=character_id, state=self.snapshot(character_id))
