"""FastAPI アプリ本体。"""

from __future__ import annotations

import random
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from engawa.characters import CHARACTERS, get_character
from engawa.config import Settings, load_settings
from engawa.core.conversation import ConversationService, SessionBusyError
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import LLMClient, create_judge_llm, create_llm
from engawa.core.proactive import ProactiveService
from engawa.core.state_service import Clock, StateService, system_clock


class PostMessage(BaseModel):
    content: str = Field(min_length=1)


class FocusMode(BaseModel):
    enabled: bool


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    db: Database | None = None,
    clock: Clock = system_clock,
    judge_llm: LLMClient | None = None,
    rand: Callable[[], float] = random.random,
) -> FastAPI:
    settings = settings or load_settings()
    llm = llm or create_llm(settings)
    db = db or Database(settings.db_path)
    hub = EventHub()
    judge_llm = judge_llm or create_judge_llm(settings)
    state = StateService(db, hub, CHARACTERS.values(), clock, settings.state_tick_seconds)
    conversation = ConversationService(db, llm, hub, state, settings.history_window)
    proactive = ProactiveService(
        db,
        hub,
        state,
        conversation,
        judge_llm,
        CHARACTERS.values(),
        settings.judge_interval_seconds,
        settings.dialogue_timeout_seconds,
        rand,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for character_id in CHARACTERS:
            db.ensure_default_sessions(character_id)
        await state.start()
        await proactive.start()
        yield
        await proactive.stop()
        await state.stop()
        db.close()

    app = FastAPI(title="Engawa Core", lifespan=lifespan)
    app.state.services = {"db": db, "state": state, "conversation": conversation, "proactive": proactive}

    def require_session(session_id: int) -> dict:
        session = db.get_session(session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    @app.get("/health")
    async def health():
        return {"status": "ok", "llm": llm.name, "llm_reachable": await llm.ping()}

    @app.get("/characters")
    async def list_characters():
        return [{"id": c.id, "name": c.name} for c in CHARACTERS.values()]

    @app.get("/characters/{character_id}/sessions")
    async def list_sessions(character_id: str):
        if get_character(character_id) is None:
            raise HTTPException(404, "character not found")
        return db.list_sessions(character_id)

    @app.get("/characters/{character_id}/state")
    async def get_state(character_id: str):
        if get_character(character_id) is None:
            raise HTTPException(404, "character not found")
        await state.refresh(character_id)
        return state.snapshot(character_id)

    @app.post("/characters/{character_id}/judge")
    async def judge_now(character_id: str):
        """調整用：ゲートを無視して今すぐ判定層を呼ぶ。"""
        if get_character(character_id) is None:
            raise HTTPException(404, "character not found")
        decision = await proactive.evaluate(character_id, force=True)
        if decision is None:
            return {"judged": False, "reason": "睡眠中"}
        return {"judged": True, "speak": decision.speak, "intent": decision.intent, "reason": decision.reason}

    @app.get("/focus")
    async def get_focus():
        return {"enabled": state.focus_mode}

    @app.put("/focus")
    async def put_focus(body: FocusMode):
        await state.set_focus_mode(body.enabled)
        return {"enabled": state.focus_mode}

    @app.get("/sessions/{session_id}/messages")
    async def list_messages(session_id: int, limit: int = 100):
        require_session(session_id)
        return db.recent_messages(session_id, limit)

    @app.post("/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: int, body: PostMessage):
        session = require_session(session_id)
        character = get_character(session["character_id"])
        if character is None:
            raise HTTPException(404, "character not found")
        try:
            return await conversation.post_user_message(character, session_id, body.content)
        except SessionBusyError:
            raise HTTPException(409, "character is still replying in this session")

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        hub.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(ws)

    return app
