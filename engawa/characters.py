"""キャラクター定義。

MVPでは翠1体をハードコードする（仕様6-2）。複数キャラクター対応（仕様6-5）に備え、
レジストリ形式で持たせておく。性格タグ・生活リズムプロファイルは次段階でここに追加する。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PERSONA_DIR = Path(__file__).resolve().parent / "persona"


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    persona_file: str

    def system_prompt(self) -> str:
        return (PERSONA_DIR / self.persona_file).read_text(encoding="utf-8")


CHARACTERS: dict[str, Character] = {
    "sui": Character(id="sui", name="翠", persona_file="sui.md"),
}


def get_character(character_id: str) -> Character | None:
    return CHARACTERS.get(character_id)
