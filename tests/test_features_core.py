"""Core unit tests for the ML feature primitives in features.py.

Covers the small pure helpers (classify_target, safe_divide, diff) and a
leak-free check that compute_fighter_history only aggregates bouts strictly
before the anchor date. All synthetic / in-memory: no DB access.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.prediction.features import (
    build_fighter_history_dataframe,
    classify_target,
    compute_fighter_history,
    diff,
    safe_divide,
)
from src.prediction.features.fighter_history import _duration_seconds


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


# --- Domain signals of the METHOD model --------------------------------------
# The winner model only looks at how a fighter WINS. These four describe how the
# PAIRING tends to end, so their leak-freeness matters just as much: a loss by KO
# that has not happened yet must never raise "this one gets knocked out".


def _bout(
    fight_id: int,
    event_date: date,
    red_id: int,
    blue_id: int,
    winner_id: int,
    *,
    method: str,
    end_round: int = 3,
    end_time: str = "5:00",
) -> dict:
    fight = _fight(fight_id, event_date, red_id, blue_id, winner_id)
    fight["method"] = method
    fight["end_round"] = end_round
    fight["end_time"] = end_time
    return fight


def test_duration_seconds_reads_round_plus_clock():
    assert _duration_seconds(3, "2:30") == 750.0  # 2 full rounds + 2:30
    assert _duration_seconds(1, "0:00") == 0.0
    assert _duration_seconds(5, "5:00") == 1500.0  # a full five-round decision
    # Anything we cannot read stays None so the imputer decides, never a made-up 0.
    assert _duration_seconds(None, "2:30") is None
    assert _duration_seconds(3, None) is None
    assert _duration_seconds(3, "") is None
    assert _duration_seconds(3, "raro") is None
    assert _duration_seconds(np.nan, "2:30") is None


def test_domain_signals_ignore_same_day_and_future_bouts():
    """The KO loss on the anchor date itself must NOT feed pct_losses_by_ko."""
    fights = pd.DataFrame(
        [
            # Fighter 1 loses by submission, then by decision: 50% of the losses
            # are submissions and none are KO, as of 2022-06-01.
            _bout(1, date(2020, 1, 1), 2, 1, 2, method="Submission", end_round=1, end_time="3:00"),
            _bout(2, date(2021, 1, 1), 3, 1, 3, method="Decision", end_round=3, end_time="5:00"),
            # The anchor bout: a KO loss ON the same day. Strictly later in
            # knowledge terms, so it must be invisible.
            _bout(3, date(2022, 6, 1), 4, 1, 4, method="KO/TKO", end_round=1, end_time="1:00"),
        ]
    )
    history_df = build_fighter_history_dataframe(fights)

    summary = compute_fighter_history(1, date(2022, 6, 1), history_df, pd.DataFrame(), None)
    assert summary is not None
    assert summary.pct_losses_by_ko == 0.0, "the same-day KO leaked into the history"
    assert summary.pct_losses_by_submission == 0.5
    # 1 of the 2 prior bouts went to a decision.
    assert summary.pct_went_the_distance == 0.5
    # Stopped at 3:00 of round 1 (180s) and a full 3-rounder (900s).
    assert summary.avg_fight_duration_s == 540.0

    # Anchored AFTER the KO, the very same call now sees it.
    later = compute_fighter_history(1, date(2023, 1, 1), history_df, pd.DataFrame(), None)
    assert later is not None
    assert later.pct_losses_by_ko == pytest.approx(1 / 3)


def test_domain_signals_count_losses_only_where_it_matters():
    """pct_losses_by_* divides by LOSSES; pct_went_the_distance by all bouts."""
    fights = pd.DataFrame(
        [
            # Two wins by KO: they must not move pct_losses_by_ko at all.
            _bout(1, date(2019, 1, 1), 1, 5, 1, method="KO/TKO", end_round=1, end_time="2:00"),
            _bout(2, date(2019, 6, 1), 1, 6, 1, method="KO/TKO", end_round=2, end_time="1:00"),
            # One loss by KO.
            _bout(3, date(2020, 1, 1), 7, 1, 7, method="KO/TKO", end_round=1, end_time="4:00"),
            _bout(4, date(2021, 1, 1), 8, 1, 8, method="U-DEC", end_round=3, end_time="5:00"),
        ]
    )
    history_df = build_fighter_history_dataframe(fights)
    summary = compute_fighter_history(1, date(2022, 1, 1), history_df, pd.DataFrame(), None)

    assert summary is not None
    assert summary.pct_losses_by_ko == 0.5, "2 losses, 1 by KO"
    assert summary.pct_losses_by_submission == 0.0
    assert summary.pct_went_the_distance == 0.25, "1 of 4 bouts reached the judges"


def test_domain_signals_are_none_without_prior_losses():
    """No losses yet -> a rate over zero is unknown, not zero."""
    fights = pd.DataFrame(
        [
            _bout(1, date(2020, 1, 1), 1, 2, 1, method="KO/TKO", end_round=1, end_time="2:00"),
            _bout(2, date(2021, 1, 1), 1, 3, 1, method="Decision", end_round=3, end_time="5:00"),
        ]
    )
    history_df = build_fighter_history_dataframe(fights)
    summary = compute_fighter_history(1, date(2022, 1, 1), history_df, pd.DataFrame(), None)

    assert summary is not None
    assert summary.pct_losses_by_ko is None
    assert summary.pct_losses_by_submission is None
    # These two do not depend on losses, so they are known.
    assert summary.pct_went_the_distance == 0.5
    # Stopped at 2:00 of round 1 (120s) and a full 3-rounder (900s).
    assert summary.avg_fight_duration_s == pytest.approx((120 + 900) / 2)
