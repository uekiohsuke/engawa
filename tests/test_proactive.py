import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from engawa.characters import CHARACTERS, Personality, get_character
from engawa.config import Settings
from engawa.core.app import create_app
from engawa.core.conversation import ConversationService, should_say_goodnight
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import INSTRUCTION_PREFIX, MockLLMClient
from engawa.core.proactive import (
    FAREWELL,
    ProactiveService,
    candidate_intents,
    gate,
    judge,
)
from engawa.core.state import CharacterState

SUI = get_character("sui")


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute).astimezone()


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingHub(EventHub):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    async def publish(self, event_type: str, **payload) -> None:
        self.events.append({"type": event_type, **payload})

    def of(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["type"] == event_type]


class FakeJudge(MockLLMClient):
    def __init__(self, response):
        super().__init__(delay=0)
        self.response = response
        self.calls = 0

    async def complete_json(self, messages):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class Env:
    """能動発話まわりのサービス一式（固定時計・常にゲート通過）。"""

    def __init__(self, tmp_path, now: datetime, boredom: float = 0.0, judge_response=None):
        from engawa.core.state_service import StateService

        self.clock = FakeClock(now)
        self.db = Database(tmp_path / "t.db")
        self.db.ensure_default_sessions("sui")
        self.db.save_state("sui", CharacterState(boredom=boredom, updated_at=now.isoformat()).to_dict())
        self.hub = RecordingHub()
        self.state = StateService(self.db, self.hub, CHARACTERS.values(), self.clock)
        self.conversation = ConversationService(self.db, MockLLMClient(delay=0), self.hub, self.state, 20)
        self.judge = FakeJudge(judge_response or {"action": "speak", "intent": "暇つぶし", "reason": "暇"})
        self.proactive = ProactiveService(
            self.db,
            self.hub,
            self.state,
            self.conversation,
            self.judge,
            CHARACTERS.values(),
            interval_seconds=0,
            dialogue_timeout_seconds=0.05,
            rand=lambda: 0.0,
        )
        self.sessions = {s["kind"]: s["id"] for s in self.db.list_sessions("sui")}

    def messages(self, kind: str) -> list[dict]:
        return self.db.recent_messages(self.sessions[kind], 100)

    async def settle(self) -> None:
        """バックグラウンドの生成・送り直しを待つ。"""
        for _ in range(200):
            if not self.proactive._tasks and not self.conversation._tasks:
                return
            await asyncio.sleep(0.01)
        raise TimeoutError


# --- ゲート ---


def _gate(**overrides):
    kwargs = dict(
        state=CharacterState(boredom=0.8),
        availability="both",
        personality=SUI.personality,
        crossings=(),
        since_conversation=timedelta(hours=1),
        since_proactive=None,
        unanswered=0,
    )
    kwargs.update(overrides)
    return gate(**kwargs)


def test_gate_probability_rises_with_boredom():
    low = _gate(state=CharacterState(boredom=0.4)).probability
    high = _gate(state=CharacterState(boredom=0.9)).probability
    assert 0 < low < high
    assert _gate(state=CharacterState(boredom=0.2)).triggers == {}


def test_gate_crossing_probability_depends_on_personality():
    sui = _gate(state=CharacterState(), crossings={"sleepiness", "fatigue"})
    assert sui.triggers == {"sleepiness": 0.9, "fatigue": 0.05}  # 会話を求める／静か
    clingy = _gate(
        state=CharacterState(),
        crossings={"fatigue"},
        personality=Personality(fatigue_response="構ってほしい"),
    )
    assert clingy.triggers == {"fatigue": 0.7}


def test_gate_is_weaker_when_message_only():
    assert _gate(availability="message_only").probability == pytest.approx(_gate().probability * 0.5)


@pytest.mark.parametrize(
    "overrides",
    [
        {"availability": "sleeping"},
        {"since_conversation": timedelta(minutes=5)},
        {"since_proactive": timedelta(minutes=20)},
        {"since_proactive": timedelta(minutes=45), "unanswered": 1},  # 返事がないと間隔が倍に
        {"unanswered": 3},
    ],
)
def test_gate_blocks(overrides):
    assert _gate(**overrides).blocked


