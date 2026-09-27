"""内部状態の管理：定期更新（コードによる定期変化）・永続化・配信。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.state import CharacterState, StateEngine, StateEvent

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
RECENT_EVENTS = 30


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
        now = clock()
        self._engines: dict[str, StateEngine] = {}
        for character in characters:
            saved = db.load_state(character.id)
            state = CharacterState.from_dict(saved) if saved else None
            self._engines[character.id] = StateEngine(character, state, now)

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

    # --- 会話との接点 ---

    async def refresh(self, character_id: str) -> None:
        """定期更新を待たずに現在時刻まで状態を進める。"""
        events = self._engines[character_id].tick(self._clock())
        if events:
            await self._commit(character_id, events)

    async def is_asleep(self, character_id: str) -> bool:
        await self.refresh(character_id)
        return self._engines[character_id].state.asleep

    async def on_user_message(self, character_id: str) -> None:
        engine = self._engines[character_id]
        await self._commit(character_id, engine.on_user_message(self._clock()))

    def prompt_section(self, character_id: str) -> str:
        return self._engines[character_id].prompt_section(self._clock())

    def snapshot(self, character_id: str) -> dict[str, Any]:
        snapshot = self._engines[character_id].snapshot(self._clock())
        snapshot["events"] = self._db.recent_state_events(character_id, RECENT_EVENTS)
        return snapshot

    async def _commit(self, character_id: str, events: list[StateEvent]) -> None:
        engine = self._engines[character_id]
        self._db.save_state(character_id, engine.state.to_dict())
        for event in events:
            log.info("state event [%s] %s: %s", character_id, event.kind, event.detail)
            self._db.add_state_event(character_id, event.kind, event.detail, event.at)
        await self._hub.publish("state.updated", character_id=character_id, state=self.snapshot(character_id))
