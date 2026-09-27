from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from engawa.characters import CHARACTERS, get_character
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.conversation import ConversationService
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import MockLLMClient
from engawa.core.state_service import StateService

NOON = datetime(2026, 9, 28, 12, 0).astimezone()


class FakeClock:
    def __init__(self, now: datetime = NOON):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_client(tmp_path, history_window=20, clock=None):
    settings = Settings(data_dir=tmp_path, history_window=history_window)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=clock or FakeClock())
    return TestClient(app)


def receive_until(ws, *types):
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in types:
            return events


def session_ids(client):
    sessions = client.get("/characters/sui/sessions").json()
    return {s["kind"]: s["id"] for s in sessions}


def test_default_sessions_created_once(tmp_path):
    db = Database(tmp_path / "t.db")
    db.ensure_default_sessions("sui")
    db.ensure_default_sessions("sui")
    kinds = sorted(s["kind"] for s in db.list_sessions("sui"))
    assert kinds == ["dialogue", "main"]


def test_recent_messages_window_and_order(tmp_path):
    db = Database(tmp_path / "t.db")
    db.ensure_default_sessions("sui")
    sid = db.list_sessions("sui")[0]["id"]
    for i in range(5):
        db.add_message(sid, "user", f"m{i}")
    assert [m["content"] for m in db.recent_messages(sid, 3)] == ["m2", "m3", "m4"]


def test_build_messages_uses_persona_and_window(tmp_path):
    db = Database(tmp_path / "t.db")
    db.ensure_default_sessions("sui")
    sid = db.list_sessions("sui")[0]["id"]
    for i in range(4):
        db.add_message(sid, "user", f"u{i}")
        db.add_message(sid, "character", f"c{i}")
    hub = EventHub()
    state = StateService(db, hub, CHARACTERS.values(), FakeClock())
    service = ConversationService(db, MockLLMClient(delay=0), hub, state, history_window=3)
    messages = service.build_messages(get_character("sui"), sid)
    assert messages[0]["role"] == "system"
    assert "翠" in messages[0]["content"]
    assert "## 現在の状態" in messages[0]["content"]
    assert "9月28日" in messages[0]["content"]
    assert [(m["role"], m["content"]) for m in messages[1:]] == [
        ("assistant", "c2"),
        ("user", "u3"),
        ("assistant", "c3"),
    ]


def test_post_message_streams_reply_over_websocket(tmp_path):
    with make_client(tmp_path) as client:
        main_id = session_ids(client)["main"]
        with client.websocket_connect("/ws") as ws:
            resp = client.post(f"/sessions/{main_id}/messages", json={"content": "こんにちは"})
            assert resp.status_code == 202
            events = receive_until(ws, "message.completed", "error")
        # 会話による状態変化の配信は、生成イベントとは別に扱う
        events = [e for e in events if e["type"] != "state.updated"]
        types = [e["type"] for e in events]
        assert types[0] == "message.created"
        assert types[1] == "generation.started"
        assert "message.delta" in types
        completed = events[-1]["message"]
        assert completed["role"] == "character"
        assert "こんにちは" in completed["content"]
        streamed = "".join(e["delta"] for e in events if e["type"] == "message.delta")
        assert streamed.strip() == completed["content"]

        history = client.get(f"/sessions/{main_id}/messages").json()
        assert [m["role"] for m in history] == ["user", "character"]


def test_sessions_have_separate_histories(tmp_path):
    with make_client(tmp_path) as client:
        ids = session_ids(client)
        with client.websocket_connect("/ws") as ws:
            client.post(f"/sessions/{ids['dialogue']}/messages", json={"content": "対話です"})
            receive_until(ws, "message.completed")
        assert len(client.get(f"/sessions/{ids['dialogue']}/messages").json()) == 2
        assert client.get(f"/sessions/{ids['main']}/messages").json() == []


def test_history_persists_across_restart(tmp_path):
    with make_client(tmp_path) as client:
        main_id = session_ids(client)["main"]
        with client.websocket_connect("/ws") as ws:
            client.post(f"/sessions/{main_id}/messages", json={"content": "覚えてる？"})
            receive_until(ws, "message.completed")
    with make_client(tmp_path) as client:
        assert session_ids(client)["main"] == main_id
        assert len(client.get(f"/sessions/{main_id}/messages").json()) == 2


def test_errors(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/sessions/999/messages").status_code == 404
        assert client.get("/characters/nobody/sessions").status_code == 404
        main_id = session_ids(client)["main"]
        assert client.post(f"/sessions/{main_id}/messages", json={"content": ""}).status_code == 422
        assert client.get("/characters/nobody/state").status_code == 404
        health = client.get("/health").json()
        assert health == {"status": "ok", "llm": "mock", "llm_reachable": True}


def test_user_message_lowers_boredom(tmp_path):
    clock = FakeClock()
    with make_client(tmp_path, clock=clock) as client:
        clock.advance(hours=2)
        before = client.get("/characters/sui/state").json()["values"]["boredom"]
        main_id = session_ids(client)["main"]
        with client.websocket_connect("/ws") as ws:
            client.post(f"/sessions/{main_id}/messages", json={"content": "暇？"})
            receive_until(ws, "message.completed")
        after = client.get("/characters/sui/state").json()["values"]["boredom"]
        assert after < before


def test_asleep_character_does_not_reply(tmp_path):
    clock = FakeClock(datetime(2026, 9, 28, 23, 0).astimezone())
    with make_client(tmp_path, clock=clock) as client:
        clock.advance(hours=3)  # 翌2時：就寝時刻（25時）を過ぎている
        main_id = session_ids(client)["main"]
        with client.websocket_connect("/ws") as ws:
            assert client.post(f"/sessions/{main_id}/messages", json={"content": "起きてる？"}).status_code == 202
            events = receive_until(ws, "message.created")  # ユーザー発言
            events += receive_until(ws, "message.created")  # 睡眠中の定型文
        notice = events[-1]["message"]
        assert notice["role"] == "system"
        assert "眠っている" in notice["content"]
        assert "generation.started" not in [e["type"] for e in events]
        state = client.get("/characters/sui/state").json()
        assert state["asleep"] is True
        assert state["availability"] == "sleeping"
        assert any(e["kind"] == "sleep" for e in state["events"])
        # 睡眠中でも続けて投稿できる（生成中扱いにならない）
        assert client.post(f"/sessions/{main_id}/messages", json={"content": "おーい"}).status_code == 202