def test_gate_allows_after_cooldown():
    assert not _gate(since_proactive=timedelta(minutes=61), unanswered=1).blocked


# --- 判定層 ---


async def test_judge_validates_output():
    candidates = candidate_intents(["boredom"])
    assert candidates == ["暇つぶし", "様子うかがい"]
    ok = await judge(FakeJudge({"action": "speak", "intent": "様子うかがい", "reason": "r"}), [], candidates)
    assert (ok.speak, ok.intent) == (True, "様子うかがい")
    unknown = await judge(FakeJudge({"action": "speak", "intent": "世界征服", "reason": "r"}), [], candidates)
    assert unknown.intent == "暇つぶし"
    silent = await judge(FakeJudge({"action": "silent", "intent": None, "reason": "今はいい"}), [], candidates)
    assert not silent.speak
    broken = await judge(FakeJudge(ValueError("bad json")), [], candidates)
    assert not broken.speak and "エラー" in broken.reason


# --- 能動発話サービス ---


async def test_speaks_in_dialogue_then_falls_back_to_message(tmp_path):
    env = Env(tmp_path, at(12), boredom=0.8)
    decision = await env.proactive.evaluate("sui")
    assert decision.speak and decision.intent == "暇つぶし"
    started = env.hub.of("proactive.started")
    assert started[0]["mode"] == "dialogue" and started[0]["session_id"] == env.sessions["dialogue"]
    [spoken] = env.messages("dialogue")
    assert spoken["role"] == "character"

    await env.settle()  # 返事がないまま対話のタイムアウトを過ぎる
    [resent] = env.messages("main")
    assert resent["content"] == spoken["content"]
    assert env.hub.of("dialogue.fallback")

    kinds = [e["kind"] for e in env.db.recent_state_events("sui", 10)]
    assert "judgment" in kinds and "proactive" in kinds
    assert env.state.state("sui").boredom == pytest.approx(0.7)


async def test_no_fallback_when_user_replies(tmp_path):
    env = Env(tmp_path, at(12), boredom=0.8)
    await env.proactive.evaluate("sui")
    await env.conversation.post_user_message(SUI, env.sessions["dialogue"], "なに？")
    await env.settle()
    assert env.messages("main") == []
    assert not env.hub.of("dialogue.fallback")


async def test_message_only_speaks_in_main(tmp_path):
    env = Env(tmp_path, at(15), boredom=0.8)  # 14〜17時は取り込み中
    await env.proactive.evaluate("sui")
    await env.settle()
    assert env.hub.of("proactive.started")[0]["mode"] == "message"
    assert len(env.messages("main")) == 1
    assert env.messages("dialogue") == []


async def test_focus_mode_forces_message(tmp_path):
    env = Env(tmp_path, at(12), boredom=0.8)
    await env.state.set_focus_mode(True)
    assert env.state.availability("sui") == "message_only"
    await env.proactive.evaluate("sui")
    await env.settle()
    assert len(env.messages("main")) == 1
    assert env.messages("dialogue") == []


async def test_judge_silence_records_reason_and_does_not_speak(tmp_path):
    env = Env(tmp_path, at(12), boredom=0.8, judge_response={"action": "silent", "intent": None, "reason": "気分じゃない"})
    decision = await env.proactive.evaluate("sui")
    assert not decision.speak
    assert env.messages("dialogue") == [] and env.messages("main") == []
    assert "気分じゃない" in env.db.recent_state_events("sui", 1)[0]["detail"]


async def test_cooldown_and_unanswered_backoff(tmp_path):
    env = Env(tmp_path, at(9), boredom=1.0)
    assert (await env.proactive.evaluate("sui")).speak
    await env.settle()
    env.clock.now += timedelta(minutes=20)
    assert await env.proactive.evaluate("sui") is None  # 会話直後・前回から間もない
    env.clock.now += timedelta(minutes=30)  # 50分後：返事がないので 30分×2 のクールダウン中
    assert await env.proactive.evaluate("sui") is None
    env.clock.now += timedelta(minutes=11)  # 61分後
    assert (await env.proactive.evaluate("sui")).speak
    assert env.judge.calls == 2


