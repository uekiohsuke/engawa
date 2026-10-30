from fastapi.testclient import TestClient

from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.db import Database
from engawa.core.llm import MockEmbedder, MockLLMClient
from tests.test_core import FakeClock
from tests.test_memory import at


def make_client(tmp_path, clock):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=clock, embedder=MockEmbedder())
    return TestClient(app)


def main_id(client) -> int:
    return next(s["id"] for s in client.get("/characters/sui/sessions").json() if s["kind"] == "main")


def say(client, content: str) -> None:
    with client.websocket_connect("/ws") as ws:
        client.post(f"/sessions/{main_id(client)}/messages", json={"content": content})
        while ws.receive_json()["type"] != "message.completed":
            pass


def messages(client) -> list[str]:
    return [m["content"] for m in client.get(f"/sessions/{main_id(client)}/messages").json()]


def test_reset_backs_up_and_restore_rolls_back(tmp_path):
    clock = FakeClock(at(12))
    with make_client(tmp_path, clock) as client:
        say(client, "カレー作った")
        client.post("/characters/sui/memory/consolidate")
        assert client.get("/characters/sui/memory").json()["profiles"]["user"]

        with client.websocket_connect("/ws") as ws:
            reset = client.post("/data/reset").json()
            assert ws.receive_json()["type"] in ("state.updated", "data.replaced")
        assert reset["backup"]["reason"] == "reset" and reset["backup"]["reason_label"] == "リセット前"
        assert messages(client) == []
        memory = client.get("/characters/sui/memory").json()
        assert memory["profiles"] == {"user": None, "self": None} and memory["episodes"] == []
        assert {s["kind"] for s in client.get("/characters/sui/sessions").json()} == {"dialogue", "main"}

        # リセット前のバックアップの中身を見る
        preview = client.get(f"/backups/{reset['backup']['name']}").json()
        sui = preview["characters"]["sui"]
        assert sui["messages"] == 2 and sui["profiles"]["user"] and sui["episode_count"] == 1
        assert sui["episodes"][0]["content"]

        say(client, "リセット後の話")
        restored = client.post(f"/backups/{reset['backup']['name']}/restore").json()
        assert restored["backup"]["reason"] == "restore"
        assert messages(client)[0] == "カレー作った"
        assert client.get("/characters/sui/memory").json()["profiles"]["user"]

        # 戻したことも、復元前のバックアップで取り消せる
        client.post(f"/backups/{restored['backup']['name']}/restore")
        assert messages(client)[0] == "リセット後の話"
        assert [b["reason"] for b in client.get("/backups").json()].count("restore") == 2


def test_restore_resumes_state_without_catching_up(tmp_path):
    clock = FakeClock(at(12))
    with make_client(tmp_path, clock) as client:
        client.put("/focus", json={"enabled": True})
        backup = client.post("/backups").json()
        assert backup["reason"] == "manual"
        clock.advance(hours=14)  # バックアップから復元までに夜が来ている
        client.put("/focus", json={"enabled": False})
        client.post(f"/backups/{backup['name']}/restore")
        state = client.get("/characters/sui/state").json()
        assert not state["asleep"]  # バックアップした時点の状態から続ける
        assert state["focus_mode"] is False  # 集中モードは今のものを引き継ぐ


def test_backup_names_and_busy_guard(tmp_path):
    with make_client(tmp_path, FakeClock(at(12))) as client:
        assert client.get("/backups/..%2Fengawa.db").status_code == 404
        assert client.get("/backups/nothing.db").status_code == 404
        name = client.post("/backups").json()["name"]
        memory = client.app.state.services["memory"]
        memory._consolidating.add("sui")
        assert client.post("/data/reset").status_code == 409
        memory._consolidating.discard("sui")
        assert client.delete(f"/backups/{name}").status_code == 204
        assert client.get("/backups").json() == []


def test_restore_upgrades_old_backup(tmp_path):
    old = tmp_path / "old.db"
    db = Database(old)
    db.ensure_default_sessions("sui")
    db._conn.execute("CREATE TABLE stm (id INTEGER)")  # 記憶v1の名残
    db._conn.commit()
    db.close()
    live = Database(tmp_path / "live.db")
    live.restore_from(old)
    assert {s["kind"] for s in live.list_sessions("sui")} == {"dialogue", "main"}
    assert "stm" not in {r[0] for r in live._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
