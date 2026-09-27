"""FastAPI アプリ本体。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from engawa.characters import CHARACTERS, get_character
from engawa.config import Settings, load_settings
from engawa.core.conversation import ConversationService, SessionBusyError
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import LLMClient, create_llm


class PostMessage(BaseModel):
    content: str = Field(min_length=1)


def create_app(settings: Settings | None = None, llm: LLMClient | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    llm = llm or create_llm(settings)
    db = db or Database(settings.db_path)
    hub = EventHub()
    conversation = ConversationService(db, llm, hub, settings.history_window)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for character_id in CHARACTERS:
            db.ensure_default_sessions(character_id)
        yield
        db.close()

    app = FastAPI(title="Engawa Core", lifespan=lifespan)

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
