import asyncio
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

import engawa.core.memory as memory_module
from engawa.characters import CHARACTERS, get_character
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.conversation import ConversationService
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import ADJUST_PREFIX, PROFILE_PREFIX, MockEmbedder, MockLLMClient
from engawa.core.memory import (
    GRACE_EXTEND,
    GRACE_MAX,
    GRACE_MIN,
    LIFETIME,
    LIFETIME_EXTEND,
    MemoryService,
    initial_grace,
    pack,
    prune_weight,
)
from engawa.core.proactive import ProactiveService, gate
from engawa.core.state import CharacterState
from engawa.core.state_service import StateService

SUI = get_character("sui")


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute).astimezone()


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingLLM(MockLLMClient):
    """生成に渡されたメッセージと、JSON処理のプロンプトを記録する。

    adjust・profile・judge には固定の応答か、プロンプトを受け取って応答を返す関数を渡せる。
    """

    def __init__(self, adjust=None, profile=None, judge=None):
        super().__init__(delay=0)
        self.chats: list[list[dict]] = []
        self.prompts: list[str] = []
        self._adjust, self._profile, self._judge = adjust, profile, judge

    async def stream_chat(self, messages):
        self.chats.append(messages)
        async for d in super().stream_chat(messages):
            yield d

    async def complete_json(self, messages):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        for prefix, answer in ((ADJUST_PREFIX, self._adjust), (PROFILE_PREFIX, self._profile)):
            if prompt.startswith(prefix):
                if answer is None:
                    break
                return answer(prompt) if callable(answer) else answer
        else:
            if self._judge is not None:
                return self._judge
        return await super().complete_json(messages)


class Env:
    def __init__(
        self,
        tmp_path,
        now: datetime,
        llm: RecordingLLM | None = None,
        judge=None,
        history_window=20,
        memory_rand=lambda: 0.99,  # 既定では「種があれば使う」側に固定
    ):
        self.clock = FakeClock(now)
        self.db = Database(tmp_path / "t.db")
        self.db.ensure_default_sessions("sui")
        self.db.save_state("sui", CharacterState(updated_at=now.isoformat()).to_dict())
        self.hub = EventHub()
        self.llm = llm or RecordingLLM()
        self.state = StateService(self.db, self.hub, CHARACTERS.values(), self.clock)
        self.memory = MemoryService(
            self.db, self.hub, self.state, self.llm, MockEmbedder(), CHARACTERS.values(), memory_rand
        )
        self.conversation = ConversationService(self.db, self.llm, self.hub, self.state, history_window, self.memory)
        self.proactive = ProactiveService(
            self.db,
            self.hub,
            self.state,
            self.conversation,
            judge or RecordingLLM(),
            CHARACTERS.values(),
            interval_seconds=0,
            dialogue_timeout_seconds=60,
            rand=lambda: 0.0,
            memory=self.memory,
        )
        self.sessions = {s["kind"]: s["id"] for s in self.db.list_sessions("sui")}

    async def say(self, content: str, kind: str = "main") -> None:
        await self.conversation.post_user_message(SUI, self.sessions[kind], content)
        await self.settle()

    async def settle(self) -> None:
        for _ in range(300):
            if not self.conversation._tasks and not self.proactive._tasks and not self.memory._tasks:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError

    async def add_episode(self, content: str, happened: datetime | None = None, **overrides) -> int:
        """検索できるエピソードを直接作る。"""
        [vector] = await MockEmbedder().embed([content])
        now = self.clock.now
        fields = dict(
            session_id=None,
            first_message_id=None,
            last_message_id=None,
            happened_at=(happened or now).isoformat(),
            created_at=(happened or now).isoformat(),
            grace_until=(now + GRACE_MIN).isoformat(),
            expires_at=(now + LIFETIME).isoformat(),
            embedding=pack(vector),
        )
        fields.update(overrides)
        return self.db.add_episode("sui", "user", content, **fields)

    def episodes(self) -> list[dict]:
        return self.db.list_episodes("sui")


