from datetime import datetime, timedelta

import pytest

from engawa.characters import get_character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.state import (
    AVAILABILITY_BOTH,
    AVAILABILITY_MESSAGE_ONLY,
    AVAILABILITY_SLEEPING,
    SLEEPINESS_SLEEP,
    CharacterState,
    StateEngine,
    day_hour,
    sleepiness_curve,
)
from engawa.core.state_service import StateService

SUI = get_character("sui")  # 8時起床・25時就寝・14〜17時は取り込み中


def at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute).astimezone()


def test_day_hour_wraps_after_midnight():
    assert day_hour(at(1), SUI.rhythm) == pytest.approx(25.0)
    assert day_hour(at(9), SUI.rhythm) == pytest.approx(9.0)


def test_sleepiness_curve_rises_toward_bedtime():
    r = SUI.rhythm
    assert sleepiness_curve(8.0, r, 0) == pytest.approx(0.35)  # 寝起き
    assert sleepiness_curve(12.0, r, 0) == pytest.approx(0.1)
    assert sleepiness_curve(23.0, r, 0) < sleepiness_curve(24.0, r, 0) < sleepiness_curve(25.0, r, 0)
    assert sleepiness_curve(26.0, r, 0) == pytest.approx(1.0)


def test_boredom_grows_with_idle_time_and_slower_when_busy():
    idle = StateEngine(SUI, None, at(10))
    idle.tick(at(12))
    busy = StateEngine(SUI, None, at(14))
    busy.tick(at(16))
    assert idle.state.boredom == pytest.approx(2 / 3, abs=1e-6)
    assert busy.state.boredom == pytest.approx(2 / 3 * 0.3, abs=1e-6)


def test_boredom_sensitivity_scales_growth():
    from dataclasses import replace

    chatty = replace(SUI, personality=replace(SUI.personality, boredom_sensitivity=2.0))
    engine = StateEngine(chatty, None, at(10))
    engine.tick(at(11))
    assert engine.state.boredom == pytest.approx(2 / 3, abs=1e-6)


def test_user_message_lowers_boredom_and_fatigue():
    engine = StateEngine(SUI, CharacterState(boredom=0.5, fatigue=0.5, updated_at=at(12).isoformat()), at(12))
    engine.on_user_message(at(12))
    assert engine.state.boredom == pytest.approx(0.35)
    assert engine.state.fatigue == pytest.approx(0.48)


def test_availability_follows_rhythm():
    engine = StateEngine(SUI, None, at(10))
    assert engine.availability(at(10)) == AVAILABILITY_BOTH
    assert engine.availability(at(15)) == AVAILABILITY_MESSAGE_ONLY
    engine.state.asleep = True
    assert engine.availability(at(10)) == AVAILABILITY_SLEEPING


def test_full_day_cycle_sleeps_at_night_and_wakes_rested():
    engine = StateEngine(SUI, None, at(8))
    events = engine.tick(at(23))
    assert not engine.state.asleep
    assert engine.state.fatigue == pytest.approx(0.6, abs=0.01)
    assert any(e.kind == "crossing" and "疲労" in e.detail for e in events)

    events = engine.tick(at(2, day=29))
    assert engine.state.asleep
    kinds = [e.kind for e in events]
    assert "sleep" in kinds
    assert any("就寝前会話" in e.detail for e in events)
    sleep_at = datetime.fromisoformat(next(e.at for e in events if e.kind == "sleep"))
    assert at(23) < sleep_at <= at(1, day=29)

    events = engine.tick(at(9, day=29))
    assert not engine.state.asleep
    assert [e.kind for e in events] == ["wake"]
    assert engine.state.fatigue < 0.05
    assert engine.state.sleepiness < SLEEPINESS_SLEEP


def test_prompt_section_reflects_personality_tags():
    engine = StateEngine(SUI, CharacterState(fatigue=0.7, sleepiness=0.7, updated_at=at(24 - 1).isoformat()), at(23))
    section = engine.prompt_section(at(23))
    assert "23:00" in section
    assert "口数が少なく" in section  # 疲労反応：静か
    assert "もう少しマスターと話していたい" in section  # 眠気反応：会話を求める
    fresh = StateEngine(SUI, None, at(12)).prompt_section(at(12))
    assert "口数が少なく" not in fresh
    assert "話していたい" not in fresh


async def test_state_persists_and_catches_up_after_restart(tmp_path):
    db = Database(tmp_path / "t.db")
    now = at(10)
    service = StateService(db, EventHub(), [SUI], clock=lambda: now)
    await service.tick_all()

    later = now + timedelta(hours=2)
    restarted = StateService(db, EventHub(), [SUI], clock=lambda: later)
    await restarted.tick_all()
    snapshot = restarted.snapshot("sui")
    assert snapshot["values"]["boredom"] == pytest.approx(2 / 3, abs=1e-6)
    assert snapshot["availability"] == AVAILABILITY_BOTH
