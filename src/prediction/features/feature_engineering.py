from __future__ import annotations

from typing import Any

from .metrics import diff
from .types import FighterHistorySummary


# SINGLE SOURCE OF TRUTH (train + serve): DO NOT MODIFY
def build_feature_row(
    red_history: FighterHistorySummary | None,
    blue_history: FighterHistorySummary | None,
    *,
    red_height_cm: float | None,
    blue_height_cm: float | None,
    red_reach_cm: float | None,
    blue_reach_cm: float | None,
    red_age: float | None,
    blue_age: float | None,
) -> dict[str, float | int | None]:
    """Single source of truth for a model feature row (keys == FEATURE_COLUMNS,
    in order). Every entry is a red-minus-blue diff, so the row negates cleanly
    under a corner swap. hist() yields None for a missing history side so the
    corresponding diff is None: the builder imputes nothing, leaving each caller
    free to keep its own imputation/exclusion policy. Shared by the training
    pipeline (build_training_dataset) and serving (api._build_feature_row) so the
    two can never drift."""

    def hist(history: FighterHistorySummary | None, attribute: str) -> Any:
        return getattr(history, attribute) if history is not None else None

    return {
        "height_cm_diff": diff(red_height_cm, blue_height_cm),
        "reach_cm_diff": diff(red_reach_cm, blue_reach_cm),
        "age_diff": diff(red_age, blue_age),
        "sig_strikes_landed_per_fight_diff": diff(hist(red_history, "sig_strikes_landed_per_fight"), hist(blue_history, "sig_strikes_landed_per_fight")),
        "sig_strike_accuracy_diff": diff(hist(red_history, "sig_strike_accuracy"), hist(blue_history, "sig_strike_accuracy")),
        "knockdowns_per_fight_diff": diff(hist(red_history, "knockdowns_per_fight"), hist(blue_history, "knockdowns_per_fight")),
        "takedowns_landed_per_fight_diff": diff(hist(red_history, "takedowns_landed_per_fight"), hist(blue_history, "takedowns_landed_per_fight")),
        "takedown_accuracy_diff": diff(hist(red_history, "takedown_accuracy"), hist(blue_history, "takedown_accuracy")),
        "control_time_seconds_per_fight_diff": diff(hist(red_history, "control_time_seconds_per_fight"), hist(blue_history, "control_time_seconds_per_fight")),
        "wins_last_5_diff": diff(hist(red_history, "wins_last_5"), hist(blue_history, "wins_last_5")),
        "total_prior_fights_diff": diff(hist(red_history, "total_prior_fights"), hist(blue_history, "total_prior_fights")),
        "total_rounds_fought_diff": diff(hist(red_history, "total_rounds_fought"), hist(blue_history, "total_rounds_fought")),
        "pct_wins_by_ko_diff": diff(hist(red_history, "pct_wins_by_ko"), hist(blue_history, "pct_wins_by_ko")),
        "days_since_last_fight_diff": diff(hist(red_history, "days_since_last_fight"), hist(blue_history, "days_since_last_fight")),
        "ranking_position_diff": diff(hist(red_history, "ranking_position"), hist(blue_history, "ranking_position")),
        "sig_strikes_absorbed_per_fight_diff": diff(hist(red_history, "sig_strikes_absorbed_per_fight"), hist(blue_history, "sig_strikes_absorbed_per_fight")),
        "sig_strike_defense_diff": diff(hist(red_history, "sig_strike_defense"), hist(blue_history, "sig_strike_defense")),
        "takedowns_absorbed_per_fight_diff": diff(hist(red_history, "takedowns_absorbed_per_fight"), hist(blue_history, "takedowns_absorbed_per_fight")),
        "takedown_defense_diff": diff(hist(red_history, "takedown_defense"), hist(blue_history, "takedown_defense")),
        "avg_opponent_prior_win_rate_diff": diff(hist(red_history, "avg_opponent_prior_win_rate"), hist(blue_history, "avg_opponent_prior_win_rate")),
    }
