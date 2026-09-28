"""記憶の確認パネル（状態確認ビューの「記憶」タブ）。

LTM・会話の種・STM（参照回数つき）を表示する。蒸留や記憶調整の結果をチューニングするための画面。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from engawa.ui.client import CoreClient
from engawa.ui.widgets import COLORS, format_time

SPEAKER_LABELS = {"user": "マスター", "character": "キャラ"}


class MemoryPanel(QWidget):
    def __init__(self, client: CoreClient, character: dict):
        super().__init__()
        self._client = client
        self._character_id = character["id"]
        self._character_name = character["name"]

        self._summary = QLabel("―")
        self._summary.setStyleSheet(f"color: {COLORS['text_muted']};")
        self._summary.setWordWrap(True)
        self._adjust_button = QPushButton("記憶調整を実行")
        self._adjust_button.setToolTip("調整用：判定層を通さずに、最近の会話から会話の種と自己言及記憶を作る")
        self._adjust_button.clicked.connect(lambda: self._run("adjust", self._adjust_button))
        self._distill_button = QPushButton("蒸留を実行")
        self._distill_button.setToolTip("調整用：睡眠を待たずに夜間蒸留を行う（その日のSTMは上位だけ残して削除される）")
        self._distill_button.clicked.connect(lambda: self._run("distill", self._distill_button))
        header = QHBoxLayout()
        header.addWidget(self._summary, 1)
        header.addWidget(self._adjust_button)
        header.addWidget(self._distill_button)

        ltm_box = QGroupBox("長期記憶（LTM）")
        ltm_layout = QVBoxLayout(ltm_box)
        self._ltm_info = QLabel()
        self._ltm_info.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 9pt;")
        self._ltm_conversation = QPlainTextEdit(readOnly=True)
        self._ltm_self = QPlainTextEdit(readOnly=True)
        ltm_layout.addWidget(self._ltm_info)
        ltm_layout.addWidget(QLabel("マスターとのこと（会話由来）"))
        ltm_layout.addWidget(self._ltm_conversation)
        ltm_layout.addWidget(QLabel("自分のこと（生活由来）"))
        ltm_layout.addWidget(self._ltm_self)

        seeds_box = QGroupBox("会話の種")
        self._seeds = QListWidget()
        self._seeds.setWordWrap(True)
        self._seeds.setMinimumHeight(110)
        QVBoxLayout(seeds_box).addWidget(self._seeds)

        stm_box = QGroupBox("短期記憶（STM）　［参照回数］")
        self._stm = QListWidget()
        self._stm.setWordWrap(True)
        QVBoxLayout(stm_box).addWidget(self._stm)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addLayout(header)
        layout.addWidget(ltm_box, 2)
        layout.addWidget(seeds_box, 1)
        layout.addWidget(stm_box, 2)

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
        if event.get("type") == "memory.updated" and event.get("character_id") == self._character_id:
            self.reload()

    def apply(self, memory: dict) -> None:
        rules = memory["rules"]
        self._summary.setText(
            f"未整理の会話 {memory['unprocessed']}件・画面の活動 {memory['unprocessed_activity']}件　／　STM {len(memory['stm'])}件"
        )
        self._summary.setToolTip(
            f"STMの期限{rules['stm_ttl_days']}日・蒸留後は1日{rules['stm_keep_per_day']}件まで\n"
            f"会話の種の期限{rules['seed_ttl_days']}日・種があっても{rules['impromptu_probability']:.0%}の確率で即興\n"
            f"想起：上位{rules['retrieve_top_k']}件・類似度{rules['retrieve_min_similarity']}以上"
        )

        ltm = memory["ltm"]
        if ltm:
            chars = len(ltm["conversation"]) + len(ltm["self"])
            self._ltm_info.setText(f"更新 {format_time(ltm['updated_at'])}　{chars} / {memory['ltm_max_chars']}字")
            self._ltm_conversation.setPlainText(ltm["conversation"])
            self._ltm_self.setPlainText(ltm["self"])
        else:
            self._ltm_info.setText("まだ蒸留されていない（眠りに落ちたときに作られる）")
            self._ltm_conversation.clear()
            self._ltm_self.clear()

        self._seeds.clear()
        for seed in reversed(memory["seeds"]):
            used = f"　✓使用済み {format_time(seed['used_at'])}" if seed["used_at"] else ""
            self._seeds.addItem(f"{format_time(seed['created_at'])}  {seed['content']}{used}")
        if not memory["seeds"]:
            self._seeds.addItem("（まだありません）")

        self._stm.clear()
        for item in memory["stm"]:
            if item["kind"] == "self":
                source = "自己言及"
            else:
                source = self._character_name if item["speaker"] == "character" else SPEAKER_LABELS["user"]
            flags = "" if item["embedded"] else "　（埋め込み未計算）"
            flags += "　蒸留済み" if item["distilled"] else ""
            self._stm.addItem(f"［{item['ref_count']}］{format_time(item['created_at'])}  {source}：{item['content']}{flags}")
        if not memory["stm"]:
            self._stm.addItem("（まだありません）")
