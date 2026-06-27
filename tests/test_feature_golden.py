"""Golden-value test pinning the 25-feature mapping of build_feature_row.

The parity/symmetry tests are builder-vs-builder and symmetric, so a transcription
bug (a feature reading the WRONG FighterHistorySummary attribute, or two columns
swapped) would pass them all. This test gives every attribute a DISTINCT value so
every expected diff is unique, then asserts each feature equals its specific
red-minus-blue value — catching any attribute/column transcription error.
"""

from datetime import date

from src.prediction.features import (
    FEATURE_COLUMNS,
    FighterHistorySummary,
    build_feature_row,
)

# Every history-derived feature -> the FighterHistorySummary attribute it must read.
_HISTORY_ATTR = {
    "sig_strikes_landed_per_fight_diff": "sig_strikes_landed_per_fight",
    "sig_strike_accuracy_diff": "sig_strike_accuracy",
    "knockdowns_per_fight_diff": "knockdowns_per_fight",
    "takedowns_landed_per_fight_diff": "takedowns_landed_per_fight",
    "takedown_accuracy_diff": "takedown_accuracy",
    "submission_attempts_per_fight_diff": "submission_attempts_per_fight",
    "control_time_seconds_per_fight_diff": "control_time_seconds_per_fight",
    "win_streak_diff": "win_streak",
    "wins_last_5_diff": "wins_last_5",
    "total_prior_fights_diff": "total_prior_fights",
    "total_rounds_fought_diff": "total_rounds_fought",
    "pct_wins_by_ko_diff": "pct_wins_by_ko",
    "pct_wins_by_submission_diff": "pct_wins_by_submission",
    "pct_wins_by_decision_diff": "pct_wins_by_decision",
    "days_since_last_fight_diff": "days_since_last_fight",
    "ranking_position_diff": "ranking_position",
    "sig_strikes_absorbed_per_fight_diff": "sig_strikes_absorbed_per_fight",
    "sig_strike_defense_diff": "sig_strike_defense",
    "takedowns_absorbed_per_fight_diff": "takedowns_absorbed_per_fight",
    "takedown_defense_diff": "takedown_defense",
    "avg_opponent_prior_win_rate_diff": "avg_opponent_prior_win_rate",
}

_NON_HISTORY = {"height_cm_diff", "reach_cm_diff", "age_diff", "scheduled_rounds"}


def _summaries():
    attrs = list(_HISTORY_ATTR.values())
    # red = (i+1)*10, blue = (i+1)*1  ->  diff = (i+1)*9, all DISTINCT per attribute.
    red_vals = {attr: (i + 1) * 10.0 for i, attr in enumerate(attrs)}
    blue_vals = {attr: (i + 1) * 1.0 for i, attr in enumerate(attrs)}
    red = FighterHistorySummary(latest_prior_fight_date=date(2021, 1, 1), **red_vals)
    blue = FighterHistorySummary(latest_prior_fight_date=date(2020, 1, 1), **blue_vals)
    return red, blue, red_vals, blue_vals


def test_build_feature_row_golden_mapping():
    red, blue, red_vals, blue_vals = _summaries()

    row = build_feature_row(
        red,
        blue,
        red_height_cm=190.0,
        blue_height_cm=170.0,
        red_reach_cm=200.0,
        blue_reach_cm=180.0,
        red_age=35.0,
        blue_age=28.0,
        scheduled_rounds=5,
    )

    # Keys + order are exactly FEATURE_COLUMNS and cover the whole list.
    assert list(row) == FEATURE_COLUMNS
    assert _NON_HISTORY | set(_HISTORY_ATTR) == set(FEATURE_COLUMNS)

    # Non-history features.
    assert row["height_cm_diff"] == 20.0
    assert row["reach_cm_diff"] == 20.0
    assert row["age_diff"] == 7.0
    assert row["scheduled_rounds"] == 5  # raw passthrough

    # Each history feature must read ITS attribute (red-minus-blue), not another.
    for feature, attr in _HISTORY_ATTR.items():
        expected = red_vals[attr] - blue_vals[attr]
        assert row[feature] == expected, f"{feature} must read attribute {attr!r}"

    # Sanity: the expected history diffs are all distinct, so a swapped attribute
    # cannot accidentally satisfy the assertion above.
    diffs = [red_vals[a] - blue_vals[a] for a in _HISTORY_ATTR.values()]
    assert len(set(diffs)) == len(diffs)
