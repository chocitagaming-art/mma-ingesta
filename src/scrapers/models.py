from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FighterRecord:
    name: str
    nickname: str | None
    headshot_url: str | None
    nationality: str | None
    birth_date: date | None
    height_cm: float | None
    reach_cm: float | None
    stance: str | None
    weight_grams: int | None
    wins: int
    losses: int
    draws: int
    source: str
    source_id: str


@dataclass(frozen=True)
class EventRecord:
    name: str
    event_date: date | None
    location: str | None
    promotion_id: int


@dataclass(frozen=True)
class FightRecord:
    event_id: int
    fighter_red_id: int
    fighter_blue_id: int
    weight_class: str | None
    weight_grams: int | None
    scheduled_rounds: int | None
    winner_id: int | None
    method: str | None
    end_round: int | None
    end_time: str | None
    odds_red: float | None
    odds_blue: float | None
    source: str
    source_id: str


@dataclass(frozen=True)
class FightStatsRecord:
    fight_id: int
    fighter_id: int
    sig_strikes_landed: int | None
    sig_strikes_attempted: int | None
    takedowns_landed: int | None
    takedowns_attempted: int | None
    submission_attempts: int | None
    control_time_seconds: int | None
    knockdowns: int | None