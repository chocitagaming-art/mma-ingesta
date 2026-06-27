"""Corner symmetry of api._swap_corners.

Every model feature is now a ``*_diff`` (scheduled_rounds was dropped), so
_swap_corners must negate every diff and leave any None untouched, so that
predict averages the forward estimate with the genuine corner-swapped estimate.
These tests pin that contract directly and via the shared builder.
"""

from datetime import date

from src.prediction.api import _swap_corners
from src.prediction.features import FighterHistorySummary, build_feature_row


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


def test_swap_negates_diffs_keeps_none():
    row = {
        "height_cm_diff": 5.0,
        "reach_cm_diff": -3.0,
        "age_diff": 0.0,
        "ranking_position_diff": None,
    }
    swapped = _swap_corners(row)

    assert swapped["height_cm_diff"] == -5.0
    assert swapped["reach_cm_diff"] == 3.0
    assert swapped["age_diff"] == 0.0
    # A missing diff stays None so the imputer fills the same median both ways.
    assert swapped["ranking_position_diff"] is None


def test_swap_is_an_involution_on_diffs():
    row = {"height_cm_diff": 5.0, "age_diff": -2.0, "ranking_position_diff": None}
    assert _swap_corners(_swap_corners(row)) == row


def test_swap_reproduces_genuine_corner_swap():
    """_swap_corners(forward) must equal the row built with the two corners
    physically swapped, because diff(a, b) == -diff(b, a) and the None pattern is
    symmetric."""
    red_history = _summary(ranking_position=5, win_streak=4)
    blue_history = _summary(ranking_position=None, win_streak=1)

    forward = build_feature_row(
        red_history,
        blue_history,
        red_height_cm=180.0,
        blue_height_cm=178.0,
        red_reach_cm=183.0,
        blue_reach_cm=180.0,
        red_age=30.0,
        blue_age=28.0,
    )
    genuine_swap = build_feature_row(
        blue_history,
        red_history,
        red_height_cm=178.0,
        blue_height_cm=180.0,
        red_reach_cm=180.0,
        blue_reach_cm=183.0,
        red_age=28.0,
        blue_age=30.0,
    )

    assert _swap_corners(forward) == genuine_swap
