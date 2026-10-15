import sqlite3
from datetime import timedelta

from fastapi.testclient import TestClient

from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.db import Database
from engawa.core.llm import MockEmbedder, MockLLMClient
from test_memory import SUI, Env, FakeClock, at


def subs(env: Env) -> list[dict]:
    return [s for s in env.db.list_sessions("sui") if s["kind"] == "sub"]


# --- 送り先 ---


async def test_seeded_message_goes_to_main(tmp_path):
    env = Env(tmp_path, at(15))  # 取り込み中 → メッセージ
    env.db.add_seed("sui", "京都旅行の話", at(14).isoformat())
    await env.proactive.speak("sui", "暇つぶし")
    await env.settle()
    assert len(env.db.recent_messages(env.sessions["main"], 10)) == 1
    assert subs(env) == []


async def test_seeded_dialogue_falls_back_to_main(tmp_path):
    env = Env(tmp_path, at(12))
    env.proactive._dialogue_timeout = 0.01
    env.db.add_seed("sui", "京都旅行の話", at(11).isoformat())
    await env.proactive.speak("sui", "暇つぶし")
    await env.settle()
    assert len(env.db.recent_messages(env.sessions["main"], 10)) == 1
    assert subs(env) == []


# --- サブスレッドと記憶 ---


async def test_sub_session_searches_episodes_more_strictly(tmp_path):
    env = Env(tmp_path, at(12))
    await env.add_episode("昨日マスターがカレーを作ったけど辛すぎたらしい", happened=at(20, day=27))
    env.db.save_profile("sui", "user", "マスターはカレーが好き。", at(1).isoformat())
    sub = await env.conversation.create_sub_session("sui", "料理")

    async def recalled(session_id: int, text: str) -> bool:
        await env.conversation.post_user_message(SUI, session_id, text)
        await env.settle()
        return "## 思い出したこと" in env.llm.chats[-1][0]["content"]

    # 少し関係する程度（類似度0.62）なら、メインでは思い出すが、サブでは出さない
    assert not await recalled(sub["id"], "カレー作ったけど辛すぎたかも")
    assert "マスターはカレーが好き。" in env.llm.chats[-1][0]["content"]  # 印象層はどこでも入る
    assert await recalled(env.sessions["main"], "カレー作ったけど辛すぎたかも")
    # はっきり関係する（類似度0.67）なら、サブでも思い出す
    assert await recalled(sub["id"], "昨日カレーを作ったけど辛すぎたんだよね")


async def test_sub_session_conversation_becomes_episodes(tmp_path):
    env = Env(tmp_path, at(12))
    sub = await env.conversation.create_sub_session("sui", "料理")
    await env.conversation.post_user_message(SUI, sub["id"], "カレーの話")
    await env.settle()
    await env.memory.adjust("sui")
    assert [e["session_id"] for e in env.episodes()] == [sub["id"]]
    assert "サブチャンネル「料理」" in env.llm.prompts[-1]


# --- アーカイブ ---


async def test_stale_sub_sessions_are_archived_and_revived_by_posting(tmp_path):
    env = Env(tmp_path, at(12))
    old = await env.conversation.create_sub_session("sui", "古い話題")
    fresh = await env.conversation.create_sub_session("sui", "新しい話題")
    env.clock.now = at(12) + timedelta(days=3, minutes=1)
    await env.conversation.post_user_message(SUI, fresh["id"], "まだ話してる")
    await env.settle()

    # 3日分の時間経過の途中で就寝しているので、就寝時の整理で既にアーカイブされていることもある
    await env.conversation.archive_stale_sessions("sui")
    by_id = {s["id"]: s for s in subs(env)}
    assert by_id[old["id"]]["archived"] == 1 and by_id[fresh["id"]]["archived"] == 0
    assert all(s["archived"] == 0 for s in env.db.list_sessions("sui") if s["kind"] != "sub")

    await env.conversation.post_user_message(SUI, old["id"], "やっぱりこの話")
    await env.settle()
    assert env.db.get_session(old["id"])["archived"] == 0


# --- API・DB ---


def test_sub_session_api(tmp_path):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=FakeClock(at(12)), embedder=MockEmbedder())
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            created = client.post("/characters/sui/sessions", json={"title": "  京都旅行  "})
            assert created.status_code == 201
            session = created.json()
            assert (session["kind"], session["title"], session["archived"]) == ("sub", "京都旅行", 0)
            assert ws.receive_json()["type"] == "session.created"

            patched = client.patch(f"/sessions/{session['id']}", json={"archived": True, "title": "旅行"}).json()
            assert (patched["title"], patched["archived"]) == ("旅行", 1)
            assert ws.receive_json()["type"] == "session.updated"

        kinds = {s["kind"] for s in client.get("/characters/sui/sessions").json()}
        assert kinds == {"dialogue", "main", "sub"}
        main_id = next(s["id"] for s in client.get("/characters/sui/sessions").json() if s["kind"] == "main")
        assert client.patch(f"/sessions/{main_id}", json={"archived": True}).status_code == 400
        assert client.post("/characters/sui/sessions", json={"title": ""}).status_code == 422
        assert client.post("/characters/nobody/sessions", json={"title": "x"}).status_code == 404


def test_migrates_old_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, character_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO sessions (character_id, kind, title, created_at) VALUES ('sui', 'main', 'main', 'x')")
    conn.commit()
    conn.close()
    db = Database(path)
    [session] = db.list_sessions("sui")
    assert session["archived"] == 0 and session["last_message_at"] is None
