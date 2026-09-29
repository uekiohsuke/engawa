"""表情タグ：生成層が返事の先頭に付ける [joy] などを、ストリームから取り除いて表情として取り出す。

タグは立ち絵を切り替えるためのもので、本文・読み上げ・記憶には残さない。
"""

from __future__ import annotations

from collections.abc import Iterable

from engawa.characters import EXPRESSION_DESCRIPTIONS

TAG_MAX_LENGTH = 16  # "[" から "]" まで。これより長く閉じないものはタグではないとみなす


def expression_section(expressions: Iterable[str]) -> str | None:
    """システムプロンプトに入れる表情タグの指示。表情が1つしかなければ不要。"""
    expressions = list(expressions)
    if len(expressions) < 2:
        return None
    lines = [
        "## 表情",
        "返事の一番最初に、そのときの表情をタグで1つだけ付けること（例：[joy] そっか、良かったじゃん。）。",
        "タグは画面の立ち絵を切り替えるためのもので、マスターには見えない。本文の途中や最後には付けないこと。",
        *(f"- [{e}]：{EXPRESSION_DESCRIPTIONS.get(e, e)}" for e in expressions),
    ]
    return "\n".join(lines)


class ExpressionFilter:
    """ストリームの断片から表情タグを取り除く。最初に見つけたタグをその返事の表情とする。

    "[" が来たら、タグかどうか分かるまで（"]" が来るか、長すぎるまで）出力を保留する。
    知らない名前の [..] は本文としてそのまま流す。
    """

    def __init__(self, expressions: Iterable[str]):
        self._expressions = set(expressions)
        self._pending = ""
        self._started = False
        self._after_tag = False
        self.expression: str | None = None

    def feed(self, delta: str) -> str:
        self._pending += delta
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, final: bool) -> str:
        out: list[str] = []
        while self._pending:
            if self._after_tag:
                # タグの直後の空白も一緒に取り除く（空白がまだ届いていなければ、次の断片で）
                self._pending = self._pending.lstrip(" 　\t")
                self._after_tag = not self._pending
                continue
            start = self._pending.find("[")
            if start < 0:
                out.append(self._pending)
                self._pending = ""
                break
            out.append(self._pending[:start])
            self._pending = self._pending[start:]
            end = self._pending.find("]", 0, TAG_MAX_LENGTH)
            if end < 0:
                if len(self._pending) < TAG_MAX_LENGTH and not final:
                    break  # タグかどうか、まだ分からない
                out.append("[")
                self._pending = self._pending[1:]
                continue
            name = self._pending[1:end].strip().lower()
            if name in self._expressions:
                self.expression = self.expression or name
                self._pending = self._pending[end + 1 :]
                self._after_tag = True
            else:
                out.append("[")
                self._pending = self._pending[1:]
        text = "".join(out)
        if not self._started:
            text = text.lstrip()  # タグの後の空白・改行
            self._started = bool(text)
        return text