async def test_no_judgment_below_boredom_threshold(tmp_path):
    env = Env(tmp_path, at(12), boredom=0.1)
    assert await env.proactive.evaluate("sui") is None
    assert env.judge.calls == 0


async def test_says_goodnight_when_falling_asleep_mid_conversation(tmp_path):
    env = Env(tmp_path, at(1, 10, day=29))
    await env.conversation.post_user_message(SUI, env.sessions["main"], "まだ起きてる？")
    await env.settle()
    env.clock.now = at(1, 30, day=29)  # 1:20 頃に眠気が睡眠の閾値を超える
    await env.state.tick_all()
    await env.settle()
    assert env.state.state("sui").asleep
    started = env.hub.of("proactive.started")
    assert started and started[-1]["intent"] == FAREWELL and started[-1]["session_id"] == env.sessions["main"]
    assert env.messages("main")[-1]["role"] == "character"
    assert not env.hub.of("dialogue.fallback")


def test_should_say_goodnight_window():
    sleep_at = at(1, 20, day=29)
    assert should_say_goodnight(sleep_at, at(1, 10, day=29))  # 就寝の直前まで話していた
    assert should_say_goodnight(sleep_at, at(1, 30, day=29))  # 定期更新の合間の発言で就寝が判明
    assert not should_say_goodnight(sleep_at, at(0, 30, day=29))
    assert not should_say_goodnight(sleep_at, at(2, 0, day=29))  # 寝た後しばらくしてからの発言
    assert not should_say_goodnight(sleep_at, None)


async def test_falling_asleep_on_user_message_replies_goodnight_only(tmp_path):
    env = Env(tmp_path, at(1, 0, day=29))
    env.clock.now = at(1, 30, day=29)  # 状態は1:00のまま。この発言で時間が進み、眠りに落ちる
    await env.conversation.post_user_message(SUI, env.sessions["main"], "まだ起きてる？")
    await env.settle()
    roles = [m["role"] for m in env.messages("main")]
    assert roles == ["user", "character"]  # 「眠っている」定型文は出さない
    assert env.hub.of("proactive.started")[-1]["intent"] == FAREWELL
    # 以降は定型文のみ
    await env.conversation.post_user_message(SUI, env.sessions["main"], "おーい")
    assert env.messages("main")[-1]["role"] == "system"


async def test_no_goodnight_without_recent_conversation(tmp_path):
    env = Env(tmp_path, at(1, 0, day=29))
    env.clock.now = at(1, 30, day=29)
    await env.state.tick_all()
    await env.settle()
    assert env.state.state("sui").asleep
    assert not env.hub.of("proactive.started")


async def test_force_judge_skips_gate_but_not_sleep(tmp_path):
    env = Env(tmp_path, at(12), boredom=0.0)
    assert (await env.proactive.evaluate("sui", force=True)).speak
    await env.settle()
    env.state.state("sui").asleep = True
    assert await env.proactive.evaluate("sui", force=True) is None


def test_proactive_instruction_reaches_generation(tmp_path):
    env = Env(tmp_path, at(12))
    messages = env.conversation.build_messages(SUI, env.sessions["main"], "話しかけて")
    assert messages[-1] == {"role": "user", "content": INSTRUCTION_PREFIX + "話しかけて"}


# --- API ---


def test_focus_and_judge_api(tmp_path):
    settings = Settings(data_dir=tmp_path, judge_interval_seconds=0, dialogue_timeout_seconds=60)
    app = create_app(settings, llm=MockLLMClient(delay=0), clock=FakeClock(at(12)))
    with TestClient(app) as client:
        assert client.get("/focus").json() == {"enabled": False}
        assert client.put("/focus", json={"enabled": True}).json() == {"enabled": True}
        state = client.get("/characters/sui/state").json()
        assert state["availability"] == "message_only"
        assert state["availability_label"] == "取り込み中（集中モード）"
        result = client.post("/characters/sui/judge").json()
        assert result["judged"] and result["speak"]
        assert client.post("/characters/nobody/judge").status_code == 404
