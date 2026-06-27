"""Train/serve parity for the shared feature-row builder.

Feeds ONE fixed (red_history, blue_history, physical) tuple through
`build_feature_row` the way the TRAINING caller assembles its arguments
(build_training_dataset) and the way the SERVING caller assembles them
(api._build_feature_row), then asserts the produced feature dicts are equal
key-for-key, including the None pattern. This is the regression guard against
the train/serve skew the refactor eliminated.
"""

from datetime import date

from src.prediction.features import (
    FEATURE_COLUMNS,
    FighterHistorySummary,
    build_feature_row,
    compute_age,
)


def _summary(**overrides) -> FighterHistorySummary:
    base = dict(
        total_prior_fights=10,
        total_rounds_fought=25,
        sig_strikes_landed_per_fight=45.0,
        sig_strike_accuracy=0.5,
        knockdowns_per_fight=0.3,
        takedowns_landed_per_fight=1.5,
        takedown_accuracy=0.4,
        submission_attempts_per_fight=0.6,
        control_time_seconds_per_fight=120.0,
        win_streak=3,
        wins_last_5=4,
        pct_wins_by_ko=0.5,
        pct_wins_by_submission=0.2,
        pct_wins_by_decision=0.3,
        days_since_last_fight=200,
        ranking_position=5,
        sig_strikes_absorbed_per_fight=35.0,
        sig_strike_defense=0.55,
        takedowns_absorbed_per_fight=0.8,
        takedown_defense=0.7,
        avg_opponent_prior_win_rate=0.52,
        latest_prior_fight_date=date(2021, 1, 1),
    )
    base.update(overrides)
    return FighterHistorySummary(**base)


def test_feature_parity_training_vs_serving():
    # blue.ranking_position is None so the None pattern is actually exercised.
    red_history = _summary(ranking_position=5)
    blue_history = _summary(
        ranking_position=None, win_streak=1, sig_strikes_landed_per_fight=40.0
    )

    anchor = date(2022, 6, 1)
    red_birth = date(1992, 1, 1)
    blue_birth = date(1990, 1, 1)

    # Physical attrs as the serving path assembles them (fighters-table dict).
    physical = {
        101: {"birth_date": red_birth, "height_cm": 180.0, "reach_cm": 183.0},
        202: {"birth_date": blue_birth, "height_cm": 178.0, "reach_cm": 180.0},
    }

    # --- training caller convention (build_training_dataset) ---
    training_row = build_feature_row(
        red_history,
        blue_history,
        red_height_cm=180.0,
        blue_height_cm=178.0,
        red_reach_cm=183.0,
        blue_reach_cm=180.0,
        red_age=compute_age(red_birth, anchor),
        blue_age=compute_age(blue_birth, anchor),
    )

    # --- serving caller convention (api._build_feature_row) ---
    red_phys = physical.get(101, {})
    blue_phys = physical.get(202, {})
    serving_row = build_feature_row(
        red_history,
        blue_history,
        red_height_cm=red_phys.get("height_cm"),
        blue_height_cm=blue_phys.get("height_cm"),
        red_reach_cm=red_phys.get("reach_cm"),
        blue_reach_cm=blue_phys.get("reach_cm"),
        red_age=compute_age(red_phys.get("birth_date"), anchor),
        blue_age=compute_age(blue_phys.get("birth_date"), anchor),
    )

    assert set(training_row) == set(FEATURE_COLUMNS)
    assert list(training_row) == FEATURE_COLUMNS  # order preserved too
    assert training_row == serving_row

    # The None pattern is identical across callers and genuinely non-empty.
    training_none = {key for key, value in training_row.items() if value is None}
    serving_none = {key for key, value in serving_row.items() if value is None}
    assert training_none == serving_none == {"ranking_position_diff"}


def test_builder_is_neutral_about_missing_history():
    """The builder imputes nothing: a None history yields None diffs (serving's
    degraded path) while physical features still flow through."""
    red_history = _summary()
    row = build_feature_row(
        red_history,
        None,  # blue debutant / no usable stats
        red_height_cm=180.0,
        blue_height_cm=178.0,
        red_reach_cm=183.0,
        blue_reach_cm=180.0,
        red_age=30.0,
        blue_age=28.0,
    )
    # Physical features survive (both sides present).
    assert row["height_cm_diff"] == 2.0
    assert row["reach_cm_diff"] == 3.0
    assert row["age_diff"] == 2.0
    # Every history-derived diff is None because blue history is missing.
    history_diffs = [
        column
        for column in FEATURE_COLUMNS
        if column not in {"height_cm_diff", "reach_cm_diff", "age_diff"}
    ]
    assert all(row[column] is None for column in history_diffs)
