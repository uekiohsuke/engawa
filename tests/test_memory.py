import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from engawa.characters import CHARACTERS, get_character
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.conversation import ConversationService
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import ADJUST_PREFIX, DISTILL_PREFIX, MockEmbedder, MockLLMClient
from engawa.core.memory import KIND_CONVERSATION, KIND_SELF, STM_KEEP_PER_DAY, MemoryService, pack
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
    """生成に渡されたメッセージと、JSON処理のプロンプトを記録する。"""

    def __init__(self, adjust=None, distill=None, judge=None):
        super().__init__(delay=0)
        self.chats: list[list[dict]] = []
        self.prompts: list[str] = []
        self._adjust, self._distill, self._judge = adjust, distill, judge

    async def stream_chat(self, messages):
        self.chats.append(messages)
        async for d in super().stream_chat(messages):
            yield d

    async def complete_json(self, messages):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if prompt.startswith(ADJUST_PREFIX) and self._adjust is not None:
            return self._adjust
        if prompt.startswith(DISTILL_PREFIX) and self._distill is not None:
            return self._distill
        if self._judge is not None and not prompt.startswith((ADJUST_PREFIX, DISTILL_PREFIX)):
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

    def stm(self, kind: str | None = None) -> list[dict]:
        return [s for s in self.db.list_stm("sui") if kind is None or s["kind"] == kind]


# --- STM ---


async def test_conversation_is_recorded_as_stm_with_embeddings(tmp_path):
    env = Env(tmp_path, at(12))
    await env.say("昨日カレーを作ったよ")
    items = env.stm(KIND_CONVERSATION)
    assert [(s["speaker"], s["kind"]) for s in items] == [("user", "conversation"), ("character", "conversation")]
    assert items[0]["content"] == "昨日カレーを作ったよ"
    assert all(s["embedding"] is not None for s in items)


async def test_asleep_notice_is_not_stm_but_user_message_is(tmp_path):
    env = Env(tmp_path, at(3, day=29))
    env.state.state("sui").asleep = True
    await env.say("起きてる？")
    assert [s["speaker"] for s in env.stm()] == ["user"]


async def test_related_stm_is_recalled_outside_history_window(tmp_path):
    env = Env(tmp_path, at(12), history_window=2)
    await env.say("昨日カレーを作ったけど辛すぎた")
    await env.say("今日はいい天気だね")
    await env.say("昨日カレーを作ったけど辛すぎたんだよね、また作る")
    system = env.llm.chats[-1][0]["content"]
    assert "## 思い出したこと" in system
    assert "マスター：昨日カレーを作ったけど辛すぎた" in system
    recalled = next(s for s in env.stm() if s["content"] == "昨日カレーを作ったけど辛すぎた")
    assert recalled["ref_count"] == 1


async def test_messages_in_history_window_are_not_recalled(tmp_path):
    env = Env(tmp_path, at(12), history_window=20)
    await env.say("昨日カレーを作ったけど辛すぎた")
    await env.say("昨日カレーを作ったけど辛すぎたんだよね")
    assert "## 思い出したこと" not in env.llm.chats[-1][0]["content"]


async def test_expired_stm_is_not_recalled(tmp_path):
    env = Env(tmp_path, at(12), history_window=1)
    old = (at(12) - timedelta(days=4)).isoformat()
    [vector] = await MockEmbedder().embed(["昨日カレーを作ったけど辛すぎた"])
    env.db.add_stm("sui", KIND_CONVERSATION, "昨日カレーを作ったけど辛すぎた", old, "user", embedding=pack(vector))
    await env.say("昨日カレーを作ったけど辛すぎたんだよね")
    assert "## 思い出したこと" not in env.llm.chats[-1][0]["content"]


# --- 記憶調整 ---


async def test_adjust_creates_seeds_and_self_memories_and_advances_cursor(tmp_path):
    llm = RecordingLLM(adjust={"seeds": ["カレーの辛さの好みを聞く"], "drop_seeds": [], "self_memories": ["マスターの料理の話が好き"]})
    env = Env(tmp_path, at(12), llm=llm)
    await env.say("昨日カレーを作ったけど辛すぎた")
    assert len(env.memory.unprocessed("sui")) == 2
    result = await env.memory.adjust("sui")
    assert result["processed"] == 2
    assert [s["content"] for s in env.db.list_seeds("sui")] == ["カレーの辛さの好みを聞く"]
    [self_memory] = env.stm(KIND_SELF)
    assert self_memory["content"] == "マスターの料理の話が好き" and self_memory["embedding"] is not None
    assert env.memory.unprocessed("sui") == []
    prompt = llm.prompts[-1]
    assert "マスター：昨日カレーを作ったけど辛すぎた" in prompt


