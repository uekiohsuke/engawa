from fastapi.testclient import TestClient

from engawa.characters import get_character
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.conversation import ConversationService
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import MockLLMClient


def make_client(tmp_path, history_window=20):
    settings = Settings(data_dir=tmp_path, history_window=history_window)
    app = create_app(settings, llm=MockLLMClient(delay=0))
    return TestClient(app)


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
    service = ConversationService(db, MockLLMClient(delay=0), EventHub(), history_window=3)
    messages = service.build_messages(get_character("sui"), sid)
    assert messages[0]["role"] == "system"
    assert "翠" in messages[0]["content"]
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
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] in ("message.completed", "error"):
                    break
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
            while ws.receive_json()["type"] != "message.completed":
                pass
        assert len(client.get(f"/sessions/{ids['dialogue']}/messages").json()) == 2
        assert client.get(f"/sessions/{ids['main']}/messages").json() == []


def test_history_persists_across_restart(tmp_path):
    with make_client(tmp_path) as client:
        main_id = session_ids(client)["main"]
        with client.websocket_connect("/ws") as ws:
            client.post(f"/sessions/{main_id}/messages", json={"content": "覚えてる？"})
            while ws.receive_json()["type"] != "message.completed":
                pass
    with make_client(tmp_path) as client:
        assert session_ids(client)["main"] == main_id
        assert len(client.get(f"/sessions/{main_id}/messages").json()) == 2


def test_errors(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/sessions/999/messages").status_code == 404
        assert client.get("/characters/nobody/sessions").status_code == 404
        main_id = session_ids(client)["main"]
        assert client.post(f"/sessions/{main_id}/messages", json={"content": ""}).status_code == 422
        health = client.get("/health").json()
        assert health == {"status": "ok", "llm": "mock", "llm_reachable": True}