def episode_of(block: int, first: int, last: int, content: str, master=None, own=None) -> dict:
    """記憶調整の出力のエピソード。block はプロンプトの「会話n」の番号。"""
    return {"session": block, "from": first, "to": last, "content": content, "about_master": master, "about_self": own}


# --- 短期記憶とエピソード化 ---


async def test_messages_stay_short_term_until_episodized(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("昨日カレーを作ったよ")
    assert [m["role"] for m in env.memory.unprocessed("sui")] == ["user", "character"]

    result = await env.memory.adjust("sui")

    assert (result["episodes"], result["processed"]) == (1, 2)
    assert env.memory.unprocessed("sui") == []
    [episode] = env.episodes()
    assert episode["session_id"] == env.sessions["main"] and episode["embedding"] is not None
    assert [m["subject"] for m in env.db.list_memos("sui")] == ["user"]  # モックは about_master だけ返す
    # 画面の履歴（会話ログ）は消えない
    assert len(env.db.recent_messages(env.sessions["main"], 10)) == 2
    prompt = next(p for p in env.llm.prompts if p.startswith(ADJUST_PREFIX))
    assert f"{SUI.user_name}：昨日カレーを作ったよ" in prompt and "（まだ続いているかもしれない）" in prompt


async def test_ongoing_conversation_is_not_episodized(tmp_path):
    env = Env(tmp_path, at(12))
    for text in ["カレー作ったよ", "それでさ、明日は"]:
        await env.say(text)
    ids = [m["id"] for m in env.memory.unprocessed("sui")]
    env.llm._adjust = {
        "episodes": [episode_of(1, ids[0], ids[1], "マスターがカレーを作った", master="料理をする人")],
        "ongoing": [{"session": 1, "from": ids[2]}],
    }

    result = await env.memory.adjust("sui")

    assert [m["id"] for m in env.memory.unprocessed("sui")] == ids[2:]  # 続いている部分は短期記憶のまま
    assert [e["content"] for e in env.episodes()] == ["マスターがカレーを作った"]
    assert (result["processed"], result["ongoing"]) == (2, 2)


async def test_gap_splits_session_and_skipped_block_stays_short_term(tmp_path):
    env = Env(tmp_path, at(16))
    await env.say("今日バイトで新人に教えた", kind="dialogue")
    old_ids = [m["id"] for m in env.memory.unprocessed("sui")]
    env.clock.now = at(21)  # 5時間後、同じ対話で別の話
    await env.say("明日の朝ごはん何にしよう", kind="dialogue")
    new_ids = [m["id"] for m in env.memory.unprocessed("sui")][2:]
    # 終わったブロック（会話1）をエピソードにし忘れ、続いているブロック（会話2）だけ保留にした
    env.llm._adjust = {"episodes": [], "ongoing": [{"session": 2, "from": new_ids[0]}]}

    await env.memory.adjust("sui")

    prompt = env.llm.prompts[-1]
    assert "### 会話1：対話　最後の発言から約5時間（終わった会話）" in prompt
    assert "### 会話2：対話　最後の発言から約1分（まだ続いているかもしれない）" in prompt
    assert [m["id"] for m in env.memory.unprocessed("sui")] == old_ids + new_ids  # 取りこぼしは消えない

    env.llm._adjust = {"episodes": [episode_of(1, old_ids[0], old_ids[1], "バイトで新人に教えた")], "ongoing": []}
    await env.memory.adjust("sui")
    assert [m["id"] for m in env.memory.unprocessed("sui")] == new_ids


async def test_finish_all_ignores_ongoing(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("カレー作ったよ")
    ids = [m["id"] for m in env.memory.unprocessed("sui")]
    env.llm._adjust = {
        "episodes": [episode_of(1, ids[0], ids[1], "カレーの話")],
        "ongoing": [{"session": 1, "from": ids[0]}],
    }
    await env.memory.adjust("sui", finish_all=True)
    assert env.memory.unprocessed("sui") == []
    assert "（終わった会話）" in env.llm.prompts[-1]


async def test_old_conversation_is_treated_as_finished(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("カレー作ったよ")
    env.clock.now = at(16)  # 途切れてから時間がたった
    ids = [m["id"] for m in env.memory.unprocessed("sui")]
    env.llm._adjust = {
        "episodes": [episode_of(1, ids[0], ids[1], "カレーの話")],
        "ongoing": [{"session": 1, "from": ids[1]}],
    }
    await env.memory.adjust("sui")
    assert "（終わった会話）" in env.llm.prompts[-1]
    assert env.memory.unprocessed("sui") == []


async def test_invalid_output_keeps_short_term_memory(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("カレー作ったよ")
    ids = [m["id"] for m in env.memory.unprocessed("sui")]
    env.llm._adjust = {"episodes": [episode_of(9999, ids[0], ids[1], "存在しない会話"), {"session": "x"}, "壊れた"]}
    result = await env.memory.adjust("sui")
    assert (result["episodes"], result["processed"]) == (0, 0)
    assert len(env.memory.unprocessed("sui")) == 2  # 次の機会にやり直す


def test_initial_grace_grows_with_length():
    assert initial_grace("") == GRACE_MIN  # 最低値
    assert initial_grace("あ" * 100) == GRACE_MIN + memory_module.GRACE_PER_100_CHARS
    assert initial_grace("あ" * 5000) == GRACE_MAX


# --- エピソードの検索 ---


async def test_related_episode_is_recalled_and_extended(tmp_path):
    env = Env(tmp_path, at(12))
    episode_id = await env.add_episode("昨日カレーを作ったけど辛すぎたんだよね", happened=at(12, day=27))
    before = next(e for e in env.episodes() if e["id"] == episode_id)
    await env.say("昨日カレーを作ったけど辛すぎたんだよね、また作る")

    system = env.llm.chats[-1][0]["content"]
    assert "## 思い出したこと" in system and "[9/27 12:00] 昨日カレーを作ったけど辛すぎたんだよね" in system
    after = next(e for e in env.episodes() if e["id"] == episode_id)
    assert after["access_count"] == 1
    assert datetime.fromisoformat(after["grace_until"]) == datetime.fromisoformat(before["grace_until"]) + GRACE_EXTEND
    assert datetime.fromisoformat(after["expires_at"]) == datetime.fromisoformat(before["expires_at"]) + LIFETIME_EXTEND


async def test_deletable_episode_revives_when_recalled(tmp_path):
    env = Env(tmp_path, at(12))
    episode_id = await env.add_episode("昨日カレーを作ったけど辛すぎたんだよね", grace_until=at(1).isoformat())
    await env.say("昨日カレーを作ったけど辛すぎたんだよね")
    [episode] = [e for e in env.episodes() if e["id"] == episode_id]
    assert datetime.fromisoformat(episode["grace_until"]) == at(12) + GRACE_EXTEND  # 今から延びる


async def test_expired_episode_is_not_recalled(tmp_path):
    env = Env(tmp_path, at(12))
    await env.add_episode("昨日カレーを作ったけど辛すぎたんだよね", expires_at=at(11).isoformat())
    await env.say("昨日カレーを作ったけど辛すぎたんだよね")
    assert "## 思い出したこと" not in env.llm.chats[-1][0]["content"]


async def test_episode_from_history_window_is_not_recalled(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("昨日カレーを作ったけど辛すぎた")
    await env.memory.adjust("sui")
    [first, _] = env.db.recent_messages(env.sessions["main"], 2)
    env.db.delete_episodes([e["id"] for e in env.episodes()])
    await env.add_episode(
        "昨日カレーを作ったけど辛すぎた", session_id=env.sessions["main"], first_message_id=first["id"],
        last_message_id=first["id"] + 1,
    )
    await env.say("昨日カレーを作ったけど辛すぎたんだよね")
    assert "## 思い出したこと" not in env.llm.chats[-1][0]["content"]  # 会話履歴にそのまま入っている


# --- 件数の上限 ---


async def test_prune_removes_expired_and_some_deletable_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "EPISODE_LIMIT", 3)
    env = Env(tmp_path, at(12), memory_rand=lambda: 0.0)
    await env.add_episode("時限切れ", expires_at=at(11).isoformat())
    deletable = [await env.add_episode(f"削除対象{i}", grace_until=at(11).isoformat()) for i in range(3)]
    keep = [await env.add_episode(f"まだ猶予がある{i}") for i in range(2)]

    result = env.memory._prune("sui")

    assert result == {"expired": 1, "over_limit": 2}
    remaining = {e["id"] for e in env.episodes()}
    assert set(keep) <= remaining and len(remaining & set(deletable)) == 1


async def test_prune_never_removes_episodes_still_in_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_module, "EPISODE_LIMIT", 1)
    env = Env(tmp_path, at(12))
    for i in range(4):
        await env.add_episode(f"まだ猶予がある{i}")
    assert env.memory._prune("sui")["over_limit"] == 0  # 一時的な超過を許す
    assert len(env.episodes()) == 4


def test_prune_weight_favors_unimportant_episodes():
    now = at(12)
    base = {"access_count": 0, "content": "あ" * 50, "expires_at": (now + timedelta(days=10)).isoformat()}
    assert prune_weight(base, now) > prune_weight(dict(base, access_count=5), now)  # よく思い出されるものは残りやすい
    assert prune_weight(dict(base, expires_at=now.isoformat()), now) > prune_weight(base, now)  # 時限が近いほど消えやすい
    assert prune_weight(dict(base, content="短い"), now) > prune_weight(base, now)  # 短いほど消えやすい


# --- 会話の種 ---


async def test_adjust_makes_recall_seeds_from_older_episodes(tmp_path):
    llm = RecordingLLM(adjust={"episodes": [], "recall_seeds": ["京都旅行のお土産の話"], "drop_seeds": []})
    env = Env(tmp_path, at(12), llm=llm)
    await env.add_episode("マスターが京都旅行に行った", happened=at(12, day=25))
    await env.add_episode("さっきの話", happened=at(11))  # 新しすぎるものは材料にしない
    await env.say("ただいま")
    await env.memory.adjust("sui")
    prompt = llm.prompts[-1]
    assert "- [9/25 12:00] マスターが京都旅行に行った" in prompt and "さっきの話" not in prompt.split("少し前の出来事")[1]
    [seed] = env.db.list_seeds("sui")
    assert (seed["source"], seed["content"]) == ("recall", "京都旅行のお土産の話")


async def test_adjust_drops_seeds(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("ただいま")
    seed_id = env.db.add_seed("sui", "古い話題", at(11).isoformat())
    env.llm._adjust = {"episodes": [], "drop_seeds": [seed_id, 9999]}
    await env.memory.adjust("sui")
    assert env.db.list_seeds("sui") == []


async def test_proactive_speech_uses_a_seed_and_marks_it_used(tmp_path):
    env = Env(tmp_path, at(12))
    env.db.add_seed("sui", "猫を飼いたい話の続き", at(11).isoformat())
    await env.proactive.speak("sui", "暇つぶし")
    instruction = env.llm.chats[-1][-1]["content"]
    assert "猫を飼いたい話の続き" in instruction
    [seed] = env.db.list_seeds("sui")
    assert seed["used_at"] is not None
    assert "種：猫を飼いたい話の続き" in env.db.recent_state_events("sui", 1)[0]["detail"]


async def test_proactive_speech_without_seed_is_impromptu(tmp_path):
    env = Env(tmp_path, at(12))
    await env.proactive.speak("sui", "暇つぶし")
    assert "話題：" not in env.llm.chats[-1][-1]["content"]
    assert "即興" in env.db.recent_state_events("sui", 1)[0]["detail"]


async def test_proactive_speech_is_sometimes_impromptu_even_with_seeds(tmp_path):
    env = Env(tmp_path, at(12), memory_rand=lambda: 0.1)  # IMPROMPTU_PROBABILITY 未満
    env.db.add_seed("sui", "猫を飼いたい話の続き", at(11).isoformat())
    await env.proactive.speak("sui", "暇つぶし")
    assert "話題：" not in env.llm.chats[-1][-1]["content"]
    assert env.db.list_seeds("sui")[0]["used_at"] is None  # 種は使われずに残る
    assert "即興" in env.db.recent_state_events("sui", 1)[0]["detail"]


async def test_expired_seed_is_not_picked(tmp_path):
    env = Env(tmp_path, at(12))
    env.db.add_seed("sui", "古すぎる話題", (at(12) - timedelta(days=4)).isoformat())
    assert env.memory.pick_seed("sui") is None


# --- 記憶調整のきっかけ ---


def test_gate_adds_memory_trigger_after_conversation_break():
    kwargs = dict(
        state=CharacterState(boredom=0.0),
        availability="both",
        personality=SUI.personality,
        crossings=(),
        since_proactive=None,
        unanswered=0,
    )
    assert gate(**kwargs, since_conversation=timedelta(minutes=15), unprocessed=4).triggers == {"memory": 0.5}
    assert gate(**kwargs, since_conversation=timedelta(minutes=15), unprocessed=3).blocked is None
    assert gate(**kwargs, since_conversation=timedelta(minutes=5), unprocessed=10).blocked == "会話した直後"
    # 話しかけが抑制されていても、記憶調整のきっかけは通る
    blocked_speak = dict(kwargs, unanswered=3)
    assert gate(**blocked_speak, since_conversation=timedelta(minutes=15), unprocessed=4).triggers == {"memory": 0.5}


async def test_judge_can_choose_memory_adjustment(tmp_path):
    judge = RecordingLLM(judge={"action": "memory", "intent": None, "reason": "振り返りたい"})
    env = Env(tmp_path, at(12), judge=judge)
    for text in ["ただいま", "今日は疲れた"]:
        await env.say(text)
    env.clock.now = at(12, 20)
    decision = await env.proactive.evaluate("sui")
    assert decision.action == "memory"
    assert "まだ振り返っていない発言：4件" in judge.prompts[-1]
    assert env.episodes() and env.memory.unprocessed("sui") == []


# --- 睡眠中の整理 ---


async def test_consolidate_updates_profiles_from_memos(tmp_path):
    def profile(prompt):
        about_master = f"{SUI.user_name}についての印象" in prompt
        return {
            "impression": "カレーが好き。" if about_master else "隣にいるのが好き。",
            "topic_seeds": ["カレーの新しいレシピを聞く"],
        }

    llm = RecordingLLM(profile=profile)
    env = Env(tmp_path, at(23), llm=llm)
    await env.say("今日もカレー作った")  # 残りの短期記憶はエピソード化される（モックが about_master を付ける）
    env.db.add_memo("sui", "self", "マスターの隣は落ち着く", at(20).isoformat())

    result = await env.memory.consolidate("sui")

    assert env.memory.unprocessed("sui") == [] and result["episodes"] == 1
    profiles = env.db.get_profiles("sui")
    assert profiles["user"]["impression"] == "カレーが好き。"
    assert profiles["self"]["impression"] == "隣にいるのが好き。"
    assert env.db.list_memos("sui") == []  # 使い終わった印象メモは消える
    user_prompt = next(p for p in llm.prompts if p.startswith(PROFILE_PREFIX) and "話しかけてくれるのが嬉しい" in p)
    assert "topic_seeds" in user_prompt and "会話の種（topic_seeds）" in user_prompt
    self_prompt = next(p for p in llm.prompts if p.startswith(PROFILE_PREFIX) and "マスターの隣は落ち着く" in p)
    assert "topic_seeds は空の配列" in self_prompt
    assert [(s["source"], s["content"]) for s in env.db.list_seeds("sui") if s["source"] == "topic"] == [
        ("topic", "カレーの新しいレシピを聞く")
    ]

    system = env.conversation.build_system_prompt(SUI)
    assert "## 記憶" in system and f"### {SUI.user_name}について\nカレーが好き。" in system


async def test_consolidate_keeps_memos_when_profile_update_fails(tmp_path):
    env = Env(tmp_path, at(23), llm=RecordingLLM(profile={"impression": ""}))
    env.db.add_memo("sui", "user", "カレー好き", at(20).isoformat())
    await env.memory.consolidate("sui")
    assert env.db.get_profiles("sui") == {}
    assert len(env.db.list_memos("sui")) == 1  # 次の睡眠でやり直す


async def test_falling_asleep_says_goodnight_then_consolidates(tmp_path):
    env = Env(tmp_path, at(1, 10, day=29))
    env.state.state("sui").fatigue = 0.3
    await env.say("まだ起きてる？")
    env.clock.now = at(1, 30, day=29)
    await env.state.tick_all()
    await env.settle()
    assert env.state.state("sui").asleep
    adjust_prompt = next(p for p in env.llm.prompts if p.startswith(ADJUST_PREFIX))
    assert "(mock) " in adjust_prompt  # おやすみの一言（モックの応答）もエピソード化の入力に入っている
    assert env.memory.unprocessed("sui") == []
    assert env.db.get_profiles("sui")["user"]["impression"]


# --- v1 からの移行 ---


def test_v1_memory_tables_are_dropped(tmp_path):
    path = tmp_path / "v1.db"
    db = Database(path)
    db.ensure_default_sessions("sui")
    sid = db.list_sessions("sui")[0]["id"]
    db.add_message(sid, "user", "前からの会話", at(12).isoformat())
    db.set_setting("memory_cursor:sui", 5)
    db.close()
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE stm (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("CREATE TABLE ltm (character_id TEXT PRIMARY KEY, conversation TEXT)")
    conn.execute("DROP TABLE seeds")
    conn.execute("CREATE TABLE seeds (id INTEGER PRIMARY KEY, character_id TEXT, content TEXT, created_at TEXT, used_at TEXT)")
    conn.commit()
    conn.close()

    db = Database(path)
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"stm", "ltm"} & tables
    assert db.add_seed("sui", "新しい種", at(12).isoformat(), source="topic")  # v2 の形で作り直されている
    assert db.get_setting("memory_cursor:sui") is None
    assert [m["content"] for m in db.unmemorized_messages("sui")] == ["前からの会話"]  # 会話ログは残る


# --- 記憶用のモデル ---


def test_memory_uses_its_own_model(monkeypatch):
    from engawa.config import load_settings
    from engawa.core.llm import MEMORY_TIMEOUT, OllamaClient, create_llm, create_memory_llm

    monkeypatch.setenv("ENGAWA_LLM_BACKEND", "ollama")
    monkeypatch.setenv("ENGAWA_LLM_MODEL", "gemma4:12b")
    monkeypatch.setenv("ENGAWA_MEMORY_MODEL", "qwen3.8:27b-mlx")
    settings = load_settings()
    memory_llm, chat_llm = create_memory_llm(settings), create_llm(settings)
    assert isinstance(memory_llm, OllamaClient)
    assert (memory_llm.name, chat_llm.name) == ("ollama:qwen3.8:27b-mlx", "ollama:gemma4:12b")
    assert memory_llm._timeout == MEMORY_TIMEOUT


# --- API ---


def test_memory_api(tmp_path):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=FakeClock(at(12)), embedder=MockEmbedder())
    with TestClient(app) as client:
        main_id = next(s["id"] for s in client.get("/characters/sui/sessions").json() if s["kind"] == "main")
        with client.websocket_connect("/ws") as ws:
            client.post(f"/sessions/{main_id}/messages", json={"content": "こんにちは"})
            while ws.receive_json()["type"] != "message.completed":
                pass
        snapshot = client.get("/characters/sui/memory").json()
        assert snapshot["unprocessed"] == 2
        assert snapshot["profiles"] == {"user": None, "self": None}

        adjusted = client.post("/characters/sui/memory/adjust").json()
        assert adjusted["episodes"] == 1 and adjusted["processed"] == 2
        snapshot = client.get("/characters/sui/memory").json()
        [episode] = snapshot["episodes"]
        assert "embedding" not in episode and episode["embedded"] and not episode["deletable"]
        assert snapshot["memos"][0]["subject"] == "user"

        consolidated = client.post("/characters/sui/memory/consolidate").json()
        assert consolidated["profiles"]["user"] > 0
        snapshot = client.get("/characters/sui/memory").json()
        assert snapshot["profiles"]["user"]["impression"] and snapshot["memos"] == []
        assert {s["source"] for s in snapshot["seeds"]} == {"recall", "topic"}
        assert client.get("/characters/nobody/memory").status_code == 404
