import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import engawa.ui.activity as activity_module
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.llm import MockEmbedder, MockLLMClient
from engawa.core.memory import ACTIVITY_TITLE_MAX
from engawa.ui.activity import ActivityWatcher, foreground_window, is_excluded
from test_memory import SUI, Env, FakeClock, at


async def observe(env: Env, app: str, title: str, minutes: float = 0.5) -> None:
    await env.memory.record_activity(app, title)
    env.clock.now += timedelta(minutes=minutes)


# --- 記録 ---


async def test_same_window_is_merged_into_one_activity(tmp_path):
    env = Env(tmp_path, at(12))
    for _ in range(5):
        await observe(env, "Code.exe", "engawa - Visual Studio Code")
    await observe(env, "chrome.exe", "猫の動画 - YouTube - Google Chrome")
    await observe(env, "Code.exe", "engawa - Visual Studio Code")
    rows = env.db.list_activity()
    assert [(r["app"], r["started_at"][11:16], r["ended_at"][11:16]) for r in rows] == [
        ("Code.exe", "12:00", "12:02"),
        ("chrome.exe", "12:02", "12:02"),
        ("Code.exe", "12:03", "12:03"),  # 別のウィンドウを挟んだら別の活動
    ]


async def test_long_gap_starts_a_new_activity(tmp_path):
    env = Env(tmp_path, at(12))
    await observe(env, "Code.exe", "engawa", minutes=5)  # PCから離れていた
    await observe(env, "Code.exe", "engawa")
    assert len(env.db.list_activity()) == 2


async def test_long_titles_are_truncated_and_still_merged(tmp_path):
    env = Env(tmp_path, at(12))
    title = "あ" * (ACTIVITY_TITLE_MAX + 50)
    await observe(env, "chrome.exe", title)
    await observe(env, "chrome.exe", title)
    [row] = env.db.list_activity()
    assert len(row["title"]) == ACTIVITY_TITLE_MAX


# --- 即興の話題の材料（仕様8-4） ---


async def test_impromptu_speech_gets_recent_activity_as_material(tmp_path):
    env = Env(tmp_path, at(11, 20))
    await observe(env, "Code.exe", "昔の作業", minutes=5)  # 30分より前のものは渡さない
    env.clock.now = at(11, 40)
    for _ in range(5):
        await observe(env, "chrome.exe", "猫の動画 - YouTube - Google Chrome")
    await observe(env, "explorer.exe", "ダウンロード")  # すぐ切り替えたものは渡さない

    await env.proactive.speak("sui", "暇つぶし")

    instruction = env.llm.chats[-1][-1]["content"]
    assert "[11:40〜11:42] chrome.exe — 猫の動画 - YouTube - Google Chrome" in instruction
    assert "ダウンロード" not in instruction and "昔の作業" not in instruction
    assert "見張っているような言い方" in instruction


async def test_seeded_speech_does_not_use_activity(tmp_path):
    env = Env(tmp_path, at(12))
    for _ in range(5):
        await observe(env, "chrome.exe", "猫の動画")
    env.db.add_seed("sui", "京都旅行の話", at(11).isoformat())
    await env.proactive.speak("sui", "暇つぶし")
    assert "猫の動画" not in env.llm.chats[-1][-1]["content"]


async def test_activity_is_not_a_memory_trigger_anymore(tmp_path):
    env = Env(tmp_path, at(12))
    for _ in range(5):
        await observe(env, "chrome.exe", "猫の動画", minutes=10)
    assert env.memory.unprocessed("sui") == []
    assert "画面" not in env.memory.build_adjust_messages(SUI, [], [])[0]["content"]


async def test_consolidate_deletes_old_activity(tmp_path):
    env = Env(tmp_path, at(12) - timedelta(days=2))
    await observe(env, "Code.exe", "古い作業")
    env.clock.now = at(23)
    await observe(env, "Code.exe", "今日の作業")
    result = await env.memory.consolidate("sui")
    assert result["expired_activity"] == 1
    assert [r["title"] for r in env.db.list_activity()] == ["今日の作業"]


def test_activity_api(tmp_path):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=FakeClock(at(12)), embedder=MockEmbedder())
    with TestClient(app) as client:
        assert client.post("/activity", json={"app": "Code.exe", "title": "engawa"}).status_code == 204
        assert client.post("/activity", json={"app": "", "title": "x"}).status_code == 422
        assert [a["title"] for a in app.state.services["db"].list_activity()] == ["engawa"]


# --- UI 側の観測 ---


def test_is_excluded():
    exclude = ["KeePass", "パスワード", "InPrivate"]
    assert is_excluded("KeePass.exe", "Database", exclude)
    assert is_excluded("msedge.exe", "新しいタブ - [InPrivate] - Microsoft Edge", exclude)
    assert is_excluded("chrome.exe", "パスワードの変更", exclude)
    assert not is_excluded("Code.exe", "engawa", exclude)
    assert not is_excluded("Code.exe", "engawa", [""])


class FakeClient:
    def __init__(self):
        self.posts = []

    def post(self, path, body, on_success=None, on_error=None):
        self.posts.append((path, body))


def test_watcher_respects_switches_and_exclusions(qt_app, monkeypatch):
    client = FakeClient()
    watcher = ActivityWatcher(client, 30, ["KeePass"])
    window = ("Code.exe", "engawa", 1)
    monkeypatch.setattr(activity_module, "foreground_window", lambda: window)

    watcher.observe()
    assert client.posts == [("/activity", {"app": "Code.exe", "title": "engawa"})]

    watcher.enabled = False
    watcher.observe()
    watcher.enabled, watcher.focus_mode = True, True
    watcher.observe()
    watcher.focus_mode = False
    window = ("KeePass.exe", "Database", 1)
    watcher.observe()
    window = ("python.exe", "縁側", watcher._own_pid)  # 縁側自身
    watcher.observe()
    assert len(client.posts) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows のみ")
def test_foreground_window_on_windows():
    result = foreground_window()
    assert result is None or (isinstance(result[0], str) and isinstance(result[2], int))
