"""記憶の確認パネル（状態確認ビューの「記憶」タブ）。

プロフィール（印象層）・印象メモ・会話の種・エピソード記憶（アクセス回数・削除猶予・時限つき）を表示する。
記憶調整や睡眠中の整理の結果をチューニングするための画面。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engawa.ui.client import CoreClient
from engawa.ui.widgets import COLORS, format_time

SUBJECT_LABELS = {"user": "あなたについて", "self": "自分について"}
SEED_SOURCES = {"recall": "思い出し", "topic": "話題作成"}
SESSION_LABELS = {"dialogue": "対話", "main": "#main", "sub": "サブ"}


def _days_left(iso: str) -> str:
    days = (datetime.fromisoformat(iso) - datetime.now().astimezone()).total_seconds() / 86400
    return f"{days:.0f}日" if days >= 1 else ("あと少し" if days > 0 else "切れ")


def _box(title: str, widget: QWidget) -> QGroupBox:
    box = QGroupBox(title)
    QVBoxLayout(box).addWidget(widget)
    return box


def _list() -> QListWidget:
    widget = QListWidget()
    widget.setWordWrap(True)
    return widget


class MemoryPanel(QWidget):
    def __init__(self, client: CoreClient, character: dict):
        super().__init__()
        self._client = client
        self._character_id = character["id"]

        self._summary = QLabel("―")
        self._summary.setStyleSheet(f"color: {COLORS['text_muted']};")
        self._summary.setWordWrap(True)
        self._adjust_button = QPushButton("記憶調整を実行")
        self._adjust_button.setToolTip("調整用：判定層を通さずに、まだ記憶にしていない会話をエピソード化する")
        self._adjust_button.clicked.connect(lambda: self._run("adjust", self._adjust_button))
        self._consolidate_button = QPushButton("睡眠中の整理を実行")
        self._consolidate_button.setToolTip(
            "調整用：睡眠を待たずに、残りの会話をすべてエピソード化し、印象メモから印象を書き直す"
        )
        self._consolidate_button.clicked.connect(lambda: self._run("consolidate", self._consolidate_button))
        header = QHBoxLayout()
        header.addWidget(self._summary, 1)
        header.addWidget(self._adjust_button)
        header.addWidget(self._consolidate_button)

        profile_box = QGroupBox("プロフィール（印象層：常にプロンプトに入る）")
        profile_layout = QVBoxLayout(profile_box)
        self._profiles: dict[str, tuple[QLabel, QPlainTextEdit]] = {}
        for subject, label in SUBJECT_LABELS.items():
            info = QLabel(label)
            info.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 9pt;")
            text = QPlainTextEdit(readOnly=True)
            profile_layout.addWidget(info)
            profile_layout.addWidget(text)
            self._profiles[subject] = (info, text)

        self._memos = _list()
        self._seeds = _list()
        self._episodes = _list()

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(profile_box)
        splitter.addWidget(_box("印象メモ（睡眠中に印象へまとめて消える）", self._memos))
        splitter.addWidget(_box("会話の種", self._seeds))
        splitter.addWidget(_box("エピソード記憶　［アクセス回数］削除猶予／時限", self._episodes))
        splitter.setSizes([260, 90, 90, 260])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addLayout(header)
        layout.addWidget(splitter, 1)

        client.event_received.connect(self._on_event)
        self.reload()

    def reload(self) -> None:
        self._client.get(f"/characters/{self._character_id}/memory", self.apply)

    def _run(self, action: str, button: QPushButton) -> None:
        button.setEnabled(False)
        self._client.post(
            f"/characters/{self._character_id}/memory/{action}",
            {},
            lambda _: button.setEnabled(True),
            lambda status, detail: button.setEnabled(True),
        )

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "data.replaced" or (
            event.get("type") == "memory.updated" and event.get("character_id") == self._character_id
        ):
            self.reload()

    def apply(self, memory: dict) -> None:
        rules = memory["rules"]
        episodes = memory["episodes"]
        deletable = sum(1 for e in episodes if e["deletable"])
        self._summary.setText(
            f"まだ記憶にしていない発言 {memory['unprocessed']}件　／　"
            f"エピソード {len(episodes)} / {rules['episode_limit']}件（うち削除対象 {deletable}件）"
        )
        similarity = "・".join(f"{SESSION_LABELS.get(k, k)} {v}" for k, v in rules["retrieve_min_similarity"].items())
        self._summary.setToolTip(
            f"削除猶予：{rules['grace_days'][0]}〜{rules['grace_days'][1]}日（文字数で変わる）・時限 {rules['lifetime_days']}日。"
            "思い出されると両方延びる\n"
            f"検索：上位{rules['retrieve_top_k']}件・類似度 {similarity} 以上\n"
            f"会話の種の期限{rules['seed_ttl_days']}日・種があっても{rules['impromptu_probability']:.0%}の確率で即興"
        )

        for subject, (info, text) in self._profiles.items():
            profile = memory["profiles"].get(subject)
            label = SUBJECT_LABELS[subject]
            if profile:
                info.setText(
                    f"{label}　更新 {format_time(profile['updated_at'])}　"
                    f"{len(profile['impression'])} / {memory['profile_max_chars']}字"
                )
                text.setPlainText(profile["impression"])
            else:
                info.setText(f"{label}　まだ無い（眠りに落ちたときに、印象メモから作られる）")
                text.clear()

        self._memos.clear()
        for memo in memory["memos"]:
            self._memos.addItem(f"{format_time(memo['created_at'])}  {SUBJECT_LABELS[memo['subject']]}：{memo['content']}")
        if not memory["memos"]:
            self._memos.addItem("（まだありません）")

        self._seeds.clear()
        for seed in reversed(memory["seeds"]):
            used = f"　✓使用済み {format_time(seed['used_at'])}" if seed["used_at"] else ""
            source = SEED_SOURCES.get(seed["source"], seed["source"])
            self._seeds.addItem(f"{format_time(seed['created_at'])}  ［{source}］{seed['content']}{used}")
        if not memory["seeds"]:
            self._seeds.addItem("（まだありません）")

        self._episodes.clear()
        for episode in episodes:
            grace = "削除対象" if episode["deletable"] else _days_left(episode["grace_until"])
            flags = "" if episode["embedded"] else "　（埋め込み未計算）"
            self._episodes.addItem(
                f"［{episode['access_count']}］{grace}／{_days_left(episode['expires_at'])}　"
                f"{format_time(episode['happened_at'])}  {episode['content']}{flags}"
            )
        if not episodes:
            self._episodes.addItem("（まだありません）")