async def test_adjust_drops_seeds(tmp_path):
    env = Env(tmp_path, at(12))
    seed_id = env.db.add_seed("sui", "古い話題", at(11).isoformat())
    env.llm._adjust = {"seeds": [], "drop_seeds": [seed_id, 9999], "self_memories": []}
    await env.memory.adjust("sui")
    assert env.db.list_seeds("sui") == []


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
    assert "記憶調整" in judge.prompts[-1] or "memory" in judge.prompts[-1]
    assert env.db.list_seeds("sui")  # モックの記憶調整が種を作った
    assert env.memory.unprocessed("sui") == []


# --- 会話の種を使った能動発話 ---


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


# --- 夜間蒸留 ---


async def test_distill_updates_ltm_and_keeps_top_stm(tmp_path):
    llm = RecordingLLM(distill={"conversation": "マスターはカレーが好き。", "self": "隣にいるのが好き。"})
    env = Env(tmp_path, at(23), llm=llm)
    ids = [
        env.db.add_stm("sui", KIND_CONVERSATION, f"発言{i}", at(20, i).isoformat(), "user") for i in range(12)
    ]
    env.db.add_stm("sui", KIND_SELF, "今日はのんびりした", at(21).isoformat())
    env.db.increment_stm_refs([ids[3], ids[3], ids[5]])
    # 前日から持ち越したSTM（前回の蒸留で残ったもの）と、期限切れのSTM
    carried = env.db.add_stm("sui", KIND_CONVERSATION, "昨日の発言", (at(23) - timedelta(days=1)).isoformat(), "user")
    old = env.db.add_stm("sui", KIND_CONVERSATION, "4日前の発言", (at(23) - timedelta(days=4)).isoformat(), "user")
    env.db.mark_stm_distilled([carried, old])
    env.db.add_seed("sui", "期限切れの種", (at(23) - timedelta(days=4)).isoformat())

    result = await env.memory.distill("sui")

    ltm = env.db.get_ltm("sui")
    assert (ltm["conversation"], ltm["self"]) == ("マスターはカレーが好き。", "隣にいるのが好き。")
    prompt = llm.prompts[-1]
    assert "［参照2回］[9/28 20:03] マスター：発言3" in prompt
    assert "今日はのんびりした" in prompt
    assert "マスター：昨日の発言（前日以前から残っている記憶）" in prompt  # 持ち越し分も入力に入る
    assert "4日前の発言" not in prompt  # 期限切れは先に削除
    assert (result["input"], result["today"]) == (14, 13)

    remaining = env.stm()
    assert len(remaining) == STM_KEEP_PER_DAY + 1  # 今日の上位10件＋持ち越し分
    assert carried in {s["id"] for s in remaining}
    assert all(s["distilled"] for s in remaining)
    assert {ids[3], ids[5]} <= {s["id"] for s in remaining}  # 参照回数の多いものが残る
    assert result["expired_stm"] == 1 and result["expired_seeds"] == 1

    system = env.conversation.build_system_prompt(SUI)
    assert "## 長期記憶" in system and "マスターはカレーが好き。" in system


async def test_falling_asleep_says_goodnight_then_distills(tmp_path):
    env = Env(tmp_path, at(1, 10, day=29))
    env.state.state("sui").fatigue = 0.3
    await env.say("まだ起きてる？")
    env.clock.now = at(1, 30, day=29)
    await env.state.tick_all()
    await env.settle()
    assert env.state.state("sui").asleep
    distill_prompt = next(p for p in env.llm.prompts if p.startswith(DISTILL_PREFIX))
    assert "(mock) " in distill_prompt  # おやすみの一言（モックの応答）も蒸留の入力に入っている
    assert env.db.get_ltm("sui") is not None


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
        assert snapshot["ltm"] is None
        assert "embedding" not in snapshot["stm"][0]

        adjusted = client.post("/characters/sui/memory/adjust").json()
        assert adjusted["processed"] == 2 and adjusted["seeds"]
        distilled = client.post("/characters/sui/memory/distill").json()
        assert distilled["input"] >= 2
        snapshot = client.get("/characters/sui/memory").json()
        assert snapshot["ltm"]["conversation"]
        assert snapshot["seeds"][0]["content"]
        assert client.get("/characters/nobody/memory").status_code == 404
