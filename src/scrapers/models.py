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
    # Title-fight flag parsed from the ufcstats belt icon in the card's
    # weight-class cell. None means "unknown" and never overwrites a stored
    # value (COALESCE, like UpcomingFightRecord); the default keeps old
    # constructions valid.
    is_title_fight: bool | None = None


@dataclass(frozen=True)
class FightStatsRoundRecord:
    """One fighter's core stats for ONE round (migration 012 / BE4).

    fight_stats keeps the same metrics summed across rounds; this record is the
    per-round breakdown parsed from the "Per round" table of a ufcstats
    fight-details page. Rounds are 1-based and match fights.end_round.
    """

    fight_id: int
    fighter_id: int
    round: int
    sig_strikes_landed: int | None
    sig_strikes_attempted: int | None
    takedowns_landed: int | None
    takedowns_attempted: int | None
    submission_attempts: int | None
    control_time_seconds: int | None
    knockdowns: int | None


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
    # Significant-strike breakdown (summed across rounds). NULL when the fight
    # page has no per-target table (very old fights) — see migration 005.
    sig_str_head_landed: int | None = None
    sig_str_head_attempted: int | None = None
    sig_str_body_landed: int | None = None
    sig_str_body_attempted: int | None = None
    sig_str_leg_landed: int | None = None
    sig_str_leg_attempted: int | None = None
    sig_str_distance_landed: int | None = None
    sig_str_distance_attempted: int | None = None
    sig_str_clinch_landed: int | None = None
    sig_str_clinch_attempted: int | None = None
    sig_str_ground_landed: int | None = None
    sig_str_ground_attempted: int | None = None