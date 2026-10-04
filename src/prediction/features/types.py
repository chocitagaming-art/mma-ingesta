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
    # Domain signals for the METHOD model. Same leak-free rule as the block
    # above: aggregated only over fights strictly before the current bout.
    # The winner model looks only at how a fighter WINS; how they LOSE (chin,
    # submission defence) and how long their fights last is what actually says
    # whether THIS pairing ends early. Defaulted to None so the many test
    # fixtures that build a summary by hand keep working; the real producer
    # (compute_fighter_history) always passes them explicitly.
    pct_losses_by_ko: float | None = None
    pct_losses_by_submission: float | None = None
    avg_fight_duration_s: float | None = None
    pct_went_the_distance: float | None = None


@dataclass(frozen=True)
class DatasetBuildResult:
    dataset: pd.DataFrame
    spot_checks: list[dict[str, Any]]
    total_fights_seen: int
    excluded_no_target: int
    # Phase 4 opened both gates of the winner CSV, so build_training_dataset no
    # longer excludes for these two reasons and always reports 0. Kept so the log
    # line and the callers that build a result by hand keep their shape.
    excluded_missing_history: int
    excluded_missing_stats: int
    # The rows each old gate used to exclude, now kept with their history diffs
    # NaN: a corner without a UFC summary (a debutant, or prior fights without
    # fight_stats), or both summaries present with an accuracy None (zero
    # attempts). Mutually exclusive, like the two exclusions they replace.
    included_no_ufc_history: int = 0
    included_nan_stats: int = 0


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


# --- Phase 4 (Contender Series): winner-only columns ------------------------------
# FEATURE_COLUMNS above stays exactly as it is: it is the schema of the 27-jun bundle
# and the base of the METHOD model (METHOD_FEATURE_COLUMNS inherits it). Everything
# below is used ONLY by the winner model, so the method model's LogisticRegression
# never sees these columns or their NaN.
#
# Per-corner pairs: a corner swap EXCHANGES {base}_red and {base}_blue, it does not
# negate them. Written as red-minus-blue diffs the pre-UFC block lost its signal
# (29-sep, docs/experiments/preufc-encoding-2026-09-29/), so it goes per corner.
CORNER_SIDES = ("red", "blue")

# Prior UFC fights of each corner, counted from `fights` with the strict date cut.
# Never NaN: a debutant is an explicit 0. With the gates open the history diffs of a
# debutant are NaN, and a NaN does not say which corner is the new one; the bench of
# the 30-sep experiment carried this count explicitly (build_bench_dataset.py:84).
UFC_COUNT_BASES = ["ufc_prev_fights"]

# The nine pre-UFC variables of fight_history_espn, in the order of
# docs/experiments/preufc-dwcs-2026-09-30/build_espn_block.py (ESPN_COLUMNS).
PREUFC_BASES = [
    "espn_has_history",
    "espn_prev_fights",
    "espn_win_rate",
    "espn_ko_rate",
    "espn_sub_rate",
    "espn_streak",
    "espn_days_since_last",
    "espn_years_pro",
    "espn_title_fights",
]

CORNER_PAIR_BASES = UFC_COUNT_BASES + PREUFC_BASES


def pair_columns(bases: list[str]) -> list[str]:
    """[f"{base}_red", f"{base}_blue", ...] for each base, in order."""
    return [f"{base}_{side}" for base in bases for side in CORNER_SIDES]


UFC_COUNT_COLUMNS = pair_columns(UFC_COUNT_BASES)
PREUFC_COLUMNS = pair_columns(PREUFC_BASES)
# Arm C of the phase-4 measurement only (informative): the same block written as
# red-minus-blue diffs. They end in _diff, so a corner swap negates them.
PREUFC_DIFF_COLUMNS = [f"{base}_diff" for base in PREUFC_BASES]

# Every column the winner training CSV may carry, in order.
WINNER_FEATURE_COLUMNS = (
    FEATURE_COLUMNS + UFC_COUNT_COLUMNS + PREUFC_COLUMNS + PREUFC_DIFF_COLUMNS
)

# Named column sets for train/calibrate/evaluate (--feature-set). "legacy" is the
# 27-jun bundle and the default, so nothing changes unless a set is asked for.
FEATURE_SETS: dict[str, list[str]] = {
    "legacy": list(FEATURE_COLUMNS),
    "base": FEATURE_COLUMNS + UFC_COUNT_COLUMNS,
    "preufc": FEATURE_COLUMNS + UFC_COUNT_COLUMNS + PREUFC_COLUMNS,
    "preufc_diff": FEATURE_COLUMNS + UFC_COUNT_COLUMNS + PREUFC_DIFF_COLUMNS,
}
DEFAULT_FEATURE_SET = "legacy"
