"""時刻をキャラクターに伝えるための言葉（時間帯・季節・経過時間）。"""

from __future__ import annotations

from datetime import datetime, timedelta

WEEKDAYS = "月火水木金土日"

# (この時刻から, 呼び方)。上から順に見て、最後に当てはまったもの
TIMES_OF_DAY = (
    (0, "深夜"),
    (4, "明け方"),
    (6, "朝"),
    (10, "昼前"),
    (12, "昼"),
    (14, "午後"),
    (17, "夕方"),
    (19, "夜"),
    (23, "深夜"),
)
SEASONS = {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春", 6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}


def time_of_day(now: datetime) -> str:
    return [label for hour, label in TIMES_OF_DAY if now.hour >= hour][-1]


def season(now: datetime) -> str:
    return SEASONS[now.month]


def describe_now(now: datetime) -> str:
    """例：9月29日（火） 14:32（午後・秋）"""
    return f"{now.month}月{now.day}日（{WEEKDAYS[now.weekday()]}） {now:%H:%M}（{time_of_day(now)}・{season(now)}）"


def message_stamp(created_at: str) -> str:
    """会話履歴の発言に付ける時刻。例：（9/29 14:32）"""
    moment = datetime.fromisoformat(created_at).astimezone()
    return f"（{moment.month}/{moment.day} {moment:%H:%M}）"


def describe_elapsed(elapsed: timedelta) -> str:
    minutes = elapsed.total_seconds() / 60
    if minutes < 60:
        return f"約{max(1, round(minutes))}分"
    if minutes < 60 * 24:
        return f"約{round(minutes / 60)}時間"
    return f"約{round(minutes / 60 / 24)}日"
