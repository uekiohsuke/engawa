"""キャラクター定義。

MVPでは翠1体をハードコードする（仕様6-2）。複数キャラクター対応（仕様6-5）に備え、
レジストリ形式で持たせておく。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PERSONA_DIR = Path(__file__).resolve().parent / "persona"

FATIGUE_RESPONSES = ("静か", "普通", "構ってほしい")
SLEEPINESS_RESPONSES = ("あっさり寝る", "軽く挨拶", "会話を求める")


@dataclass(frozen=True)
class RhythmProfile:
    """生活リズムプロファイル（仕様6-2）。時刻は 0〜24 を超えてもよい（25.0 = 翌1時）。"""

    wake_hour: float
    bedtime_hour: float
    # 「何かしている」時間帯。この間はメッセージのみ対応になり、暇度が溜まりにくい
    busy_blocks: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class Personality:
    """性格パラメータ（仕様6-1）。現時点では固定。"""

    boredom_sensitivity: float = 1.0  # 暇度の溜まりやすさ（0.5〜2.0倍）
    fatigue_response: str = "普通"  # FATIGUE_RESPONSES
    sleepiness_response: str = "軽く挨拶"  # SLEEPINESS_RESPONSES


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    persona_file: str
    personality: Personality = field(default_factory=Personality)
    rhythm: RhythmProfile = field(default_factory=lambda: RhythmProfile(wake_hour=8.0, bedtime_hour=24.0))

    def system_prompt(self) -> str:
        return (PERSONA_DIR / self.persona_file).read_text(encoding="utf-8")


CHARACTERS: dict[str, Character] = {
    "sui": Character(
        id="sui",
        name="翠",
        persona_file="sui.md",
        personality=Personality(
            boredom_sensitivity=1.0,
            fatigue_response="静か",
            sleepiness_response="会話を求める",
        ),
        # 8時起床・25時（翌1時）就寝。何かしている時間帯（14〜17時）は仮決め
        rhythm=RhythmProfile(wake_hour=8.0, bedtime_hour=25.0, busy_blocks=((14.0, 17.0),)),
    ),
}


def get_character(character_id: str) -> Character | None:
    return CHARACTERS.get(character_id)
