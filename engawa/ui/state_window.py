"""状態確認ビュー（仕様5章）。

内部状態の数値・閾値・応答可能状態・性格タグ・生活リズム・プロンプトに注入される状態・状態イベントを表示する。
「なぜ今そう振る舞ったか」を確認するための恒常的な機能として位置づける。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from engawa.ui.client import CoreClient
from engawa.ui.widgets import COLORS, format_time

STATE_STYLE = f"""
QWidget#stateWindow {{ background: {COLORS['bg_chat']}; }}
QGroupBox {{ border: 1px solid {COLORS['bg_hover']}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; color: {COLORS['text_muted']}; font-weight: bold; }}
QProgressBar {{ background: {COLORS['bg_input']}; border: none; border-radius: 4px; height: 10px; text-align: center; }}
QProgressBar::chunk {{ background: {COLORS['character']}; border-radius: 4px; }}
QPlainTextEdit {{ font-size: 9.5pt; }}
#availability {{ font-size: 12pt; font-weight: bold; }}
"""

VALUE_LABELS = {"boredom": "暇度", "fatigue": "疲労", "sleepiness": "眠気"}
AVAILABILITY_COLORS = {"both": "#23a55a", "message_only": "#f0b232", "sleeping": "#80848e"}
EVENT_KIND_LABELS = {"crossing": "閾値", "sleep": "就寝", "wake": "起床"}


def format_hour(h: float) -> str:
    return f"{int(h):02d}:{round(h % 1 * 60):02d}"


class StateWindow(QWidget):
    def __init__(self, client: CoreClient, character: dict):
        super().__init__()
        self._client = client
        self._character_id = character["id"]
        self.setObjectName("stateWindow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STATE_STYLE)
        self.setWindowTitle(f"{character['name']}の状態")
        self.resize(520, 760)

        self._availability = QLabel("―")
        self._availability.setObjectName("availability")
        self._updated = QLabel()
        self._updated.setStyleSheet(f"color: {COLORS['text_muted']};")
        header = QHBoxLayout()
        header.addWidget(self._availability)
        header.addStretch(1)
        header.addWidget(self._updated)

        values_box = QGroupBox("状態量")
        values_form = QFormLayout(values_box)
        self._bars: dict[str, tuple[QProgressBar, QLabel]] = {}
        for key, label in VALUE_LABELS.items():
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            detail = QLabel()
            detail.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 9pt;")
            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(bar)
            column.addWidget(detail)
            values_form.addRow(label, column)
            self._bars[key] = (bar, detail)

        profile_box = QGroupBox("性格・生活リズム（固定）")
        self._profile_form = QFormLayout(profile_box)
        self._profile_labels: dict[str, QLabel] = {}
        for key, label in (
            ("fatigue_response", "疲労反応"),
            ("sleepiness_response", "眠気反応"),
            ("boredom_sensitivity", "暇度感度"),
            ("sleep", "起床・就寝"),
            ("busy", "取り込み中の時間帯"),
        ):
            value = QLabel("―")
            self._profile_form.addRow(label, value)
            self._profile_labels[key] = value

        prompt_box = QGroupBox("プロンプトに注入される状態")
        self._prompt = QPlainTextEdit()
        self._prompt.setReadOnly(True)
        self._prompt.setFixedHeight(150)
        QVBoxLayout(prompt_box).addWidget(self._prompt)

        events_box = QGroupBox("状態イベント（新しい順）")
        self._events = QListWidget()
        QVBoxLayout(events_box).addWidget(self._events)

        memory_box = QGroupBox("長期記憶（LTM）")
        memory_label = QLabel("記憶システムは未実装")
        memory_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        QVBoxLayout(memory_box).addWidget(memory_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.addLayout(header)
        layout.addWidget(values_box)
        layout.addWidget(profile_box)
        layout.addWidget(prompt_box)
        layout.addWidget(events_box, 1)
        layout.addWidget(memory_box)

        client.event_received.connect(self._on_event)
        self.reload()

    def reload(self) -> None:
        self._client.get(f"/characters/{self._character_id}/state", self.apply)

    def _on_event(self, event: dict) -> None:
        if event.get("type") == "state.updated" and event.get("character_id") == self._character_id:
            self.apply(event["state"])

    def apply(self, state: dict) -> None:
        color = AVAILABILITY_COLORS.get(state["availability"], COLORS["text"])
        self._availability.setText(f'<span style="color:{color};">●</span> {state["availability_label"]}')
        self._updated.setText(f"更新 {format_time(state['updated_at'])}")

        for key, (bar, detail) in self._bars.items():
            value = state["values"][key]
            bar.setValue(round(value * 1000))
            thresholds = "・".join(f"{name} {v:.2f}" for name, v in state["thresholds"][key].items())
            detail.setText(f"{value:.3f}　（閾値：{thresholds}）")

        p, r = state["personality"], state["rhythm"]
        self._profile_labels["fatigue_response"].setText(p["fatigue_response"])
        self._profile_labels["sleepiness_response"].setText(p["sleepiness_response"])
        self._profile_labels["boredom_sensitivity"].setText(f"×{p['boredom_sensitivity']:.1f}")
        self._profile_labels["sleep"].setText(f"{format_hour(r['wake_hour'])} 起床 ／ {format_hour(r['bedtime_hour'])} 就寝")
        busy = "、".join(f"{format_hour(a)}〜{format_hour(b)}" for a, b in r["busy_blocks"]) or "なし"
        self._profile_labels["busy"].setText(busy)

        self._prompt.setPlainText(state["prompt_section"])

        self._events.clear()
        for e in state.get("events", []):
            kind = EVENT_KIND_LABELS.get(e["kind"], e["kind"])
            self._events.addItem(f"{format_time(e['created_at'])}  [{kind}] {e['detail']}")
        if not state.get("events"):
            self._events.addItem("（まだありません）")
