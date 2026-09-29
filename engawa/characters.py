"""キャラクター定義。

MVPでは翠1体をハードコードする（仕様6-2）。複数キャラクター対応（仕様6-5）に備え、
レジストリ形式で持たせておく。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PERSONA_DIR = Path(__file__).resolve().parent / "persona"
ASSET_DIR = Path(__file__).resolve().parent / "assets"

DEFAULT_EXPRESSION = "normal"
# 生成層に表情タグを選ばせるときの説明。立ち絵が登録されている表情だけを伝える
EXPRESSION_DESCRIPTIONS = {
    "normal": "ふだん・落ち着いている",
    "think": "考え込む・悩む・疑問に思う・呆れる",
    "joy": "嬉しい・楽しい・機嫌がいい",
    "blush": "照れる・恥ずかしい・図星を突かれる",
}

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
class CharacterImages:
    """見た目の画像。パスは ASSET_DIR からの相対パス（区切りは /）。

    standing は表情名 → 立ち絵。表情の切り替え・キャラクターごとのディレクトリ分けは、
    ここの対応を書き換えるだけで済むようにしておく。
    """

    icon: str | None = None  # キャラクター一覧・チャットの発言に出す
    standing: dict[str, str] = field(default_factory=dict)  # 対話ウィンドウに出す


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    persona_file: str
    personality: Personality = field(default_factory=Personality)
    rhythm: RhythmProfile = field(default_factory=lambda: RhythmProfile(wake_hour=8.0, bedtime_hour=24.0))
    images: CharacterImages = field(default_factory=CharacterImages)

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
        images=CharacterImages(
            icon="icon/sui_chibi_normal.png",
            standing={
                expression: f"standing/sui_standing_{expression}.png"
                # blush は背景が透過されていない画像のため、差し替えるまで使わない
                for expression in (DEFAULT_EXPRESSION, "think", "joy")
            },
        ),
    ),
}


def get_character(character_id: str) -> Character | None:
    return CHARACTERS.get(character_id)
