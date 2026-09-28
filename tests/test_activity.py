import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import engawa.ui.activity as activity_module
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.llm import ADJUST_PREFIX, MockEmbedder, MockLLMClient
from engawa.core.memory import ACTIVITY_TITLE_MAX
from engawa.core.proactive import gate
from engawa.core.state import CharacterState
from engawa.ui.activity import ActivityWatcher, foreground_window, is_excluded
from test_memory import SUI, Env, FakeClock, RecordingLLM, at


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


# --- 記憶調整への組み込み ---


async def test_adjust_uses_activity_and_advances_cursor(tmp_path):
    llm = RecordingLLM(adjust={"seeds": ["さっき見てた猫の動画のこと"], "drop_seeds": [], "self_memories": []})
    env = Env(tmp_path, at(12), llm=llm)
    for _ in range(5):
        await observe(env, "chrome.exe", "猫の動画 - YouTube - Google Chrome")
    await observe(env, "explorer.exe", "ダウンロード")  # すぐ切り替えたものは渡さない
    assert env.memory.unprocessed_activity_minutes("sui") == pytest.approx(2.0)

    result = await env.memory.adjust("sui")
    prompt = next(p for p in llm.prompts if p.startswith(ADJUST_PREFIX))
    assert "[9/28 12:00〜12:02] chrome.exe — 猫の動画 - YouTube - Google Chrome" in prompt
    assert "ダウンロード" not in prompt
    assert "見張っているような話題にしないこと" in prompt
    assert result["processed_activity"] == 2
    assert env.memory.unprocessed_activity("sui") == []
    assert [s["content"] for s in env.db.list_seeds("sui")] == ["さっき見てた猫の動画のこと"]


def test_gate_triggers_memory_adjustment_on_accumulated_activity():
    kwargs = dict(
        state=CharacterState(),
        availability="both",
        personality=SUI.personality,
        crossings=(),
        since_conversation=timedelta(hours=2),
        since_proactive=None,
        unanswered=0,
    )
    assert gate(**kwargs, unprocessed_activity_minutes=30).triggers == {"memory": 0.5}
    assert gate(**kwargs, unprocessed_activity_minutes=29).triggers == {}


async def test_distill_deletes_expired_activity(tmp_path):
    env = Env(tmp_path, at(12) - timedelta(days=4))
    await observe(env, "Code.exe", "古い作業")
    env.clock.now = at(23)
    await observe(env, "Code.exe", "今日の作業")
    result = await env.memory.distill("sui")
    assert result["expired_activity"] == 1
    assert [r["title"] for r in env.db.list_activity()] == ["今日の作業"]


def test_activity_api(tmp_path):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=FakeClock(at(12)), embedder=MockEmbedder())
    with TestClient(app) as client:
        assert client.post("/activity", json={"app": "Code.exe", "title": "engawa"}).status_code == 204
        assert client.post("/activity", json={"app": "", "title": "x"}).status_code == 422
        assert client.get("/characters/sui/memory").json()["unprocessed_activity"] == 1


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
