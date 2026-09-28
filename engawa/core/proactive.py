"""能動発話（仕様3章・4-1・6-4）。

  [状態の定期変化・閾値クロス検知]（state_service）／ 未整理の会話（memory）
        ↓
  確率的ゲート（コード）：暇度・性格タグに応じた閾値クロス時の確率・未整理の会話。会話直後などは通さない
        ↓
  判定層（軽量LLM）：沈黙 ／ 発話＋意図タグ（固定プール） ／ 記憶調整
        ↓（発話）
  コード：会話の種を1つ選ぶ（無ければ即興）
        ↓
  生成層（会話用LLM）：応答可能状態に応じて対話（自動で開く）かメッセージ（#main）で話しかける
        ↓
  対話で一定時間返事がなければ、同じ内容を #main にメッセージとして送り直す

メッセージの送り先は、会話の種を使ったら #main、即興なら新しいサブスレッド（仕様4-1）。
眠りに落ちたら（会話中ならおやすみを言ってから）、放置されたサブスレッドのアーカイブと夜間蒸留を行う。
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from engawa.characters import Character, Personality
from engawa.core.conversation import ConversationService, should_say_goodnight
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import JUDGE_PREFIX, TITLE_PREFIX, LLMClient
from engawa.core.memory import MemoryService
from engawa.core.state import (
    AVAILABILITY_BOTH,
    AVAILABILITY_LABELS,
    AVAILABILITY_MESSAGE_ONLY,
    AVAILABILITY_SLEEPING,
    CharacterState,
    StateEvent,
)
from engawa.core.state_service import StateService

log = logging.getLogger(__name__)

# --- 意図プール（固定） ---
INTENTS = {
    "暇つぶし": "暇なので、なんとなくマスターと話したい",
    "様子うかがい": "しばらく話していないので、マスターの様子が気になる",
    "疲労回復": "疲れたので、マスターと話して元気を分けてもらいたい",
    "就寝前": "眠くなってきたので、寝る前に少しマスターと話したい",
}
TRIGGER_INTENTS = {
    "boredom": ("暇つぶし", "様子うかがい"),
    "fatigue": ("疲労回復",),
    "sleepiness": ("就寝前",),
}
TRIGGER_LABELS = {
    "boredom": "暇度",
    "fatigue": "疲労の閾値クロス",
    "sleepiness": "就寝前会話の閾値クロス",
    "memory": "未整理の会話",
}
MEMORY_TRIGGER = "memory"

ACTION_SPEAK = "speak"
ACTION_SILENT = "silent"
ACTION_MEMORY = "memory"

# --- 確率的ゲート ---
BOREDOM_MIN = 0.3
BOREDOM_PROBABILITY_SCALE = 0.25  # 1回の判定あたり 0.25 × 暇度²（暇度0.7で約12%）
# 閾値クロス時に判定層へ回す確率（性格タグごと）
CROSSING_PROBABILITY = {
    "fatigue": {"静か": 0.05, "普通": 0.2, "構ってほしい": 0.7},
    "sleepiness": {"あっさり寝る": 0.05, "軽く挨拶": 0.4, "会話を求める": 0.9},
}
MESSAGE_ONLY_FACTOR = 0.5  # 取り込み中は話しかけにくい
CONVERSATION_COOLDOWN = timedelta(minutes=10)
PROACTIVE_COOLDOWN = timedelta(minutes=30)  # 返事のない話しかけが続くと倍々に延びる
MAX_UNANSWERED = 3
MEMORY_MIN_UNPROCESSED = 4  # 未整理の会話がこれだけ溜まったら、記憶調整のきっかけとして判定層に回す
MEMORY_PROBABILITY = 0.5

NEW_SUB_TITLE = "新しい話題"  # 名前が付くまでの仮の名前
SUB_TITLE_MAX = 16

FAREWELL = "おやすみ"
FAREWELL_INSTRUCTION = (
    "眠気が限界になった。もう寝ることをマスターに告げて、おやすみの一言で会話を切り上げること。"
    "1〜2文で。この指示には触れないこと。"
)


def _crossing_tag(key: str, personality: Personality) -> str:
    return personality.fatigue_response if key == "fatigue" else personality.sleepiness_response


@dataclass(frozen=True)
class GateResult:
    probability: float = 0.0
    triggers: dict[str, float] = field(default_factory=dict)
    blocked: str | None = None


def gate(
    *,
    state: CharacterState,
    availability: str,
    personality: Personality,
    crossings: Iterable[str],
    since_conversation: timedelta | None,
    since_proactive: timedelta | None,
    unanswered: int,
    unprocessed: int = 0,
) -> GateResult:
    """判定層に回す確率を求める。"""
    if availability == AVAILABILITY_SLEEPING:
        return GateResult(blocked="睡眠中")
    if since_conversation is not None and since_conversation < CONVERSATION_COOLDOWN:
        return GateResult(blocked="会話した直後")

    triggers: dict[str, float] = {}
    if unprocessed >= MEMORY_MIN_UNPROCESSED:
        triggers[MEMORY_TRIGGER] = MEMORY_PROBABILITY

    speak_blocked = None
    if unanswered >= MAX_UNANSWERED:
        speak_blocked = f"返事のない話しかけが{unanswered}回続いている"
    elif since_proactive is not None and since_proactive < PROACTIVE_COOLDOWN * 2**unanswered:
        speak_blocked = "前回話しかけてから間もない"
    if not speak_blocked:
        factor = MESSAGE_ONLY_FACTOR if availability == AVAILABILITY_MESSAGE_ONLY else 1.0
        if state.boredom >= BOREDOM_MIN:
            triggers["boredom"] = BOREDOM_PROBABILITY_SCALE * state.boredom**2 * factor
        for key in crossings:
            if key in CROSSING_PROBABILITY:
                triggers[key] = CROSSING_PROBABILITY[key][_crossing_tag(key, personality)] * factor

    if not triggers:
        return GateResult(blocked=speak_blocked)
    probability = 1 - math.prod(1 - p for p in triggers.values())
    return GateResult(probability, triggers)


# --- 判定層 ---


@dataclass(frozen=True)
class Decision:
    action: str  # speak / silent / memory
    intent: str | None
    reason: str

    @property
    def speak(self) -> bool:
        return self.action == ACTION_SPEAK


def candidate_intents(triggers: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(i for t in triggers for i in TRIGGER_INTENTS.get(t, ())))


def _format_elapsed(delta: timedelta | None) -> str:
    if delta is None:
        return "まだ一度も話していない"
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes}分前" if minutes < 120 else f"約{minutes // 60}時間前"


def build_judge_messages(
    character: Character,
    state: CharacterState,
    availability: str,
    triggers: Iterable[str],
    since_conversation: timedelta | None,
    now: datetime,
    unprocessed: int = 0,
) -> list[dict[str, str]]:
    p = character.personality
    triggers = list(triggers)
    candidates = candidate_intents(triggers)
    allow_memory = MEMORY_TRIGGER in triggers
    actions = [
        *([f'"{ACTION_SPEAK}"（マスターに話しかける）'] if candidates else []),
        *([f'"{ACTION_MEMORY}"（ひとりで最近の会話を振り返り、記憶を整理する）'] if allow_memory else []),
        f'"{ACTION_SILENT}"（何もしない）',
    ]
    prompt = "\n".join(
        [
            f"{JUDGE_PREFIX}あなたはキャラクター「{character.name}」の行動判定係です。{character.name}が今どう過ごすかを判定してください。",
            "話しかけすぎは鬱陶しく、話しかけなさすぎは寂しい。性格と今の状態から、その人らしい判断をすること。",
            "",
            f"- 現在時刻：{now:%H:%M}",
            f"- 状態：暇度 {state.boredom:.2f}、疲労 {state.fatigue:.2f}、眠気 {state.sleepiness:.2f}（いずれも0〜1）",
            f"- 応答可能状態：{AVAILABILITY_LABELS[availability]}",
            f"- 性格：疲労反応＝{p.fatigue_response}、眠気反応＝{p.sleepiness_response}、暇度感度＝×{p.boredom_sensitivity}",
            f"- 最後にマスターと会話したのは：{_format_elapsed(since_conversation)}",
            f"- まだ振り返っていない会話：{unprocessed}件",
            f"- 今回のきっかけ：{'、'.join(TRIGGER_LABELS[t] for t in triggers)}",
            *([f"- 話しかける場合の意図の候補：{'／'.join(f'{i}（{INTENTS[i]}）' for i in candidates)}"] if candidates else []),
            f"- 選べる行動：{'、'.join(actions)}",
            "",
            'JSONのみで答えること：{"action": 選べる行動のいずれか, '
            '"intent": 話しかける場合は意図の候補の名前（それ以外は null）, "reason": "30字以内の理由"}',
        ]
    )
    return [{"role": "user", "content": prompt}]


async def judge(
    llm: LLMClient, messages: list[dict[str, str]], candidates: list[str], allow_memory: bool = False
) -> Decision:
    try:
        data: Any = await llm.complete_json(messages)
    except Exception as e:
        log.exception("judge failed")
        return Decision(ACTION_SILENT, None, f"判定層エラー：{type(e).__name__}")
    if not isinstance(data, dict):
        return Decision(ACTION_SILENT, None, "判定層の出力が不正")
    reason = str(data.get("reason") or "")
    action = data.get("action")
    if action == ACTION_SPEAK and candidates:
        intent = data.get("intent")
        return Decision(ACTION_SPEAK, intent if intent in candidates else candidates[0], reason)
    if action == ACTION_MEMORY and allow_memory:
        return Decision(ACTION_MEMORY, None, reason)
    return Decision(ACTION_SILENT, None, reason)


def proactive_instruction(intent: str, since_conversation: timedelta | None, seed: str | None = None) -> str:
    elapsed = (
        "マスターとはまだ話したことがない。"
        if since_conversation is None
        else f"最後にマスターと話したのは{_format_elapsed(since_conversation)}。"
    )
    topic = f"話題：前から話してみたかった「{seed}」について振ること。" if seed else ""
    return (
        f"ここはあなたから話しかける場面。意図：{INTENTS[intent]}。{elapsed}{topic}"
        "状況と今の調子に合った、自然な話しかけの一言を1〜2文で言うこと。この指示には触れないこと。"
    )


# --- 能動発話サービス ---


class ProactiveService:
    def __init__(
        self,
        db: Database,
        hub: EventHub,
        state: StateService,
        conversation: ConversationService,
        judge_llm: LLMClient,
        characters: Iterable[Character],
        interval_seconds: float = 60.0,
        dialogue_timeout_seconds: float = 180.0,
        rand: Callable[[], float] = random.random,
        memory: MemoryService | None = None,
    ):
        self._db = db
        self._hub = hub
        self._state = state
        self._conversation = conversation
        self._judge_llm = judge_llm
        self._memory = memory
        self._characters = {c.id: c for c in characters}
        self._interval = interval_seconds
        self._dialogue_timeout = dialogue_timeout_seconds
        self._rand = rand
        self._pending_crossings: dict[str, set[str]] = {cid: set() for cid in self._characters}
        self._proactive_times: dict[str, list[datetime]] = {cid: [] for cid in self._characters}
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        state.add_listener(self._on_state_events)

    # --- ループ ---

    async def start(self) -> None:
        if self._interval > 0:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in [self._task, *self._tasks]:
            if task:
                task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            for character_id in self._characters:
                try:
                    await self.evaluate(character_id)
                except Exception:
                    log.exception("proactive evaluation failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- 判定 ---

    def _since(self, message: dict[str, Any] | None, now: datetime) -> timedelta | None:
        return now - datetime.fromisoformat(message["created_at"]) if message else None

    def _unanswered(self, character_id: str) -> int:
        last_user = self._db.last_message(character_id, ("user",))
        since = datetime.fromisoformat(last_user["created_at"]) if last_user else None
        return sum(1 for t in self._proactive_times[character_id] if since is None or t > since)

    async def evaluate(self, character_id: str, force: bool = False) -> Decision | None:
        """1回分の判定。ゲートを通らなければ None。

        force=True は調整用：睡眠中以外はゲート（クールダウン・確率）を無視して判定層を呼ぶ。
        """
        character = self._characters[character_id]
        await self._state.refresh(character_id)  # 閾値クロスはリスナー経由で _pending_crossings に入る
        crossings = self._pending_crossings[character_id]
        self._pending_crossings[character_id] = set()

        now = self._state.now()
        availability = self._state.availability(character_id)
        since_conversation = self._since(self._db.last_message(character_id), now)
        unprocessed = len(self._memory.unprocessed(character_id)) if self._memory else 0
        times = self._proactive_times[character_id]
        result = gate(
            state=self._state.state(character_id),
            availability=availability,
            personality=character.personality,
            crossings=crossings,
            since_conversation=since_conversation,
            since_proactive=now - times[-1] if times else None,
            unanswered=self._unanswered(character_id),
            unprocessed=unprocessed,
        )
        if force and availability != AVAILABILITY_SLEEPING:
            triggers = {"boredom": BOREDOM_PROBABILITY_SCALE * self._state.state(character_id).boredom**2}
            if unprocessed:
                triggers[MEMORY_TRIGGER] = MEMORY_PROBABILITY
            result = GateResult(1.0, result.triggers | triggers)
        elif result.blocked or not result.triggers or self._rand() >= result.probability:
            return None

        candidates = candidate_intents(result.triggers)
        allow_memory = MEMORY_TRIGGER in result.triggers
        messages = build_judge_messages(
            character, self._state.state(character_id), availability, result.triggers, since_conversation, now, unprocessed
        )
        decision = await judge(self._judge_llm, messages, candidates, allow_memory)
        triggers = "、".join(f"{TRIGGER_LABELS[t]} {p:.2f}" for t, p in result.triggers.items())
        gate_note = "手動判定" if force else f"きっかけ：{triggers}／通過確率 {result.probability:.2f}"
        verdict = {
            ACTION_SPEAK: f"発話する（{decision.intent}）",
            ACTION_MEMORY: "記憶調整",
            ACTION_SILENT: "沈黙",
        }[decision.action]
        await self._state.record(character_id, "judgment", f"{verdict}：{decision.reason}　［{gate_note}］")
        if decision.action == ACTION_SPEAK:
            await self.speak(character_id, decision.intent, since_conversation)
        elif decision.action == ACTION_MEMORY and self._memory:
            await self._memory.adjust(character_id)
        return decision

    # --- 発話 ---

    def _session_id(self, character_id: str, kind: str) -> int:
        return next(s["id"] for s in self._db.list_sessions(character_id) if s["kind"] == kind)

    async def _new_sub_session(self, character_id: str) -> int:
        session = await self._conversation.create_sub_session(character_id, NEW_SUB_TITLE)
        return session["id"]

    async def _name_sub_session(self, session_id: int, content: str) -> None:
        """話しかけの内容から、サブチャンネルの名前を判定層のモデルに付けさせる。"""
        prompt = (
            f"{TITLE_PREFIX}次の話しかけの話題を表す、チャット用のチャンネル名を{SUB_TITLE_MAX}字以内で付けてください。"
            f"記号や絵文字は使わない。\n\n話しかけ：{content}\n\n"
            'JSONのみで答えること：{"title": "チャンネル名"}'
        )
        try:
            data = await self._judge_llm.complete_json([{"role": "user", "content": prompt}])
            title = str(data.get("title") or "").strip()
        except Exception:
            log.exception("naming sub session failed")
            title = ""
        await self._conversation.update_session(session_id, title=(title or content)[:SUB_TITLE_MAX])

    async def speak(
        self,
        character_id: str,
        intent: str,
        since_conversation: timedelta | None = None,
        session_id: int | None = None,
    ) -> dict[str, Any] | None:
        """話しかける。session_id を省略すると送り先を選ぶ：

        - 両方対応できる → 対話
        - それ以外（メッセージ）→ 会話の種を使うなら #main、即興なら新しいサブスレッド（仕様4-1）
        """
        character = self._characters[character_id]
        # 会話の種を1つ選ぶ（仕様6-4：選ぶのはコード）。無ければ即興
        seed = self._memory.pick_seed(character_id) if self._memory and intent != FAREWELL else None
        new_sub = False
        if session_id is None:
            if self._state.availability(character_id) == AVAILABILITY_BOTH:
                session_id = self._session_id(character_id, "dialogue")
            elif seed:
                session_id = self._session_id(character_id, "main")
            else:
                session_id = await self._new_sub_session(character_id)
                new_sub = True
        mode = "dialogue" if self._db.get_session(session_id)["kind"] == "dialogue" else "message"

        await self._hub.publish(
            "proactive.started", character_id=character_id, session_id=session_id, mode=mode, intent=intent
        )
        if intent == FAREWELL:
            instruction = FAREWELL_INSTRUCTION
        else:
            instruction = proactive_instruction(intent, since_conversation, seed["content"] if seed else None)
        message = await self._conversation.speak(
            character, session_id, instruction, topic=seed["content"] if seed else None
        )
        if message is None:
            return None
        if new_sub:
            await self._name_sub_session(session_id, message["content"])

        self._proactive_times[character_id].append(self._state.now())
        if seed:
            await self._memory.mark_seed_used(character_id, seed["id"])
        if intent != FAREWELL:
            await self._state.on_proactive(character_id)
        topic = f"・種：{seed['content']}" if seed else "・即興"
        await self._state.record(
            character_id,
            "proactive",
            f"{'対話' if mode == 'dialogue' else 'メッセージ'}で話しかけた（{intent}{'' if intent == FAREWELL else topic}）",
        )
        if mode == "dialogue" and intent != FAREWELL:
            self._spawn(self._fallback_if_unanswered(character_id, session_id, message, used_seed=seed is not None))
        return message

    async def _fallback_if_unanswered(
        self, character_id: str, session_id: int, message: dict[str, Any], used_seed: bool
    ) -> None:
        """対話で話しかけて返事がなければ、同じ内容をメッセージとして送り直す（仕様4-1）。

        送り先はメッセージの能動発話と同じ基準：会話の種を使っていたら #main、即興なら新しいサブスレッド。
        """
        await asyncio.sleep(self._dialogue_timeout)
        if self._db.has_user_message_after(session_id, message["id"]):
            return
        if used_seed:
            target = self._session_id(character_id, "main")
        else:
            target = await self._new_sub_session(character_id)
        await self._conversation.post_character_message(target, message["content"])
        if not used_seed:
            await self._name_sub_session(target, message["content"])
        await self._hub.publish("dialogue.fallback", character_id=character_id, session_id=session_id)
        where = "#main" if used_seed else "新しいサブチャンネル"
        await self._state.record(character_id, "proactive", f"対話に返事がなかったので、{where}に送り直した")

    # --- 状態イベント ---

    async def _on_state_events(self, character_id: str, events: list[StateEvent]) -> None:
        for event in events:
            if event.kind == "crossing" and event.key:
                self._pending_crossings[character_id].add(event.key)
        sleep = next((e for e in events if e.kind == "sleep"), None)
        if sleep:
            self._spawn(self._fall_asleep(character_id, sleep))

    async def _fall_asleep(self, character_id: str, sleep: StateEvent) -> None:
        last_user = self._db.last_message(character_id, ("user",))
        last_user_at = datetime.fromisoformat(last_user["created_at"]) if last_user else None
        if should_say_goodnight(datetime.fromisoformat(sleep.at), last_user_at):
            # 会話中に眠りに落ちるときは、判定層を通さずにおやすみを言って切り上げる（仕様6-3）
            await self.speak(character_id, FAREWELL, session_id=last_user["session_id"])
        await self._conversation.archive_stale_sessions(character_id)
        if self._memory:
            # 睡眠中に夜間蒸留（仕様8-2）。おやすみの一言も含めるため、その後に行う
            try:
                await self._memory.distill(character_id)
            except Exception:
                log.exception("distillation failed")
                await self._state.record(character_id, "memory", "夜間蒸留に失敗した（ログを参照）")
