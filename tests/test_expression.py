import sqlite3
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Signal

from engawa.characters import get_character
from engawa.core.db import Database
from engawa.core.expression import ExpressionFilter, expression_section
from engawa.core.timewords import describe_elapsed, describe_now, message_stamp, season, time_of_day
from engawa.ui.dialogue_window import DialogueWindow
from test_core import NOON, make_client, receive_until, session_ids
from test_images import SUI
from test_memory import Env, at

EXPRESSIONS = ("normal", "think", "joy", "blush")


def run_filter(chunks, expressions=EXPRESSIONS):
    tags = ExpressionFilter(expressions)
    text = "".join(tags.feed(c) for c in chunks) + tags.flush()
    return tags.expression, text


# --- 表情タグ ---


def test_leading_tag_is_removed_even_when_split_across_chunks():
    assert run_filter(["[j", "oy", "] ", "そっか、", "良かったじゃん。"]) == ("joy", "そっか、良かったじゃん。")
    assert run_filter(["[Blush]\n", "べ、別に。"]) == ("blush", "べ、別に。")


def test_text_without_tag_passes_through():
    assert run_filter(["ふーん", "。"]) == (None, "ふーん。")


def test_unknown_brackets_are_kept_as_text():
    assert run_filter(["[angry] 配列の", "a[0] を見て"]) == (None, "[angry] 配列のa[0] を見て")
    assert run_filter(["[", "これはタグじゃない長い括弧書き]です"]) == (None, "[これはタグじゃない長い括弧書き]です")
    assert run_filter(["最後が[jo"]) == (None, "最後が[jo")  # 閉じないまま終わった


def test_first_tag_wins_and_later_tags_are_removed():
    assert run_filter(["[think] うーん。", "[joy] まあいいか。"]) == ("think", "うーん。まあいいか。")


def test_expression_section_lists_only_registered_expressions():
    section = expression_section(["normal", "joy"])
    assert "[joy]：嬉しい" in section and "[blush]" not in section
    assert expression_section(["normal"]) is None


# --- 生成 ---


def test_generation_announces_expression_before_text(tmp_path):
    with make_client(tmp_path) as client:
        main_id = session_ids(client)["main"]
        with client.websocket_connect("/ws") as ws:
            client.post(f"/sessions/{main_id}/messages", json={"content": "こんにちは"})
            events = receive_until(ws, "message.completed", "error")
        types = [e["type"] for e in events if e["type"] in ("expression.changed", "message.delta")]
        assert types[0] == "expression.changed"
        expression = next(e for e in events if e["type"] == "expression.changed")["expression"]
        assert expression in EXPRESSIONS
        deltas = "".join(e["delta"] for e in events if e["type"] == "message.delta")
        completed = events[-1]["message"]
        assert "[" not in deltas and deltas.startswith("(mock)")
        assert completed["expression"] == expression and completed["content"] == deltas
        # モックは発言の時刻を引用しない
        assert "（" not in completed["content"].split("「")[0]


def test_old_database_gets_expression_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,"
        " role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (1, 'character', '前から', 'x')")
    conn.commit()
    conn.close()
    db = Database(path)
    [old] = db.recent_messages(1, 10)
    assert old["expression"] is None
    assert db.add_message(1, "character", "新しい", "y", "joy")["expression"] == "joy"


# --- 時刻 ---


def test_time_words():
    assert [time_of_day(NOON.replace(hour=h)) for h in (0, 5, 8, 11, 13, 15, 18, 21, 23)] == [
        "深夜", "明け方", "朝", "昼前", "昼", "午後", "夕方", "夜", "深夜",
    ]
    assert [season(NOON.replace(month=m)) for m in (1, 4, 7, 10, 12)] == ["冬", "春", "夏", "秋", "冬"]
    assert describe_now(NOON) == "9月28日（月） 12:00（昼・秋）"
    assert message_stamp(NOON.isoformat()) == "（9/28 12:00）"
    assert [describe_elapsed(timedelta(minutes=m)) for m in (0.2, 45, 185, 60 * 50)] == ["約1分", "約45分", "約3時間", "約2日"]


def system_prompt_after_gap(env: Env, gap: timedelta, instruction: str | None = None) -> str:
    sid = env.sessions["main"]
    env.db.add_message(sid, "character", "またね", (env.clock.now - gap).isoformat())
    if instruction is None:
        env.db.add_message(sid, "user", "ただいま", env.clock.now.isoformat())
    return env.conversation.build_messages(get_character("sui"), sid, instruction)[0]["content"]


def test_reply_after_long_gap_mentions_elapsed_time(tmp_path):
    assert "前回のやりとりから約3時間ぶり" in system_prompt_after_gap(Env(tmp_path / "a", at(12)), timedelta(hours=3))
    assert "ぶり" not in system_prompt_after_gap(Env(tmp_path / "b", at(12)), timedelta(minutes=5))


def test_proactive_after_long_gap_mentions_elapsed_time(tmp_path):
    prompt = system_prompt_after_gap(Env(tmp_path, at(12)), timedelta(days=2), instruction="話しかけて")
    assert "前回のやりとりから約2日ぶり" in prompt


# --- 対話ウィンドウ ---


class FakeClient(QObject):
    event_received = Signal(dict)

    def get(self, path, on_success=None, on_error=None):
        pass


class FakeSpeaker(QObject):
    speaking_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.enabled = True
        self.busy = False

    def is_busy(self):
        return self.busy

    def stop(self):
        pass

    def begin_stream(self):
        pass

    def feed(self, delta):
        pass

    def end_stream(self):
        pass


def test_dialogue_window_switches_and_reverts_expression(qt_app):
    client, speaker = FakeClient(), FakeSpeaker()
    window = DialogueWindow(client, SUI, session_id=1, speaker=speaker)
    window.show()
    normal = window._standing.pixmap().cacheKey()

    client.event_received.emit({"type": "generation.started", "session_id": 1})
    client.event_received.emit({"type": "expression.changed", "session_id": 1, "expression": "think"})
    client.event_received.emit({"type": "expression.changed", "session_id": 2, "expression": "joy"})  # 別の会話
    assert window._expression == "think" and window._standing.pixmap().cacheKey() != normal
    message = {"id": 1, "session_id": 1, "role": "character", "content": "うーん。", "expression": "think"}
    client.event_received.emit({"type": "message.completed", "message": message})
    assert window._revert_timer.isActive()

    speaker.busy = True  # まだ読み上げている間は戻さない
    window._revert_timer.timeout.emit()
    assert window._expression == "think" and window._revert_timer.isActive()
    speaker.busy = False
    window._revert_timer.timeout.emit()
    assert window._expression == "normal" and window._standing.pixmap().cacheKey() == normal
    window.close()
