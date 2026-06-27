"""Core unit tests for the ML feature primitives in features.py.

Covers the small pure helpers (classify_target, safe_divide, diff) and a
leak-free check that compute_fighter_history only aggregates bouts strictly
before the anchor date. All synthetic / in-memory: no DB access.
"""

from datetime import date

import numpy as np
import pandas as pd

from src.prediction.features import (
    build_fighter_history_dataframe,
    classify_target,
    compute_fighter_history,
    diff,
    safe_divide,
)


def test_classify_target_red_blue_and_none():
    red_wins = pd.Series({"winner_id": 10, "fighter_red_id": 10, "fighter_blue_id": 20})
    blue_wins = pd.Series({"winner_id": 20, "fighter_red_id": 10, "fighter_blue_id": 20})
    no_winner = pd.Series({"winner_id": np.nan, "fighter_red_id": 10, "fighter_blue_id": 20})
    third_party = pd.Series({"winner_id": 99, "fighter_red_id": 10, "fighter_blue_id": 20})

    assert classify_target(red_wins) == 1
    assert classify_target(blue_wins) == 0
    assert classify_target(no_winner) is None
    assert classify_target(third_party) is None


def test_safe_divide_zero_and_missing_denominator():
    assert safe_divide(5, 2) == 2.5
    assert safe_divide(5, 0) is None
    assert safe_divide(5, None) is None
    assert safe_divide(5, np.nan) is None
    assert safe_divide(None, 5) is None
    assert safe_divide(np.nan, 5) is None


def test_diff_is_none_when_either_side_missing():
    assert diff(5, 3) == 2.0
    assert diff(None, 3) is None
    assert diff(5, None) is None
    assert diff(np.nan, 3) is None
    assert diff(5, np.nan) is None


def _fight(fight_id: int, event_date: date, red_id: int, blue_id: int, winner_id: int) -> dict:
    return {
        "fight_id": fight_id,
        "event_date": event_date,
        "fighter_red_id": red_id,
        "fighter_blue_id": blue_id,
        "winner_id": winner_id,
        "method": "Decision",
        "end_round": 3,
        "red_sig_strikes_landed": 50,
        "red_sig_strikes_attempted": 100,
        "red_takedowns_landed": 2,
        "red_takedowns_attempted": 4,
        "red_submission_attempts": 1,
        "red_control_time_seconds": 120,
        "red_knockdowns": 0,
        "blue_sig_strikes_landed": 40,
        "blue_sig_strikes_attempted": 90,
        "blue_takedowns_landed": 1,
        "blue_takedowns_attempted": 3,
        "blue_submission_attempts": 0,
        "blue_control_time_seconds": 60,
        "blue_knockdowns": 0,
    }


def test_compute_fighter_history_is_leak_free():
    fights = pd.DataFrame(
        [
            _fight(1, date(2020, 1, 1), 1, 2, 1),
            _fight(2, date(2021, 1, 1), 1, 3, 1),
            _fight(3, date(2022, 6, 1), 1, 4, 1),  # the current/anchor bout
        ]
    )
    history_df = build_fighter_history_dataframe(fights)
    empty_rankings = pd.DataFrame()

    # Anchored at the current bout: only the two strictly-earlier bouts count.
    summary = compute_fighter_history(1, date(2022, 6, 1), history_df, empty_rankings, None)
    assert summary is not None
    assert summary.total_prior_fights == 2
    assert summary.latest_prior_fight_date == date(2021, 1, 1)

    # Anchoring exactly on the 2nd bout's date proves the strict `<` cutoff:
    # that same-day bout is excluded, leaving only the first.
    earlier = compute_fighter_history(1, date(2021, 1, 1), history_df, empty_rankings, None)
    assert earlier is not None
    assert earlier.total_prior_fights == 1
    assert earlier.latest_prior_fight_date == date(2020, 1, 1)

    # Before any bout the fighter has no prior history at all.
    assert compute_fighter_history(1, date(2019, 1, 1), history_df, empty_rankings, None) is None
