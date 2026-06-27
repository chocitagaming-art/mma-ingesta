from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


OUTPUT_CSV_PATH = Path("training_dataset.csv")
OUTPUT_TABLE_NAME = "fight_prediction_training_data"
SPOT_CHECK_COUNT = 3

# Used for hypothetical matchups that have no real bout on record. Real bouts
# carry their own scheduled_rounds (3 for prelims/main-card, 5 for main events
# and title fights), which we read straight from the fights row.
DEFAULT_SCHEDULED_ROUNDS = 3


@dataclass(frozen=True)
class FighterHistorySummary:
    total_prior_fights: int
    total_rounds_fought: int
    sig_strikes_landed_per_fight: float | None
    sig_strike_accuracy: float | None
    knockdowns_per_fight: float | None
    takedowns_landed_per_fight: float | None
    takedown_accuracy: float | None
    submission_attempts_per_fight: float | None
    control_time_seconds_per_fight: float | None
    win_streak: int
    wins_last_5: int
    pct_wins_by_ko: float | None
    pct_wins_by_submission: float | None
    pct_wins_by_decision: float | None
    days_since_last_fight: int | None
    ranking_position: int | None
    # Defensive / opponent-quality features (#25). All career-to-date and
    # leak-free: aggregated only over fights strictly before the current bout.
    sig_strikes_absorbed_per_fight: float | None
    sig_strike_defense: float | None
    takedowns_absorbed_per_fight: float | None
    takedown_defense: float | None
    avg_opponent_prior_win_rate: float | None
    latest_prior_fight_date: date | None


@dataclass(frozen=True)
class DatasetBuildResult:
    dataset: pd.DataFrame
    spot_checks: list[dict[str, Any]]
    total_fights_seen: int
    excluded_no_target: int
    excluded_missing_history: int
    excluded_missing_stats: int


# Every feature is a red-minus-blue diff. Five zero-importance features were
# dropped after the importance audit (submission_attempts_per_fight_diff,
# win_streak_diff, pct_wins_by_submission_diff, pct_wins_by_decision_diff and the
# only non-diff feature, scheduled_rounds). With scheduled_rounds gone every
# remaining feature negates under a corner swap, which strengthens corner
# symmetry. ranking_position_diff stays here but is auto-dropped at train time by
# get_available_feature_columns when it is all-NaN (existing behaviour).
FEATURE_COLUMNS = [
    "height_cm_diff",
    "reach_cm_diff",
    "age_diff",
    "sig_strikes_landed_per_fight_diff",
    "sig_strike_accuracy_diff",
    "knockdowns_per_fight_diff",
    "takedowns_landed_per_fight_diff",
    "takedown_accuracy_diff",
    "control_time_seconds_per_fight_diff",
    "wins_last_5_diff",
    "total_prior_fights_diff",
    "total_rounds_fought_diff",
    "pct_wins_by_ko_diff",
    "days_since_last_fight_diff",
    "ranking_position_diff",
    # Defensive signal + opponent quality / strength-of-schedule (#25).
    "sig_strikes_absorbed_per_fight_diff",
    "sig_strike_defense_diff",
    "takedowns_absorbed_per_fight_diff",
    "takedown_defense_diff",
    "avg_opponent_prior_win_rate_diff",
]
