"""内部状態モデル（仕様6章）。IO を持たない純粋なロジック。

- 暇度：会話がない時間に応じて増え、会話するたびに下がる。性格の感度係数と生活リズムで補正する
- 疲労：起きている時間に応じて溜まり、会話で少し回復する。睡眠中に回復し、起床時にリセットされる
- 眠気：生活リズム（就寝時刻までの残り時間）で決まるカーブに、疲労を少し上乗せしたもの
- 睡眠：眠気が睡眠の閾値を超えたら就寝し、起床時刻になったら起きる

生成層（LLM）による会話内容に応じた増減（仕様6-4）は次段階で追加する。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from engawa.characters import Character, RhythmProfile
from engawa.core.timewords import describe_now

# --- 閾値 ---
BOREDOM_HIGH = 0.7
FATIGUE_HIGH = 0.6
SLEEPINESS_PRE_SLEEP = 0.6  # 就寝前会話イベントの閾値
SLEEPINESS_SLEEP = 0.9  # 睡眠の閾値

# --- 変化速度 ---
BOREDOM_GAIN_PER_HOUR = 1 / 3  # 活動時間帯なら約3時間で最大
BOREDOM_BUSY_FACTOR = 0.3  # 何かしている時間帯は溜まりにくい
BOREDOM_DECAY_ASLEEP_PER_HOUR = 0.1
BOREDOM_PER_MESSAGE = -0.15
BOREDOM_PER_PROACTIVE = -0.1
FATIGUE_GAIN_PER_HOUR = 0.04  # 起床から就寝（17時間）で約0.7
FATIGUE_RECOVERY_ASLEEP_PER_HOUR = 0.1
FATIGUE_PER_MESSAGE = -0.02
SLEEPINESS_FATIGUE_WEIGHT = 0.1

MAX_STEP = timedelta(minutes=5)  # まとめて時間を進めるときの刻み
MAX_CATCH_UP = timedelta(days=7)

AVAILABILITY_BOTH = "both"
AVAILABILITY_MESSAGE_ONLY = "message_only"
AVAILABILITY_SLEEPING = "sleeping"
AVAILABILITY_LABELS = {
    AVAILABILITY_BOTH: "起きている",
    AVAILABILITY_MESSAGE_ONLY: "取り込み中",
    AVAILABILITY_SLEEPING: "睡眠中",
}

# 上向きの閾値クロスとして記録するもの（キー, 閾値, 説明）。次段階の確率的ゲートはここに接続する
CROSSINGS = (
    ("boredom", BOREDOM_HIGH, "暇度が高くなった"),
    ("fatigue", FATIGUE_HIGH, "疲労が高くなった"),
    ("sleepiness", SLEEPINESS_PRE_SLEEP, "就寝前会話の閾値を超えた"),
)


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def day_hour(now: datetime, rhythm: RhythmProfile) -> float:
    """起床時刻を起点にした「その日」の時刻。起床前の深夜は 24 を足す（例：1時 → 25.0）。"""
    h = now.hour + now.minute / 60 + now.second / 3600
    return h + 24 if h < rhythm.wake_hour else h


def in_busy_block(h: float, rhythm: RhythmProfile) -> bool:
    return any(start <= h < end for start, end in rhythm.busy_blocks)


def is_wake_window(h: float, rhythm: RhythmProfile) -> bool:
    """起きているべき時間帯（起床〜就寝3時間前）。この間に眠っていたら起きる。"""
    return rhythm.wake_hour <= h < rhythm.bedtime_hour - 3


def sleepiness_curve(h: float, rhythm: RhythmProfile, fatigue: float) -> float:
    wake, bed = rhythm.wake_hour, rhythm.bedtime_hour
    if h < wake + 1:
        base = 0.35 - 0.25 * (h - wake)  # 寝起き
    elif h < bed - 3:
        base = 0.1
    elif h < bed:
        base = 0.1 + 0.75 * (h - (bed - 3)) / 3
    else:
        base = 0.85 + 0.15 * (h - bed)
    return _clamp(base + SLEEPINESS_FATIGUE_WEIGHT * fatigue)


@dataclass
class CharacterState:
    boredom: float = 0.0
    fatigue: float = 0.0
    sleepiness: float = 0.0
    asleep: bool = False
    updated_at: str | None = None  # ISO 8601

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CharacterState:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass(frozen=True)
class StateEvent:
    kind: str  # crossing / sleep / wake
    detail: str
    at: str
    key: str | None = None  # crossing のときの状態量（boredom / fatigue / sleepiness）


def _level(value: float, labels: tuple[str, str, str, str]) -> str:
    for threshold, label in zip((0.3, 0.6, 0.85), labels):
        if value < threshold:
            return label
    return labels[3]


FATIGUE_HINTS = {
    "静か": "疲れているので、いつもより口数が少なく、短く淡々と返す。",
    "普通": "疲れていることが、口調や内容に少しだけ滲む。",
    "構ってほしい": "疲れているので、それとなく疲れたアピールをして労ってもらいたがる。",
}
SLEEPINESS_HINTS = {
    "あっさり寝る": "眠いので、会話はそっけなく切り上げたがる。",
    "軽く挨拶": "眠いので、長話はせず、おやすみの一言くらいは言いたい。",
    "会話を求める": "眠いけれど、寝る前にもう少しマスターと話していたい。",
}


class StateEngine:
    def __init__(self, character: Character, state: CharacterState | None, now: datetime):
        self.character = character
        self.state = state or CharacterState()
        if self.state.updated_at is None:
            self.state.sleepiness = sleepiness_curve(day_hour(now, character.rhythm), character.rhythm, 0.0)
            self.state.updated_at = now.isoformat()

    @property
    def rhythm(self) -> RhythmProfile:
        return self.character.rhythm

    def availability(self, now: datetime, focus_mode: bool = False) -> str:
        if self.state.asleep:
            return AVAILABILITY_SLEEPING
        # 集中モード中は生活リズムに関わらずメッセージのみ（仕様5章）
        if focus_mode or in_busy_block(day_hour(now, self.rhythm), self.rhythm):
            return AVAILABILITY_MESSAGE_ONLY
        return AVAILABILITY_BOTH

    def tick(self, now: datetime) -> list[StateEvent]:
        """前回更新から now まで時間を進める。長時間空いた場合は刻んで追いつく。"""
        last = datetime.fromisoformat(self.state.updated_at)
        if now <= last:
            return []
        t = max(last, now - MAX_CATCH_UP)
        events: list[StateEvent] = []
        while t < now:
            step_end = min(now, t + MAX_STEP)
            events += self._advance(step_end, (step_end - t).total_seconds() / 3600)
            t = step_end
        self.state.updated_at = now.isoformat()
        return events

    def _advance(self, t: datetime, hours: float) -> list[StateEvent]:
        s = self.state
        h = day_hour(t, self.rhythm)
        before = s.to_dict()
        events: list[StateEvent] = []
        if s.asleep:
            s.boredom = _clamp(s.boredom - BOREDOM_DECAY_ASLEEP_PER_HOUR * hours)
            s.fatigue = _clamp(s.fatigue - FATIGUE_RECOVERY_ASLEEP_PER_HOUR * hours)
            if is_wake_window(h, self.rhythm):
                s.asleep = False
                s.fatigue = 0.0
                events.append(StateEvent("wake", "起床した（疲労が回復）", t.isoformat()))
        else:
            factor = BOREDOM_BUSY_FACTOR if in_busy_block(h, self.rhythm) else 1.0
            gain = BOREDOM_GAIN_PER_HOUR * self.character.personality.boredom_sensitivity * factor
            s.boredom = _clamp(s.boredom + gain * hours)
            s.fatigue = _clamp(s.fatigue + FATIGUE_GAIN_PER_HOUR * hours)
        s.sleepiness = sleepiness_curve(h, self.rhythm, s.fatigue)
        events += self._crossings(before, t)
        if not s.asleep and s.sleepiness >= SLEEPINESS_SLEEP:
            s.asleep = True
            events.append(StateEvent("sleep", "眠気が睡眠の閾値を超えて就寝した", t.isoformat()))
        return events

    def _crossings(self, before: dict[str, Any], t: datetime) -> list[StateEvent]:
        return [
            StateEvent("crossing", f"{detail}（{threshold}）", t.isoformat(), key)
            for key, threshold, detail in CROSSINGS
            if before[key] < threshold <= getattr(self.state, key)
        ]

    def on_user_message(self, now: datetime) -> list[StateEvent]:
        """ユーザーとの会話による増減。"""
        events = self.tick(now)
        self.state.boredom = _clamp(self.state.boredom + BOREDOM_PER_MESSAGE)
        self.state.fatigue = _clamp(self.state.fatigue + FATIGUE_PER_MESSAGE)
        return events

    def on_proactive(self, now: datetime) -> list[StateEvent]:
        """自分から話しかけたことによる増減（返事がなくても少しは気が紛れる）。"""
        events = self.tick(now)
        self.state.boredom = _clamp(self.state.boredom + BOREDOM_PER_PROACTIVE)
        return events

    def prompt_section(self, now: datetime) -> str:
        """システムプロンプトに注入する「現在の状態」セクション（仕様6章）。"""
        s = self.state
        p = self.character.personality
        lines = [
            "## 現在の状態（あなた自身の今の調子。数値や「状態」という言葉は口にせず、振る舞いに自然に表すこと）",
            f"- 現在時刻：{describe_now(now)}",
            f"- 眠気：{_level(s.sleepiness, ('眠くない', '少し眠い', 'かなり眠い', 'もう限界に近いくらい眠い'))}",
            f"- 疲れ：{_level(s.fatigue, ('元気', '少し疲れている', '疲れている', 'へとへと'))}",
            f"- 暇さ：{_level(s.boredom, ('特に暇ではない', '少し暇', '暇を持て余している', '退屈でたまらない'))}",
        ]
        if s.fatigue >= FATIGUE_HIGH:
            lines.append(f"- {FATIGUE_HINTS[p.fatigue_response]}")
        if s.sleepiness >= SLEEPINESS_PRE_SLEEP:
            lines.append(f"- {SLEEPINESS_HINTS[p.sleepiness_response]}")
        return "\n".join(lines)

    def snapshot(self, now: datetime, focus_mode: bool = False) -> dict[str, Any]:
        p = self.character.personality
        availability = self.availability(now, focus_mode)
        label = AVAILABILITY_LABELS[availability]
        if focus_mode and availability == AVAILABILITY_MESSAGE_ONLY:
            label += "（集中モード）"
        return {
            "character_id": self.character.id,
            "values": {"boredom": self.state.boredom, "fatigue": self.state.fatigue, "sleepiness": self.state.sleepiness},
            "asleep": self.state.asleep,
            "availability": availability,
            "availability_label": label,
            "focus_mode": focus_mode,
            "thresholds": {
                "boredom": {"高い": BOREDOM_HIGH},
                "fatigue": {"高い": FATIGUE_HIGH},
                "sleepiness": {"就寝前会話": SLEEPINESS_PRE_SLEEP, "睡眠": SLEEPINESS_SLEEP},
            },
            "personality": {
                "boredom_sensitivity": p.boredom_sensitivity,
                "fatigue_response": p.fatigue_response,
                "sleepiness_response": p.sleepiness_response,
            },
            "rhythm": {
                "wake_hour": self.rhythm.wake_hour,
                "bedtime_hour": self.rhythm.bedtime_hour,
                "busy_blocks": [list(b) for b in self.rhythm.busy_blocks],
            },
            "prompt_section": self.prompt_section(now),
            "updated_at": self.state.updated_at,
        }
