"""The strict corner swap shared by serving, evaluation and the method trainer.

A corner swap must turn the row built for (A, B) into EXACTLY the row built for
(B, A): every red-minus-blue diff negates, every per-corner pair {base}_red /
{base}_blue exchanges its two values, and the method model's symmetric columns stay
put. Anything else is a column nobody has said how to swap, and passing it through
silently is how the per-corner block lost its signal (phase-4 map, 0.78 -> 0.54),
so it raises.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

import src.prediction.api as api
from src.prediction.corners import (
    CORNER_PAIRS,
    SWAP_INVARIANT_COLUMNS,
    swap_corners,
)
from src.prediction.features import FighterHistorySummary, build_feature_row
from src.prediction.features.method_features import (
    METHOD_FEATURE_COLUMNS,
    build_method_feature_row,
)
from src.prediction.features.types import (
    CORNER_PAIR_BASES,
    FEATURE_COLUMNS,
    FEATURE_SETS,
    PREUFC_BASES,
    WINNER_FEATURE_COLUMNS,
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
        pct_losses_by_ko=0.2,
        pct_losses_by_submission=0.1,
        avg_fight_duration_s=640.0,
        pct_went_the_distance=0.4,
    )
    base.update(overrides)
    return FighterHistorySummary(**base)


# Two fighters, deliberately different in EVERY input, the second without a
# pre-UFC record (has_history 0, the rates unknown) and without a ranking.
FIGHTER_A = {
    "history": _summary(),
    "height_cm": 180.0,
    "reach_cm": 185.0,
    "age": 30.0,
    "ufc_prev_fights": 6,
    "preufc": {
        "espn_has_history": 1.0,
        "espn_prev_fights": 14.0,
        "espn_win_rate": 0.86,
        "espn_ko_rate": 0.5,
        "espn_sub_rate": 0.21,
        "espn_streak": 5.0,
        "espn_days_since_last": 230.0,
        "espn_years_pro": 6.5,
        "espn_title_fights": 2.0,
    },
}
FIGHTER_B = {
    "history": _summary(
        total_prior_fights=2,
        total_rounds_fought=4,
        sig_strikes_landed_per_fight=31.0,
        takedown_accuracy=None,
        wins_last_5=1,
        ranking_position=None,
        pct_losses_by_ko=0.5,
        avg_fight_duration_s=410.0,
    ),
    "height_cm": 176.0,
    "reach_cm": None,
    "age": 27.0,
    "ufc_prev_fights": 0,
    "preufc": {
        "espn_has_history": 0.0,
        "espn_prev_fights": 0.0,
        "espn_win_rate": None,
        "espn_ko_rate": None,
        "espn_sub_rate": None,
        "espn_streak": None,
        "espn_days_since_last": None,
        "espn_years_pro": None,
        "espn_title_fights": None,
    },
}


def _winner_row(red: dict, blue: dict) -> dict:
    """The full winner row for red vs blue, built per corner: diffs from the
    shared builder, pairs as {base}_red = red's value, {base}_blue = blue's."""
    row = build_feature_row(
        red["history"],
        blue["history"],
        red_height_cm=red["height_cm"],
        blue_height_cm=blue["height_cm"],
        red_reach_cm=red["reach_cm"],
        blue_reach_cm=blue["reach_cm"],
        red_age=red["age"],
        blue_age=blue["age"],
    )
    row["ufc_prev_fights_red"] = red["ufc_prev_fights"]
    row["ufc_prev_fights_blue"] = blue["ufc_prev_fights"]
    for base in PREUFC_BASES:
        row[f"{base}_red"] = red["preufc"][base]
        row[f"{base}_blue"] = blue["preufc"][base]
    # Arm C of the measurement: the same block as red-minus-blue diffs.
    for base in PREUFC_BASES:
        red_value, blue_value = red["preufc"][base], blue["preufc"][base]
        row[f"{base}_diff"] = (
            None if red_value is None or blue_value is None else red_value - blue_value
        )
    return row


def _method_row(red: dict, blue: dict) -> dict:
    base_row = build_feature_row(
        red["history"],
        blue["history"],
        red_height_cm=red["height_cm"],
        blue_height_cm=blue["height_cm"],
        red_reach_cm=red["reach_cm"],
        blue_reach_cm=blue["reach_cm"],
        red_age=red["age"],
        blue_age=blue["age"],
    )
    return build_method_feature_row(
        base_row,
        red["history"],
        blue["history"],
        scheduled_rounds=5,
        weight_class="Women's Flyweight",
        is_title_fight=True,
    )


# --- The swap reproduces a genuine corner swap -----------------------------------


def test_swap_pairs_reproduces_genuine_corner_swap():
    forward = _winner_row(FIGHTER_A, FIGHTER_B)
    genuine = _winner_row(FIGHTER_B, FIGHTER_A)
    # Every winner column is present, so the whole contract is exercised.
    assert set(forward) == set(WINNER_FEATURE_COLUMNS)

    assert swap_corners(forward) == genuine
    assert swap_corners(genuine) == forward


def test_swap_exchanges_pairs_and_negates_diffs_value_by_value():
    forward = _winner_row(FIGHTER_A, FIGHTER_B)
    swapped = swap_corners(forward)

    for base, (red, blue) in CORNER_PAIRS.items():
        assert swapped[red] == forward[blue], base
        assert swapped[blue] == forward[red], base
    assert swapped["espn_win_rate_red"] is None
    assert swapped["espn_win_rate_blue"] == 0.86
    assert swapped["ufc_prev_fights_red"] == 0
    assert swapped["ufc_prev_fights_blue"] == 6
    assert swapped["age_diff"] == -forward["age_diff"]
    # A missing diff stays None so the imputer fills the same median both ways.
    assert forward["reach_cm_diff"] is None and swapped["reach_cm_diff"] is None
    assert forward["espn_win_rate_diff"] is None
    assert swapped["espn_win_rate_diff"] is None
    assert swapped["espn_prev_fights_diff"] == -14.0


def test_swap_is_an_involution_and_keeps_key_order():
    forward = _winner_row(FIGHTER_A, FIGHTER_B)
    assert swap_corners(swap_corners(forward)) == forward
    assert list(swap_corners(forward)) == list(forward)


def test_swap_does_not_mutate_its_input():
    forward = _winner_row(FIGHTER_A, FIGHTER_B)
    snapshot = dict(forward)
    swap_corners(forward)
    assert forward == snapshot


def test_legacy_rows_swap_exactly_as_before():
    """The 27-jun shape (20 diffs, None and float NaN) is unchanged: negate the
    number, keep None, NaN stays NaN."""
    row = {
        "height_cm_diff": 5.0,
        "reach_cm_diff": -3.0,
        "age_diff": 0.0,
        "wins_last_5_diff": 2,
        "ranking_position_diff": None,
        "days_since_last_fight_diff": float("nan"),
    }
    swapped = swap_corners(row)
    assert swapped["height_cm_diff"] == -5.0
    assert swapped["reach_cm_diff"] == 3.0
    assert swapped["age_diff"] == 0.0
    assert swapped["wins_last_5_diff"] == -2
    assert swapped["ranking_position_diff"] is None
    assert math.isnan(swapped["days_since_last_fight_diff"])


def test_api_keeps_swap_corners_as_an_alias():
    """evaluate.py and train_method.py import api._swap_corners: the alias makes
    them use the strict swap without touching their imports."""
    assert api._swap_corners is swap_corners


# --- The method model's symmetric columns ----------------------------------------


def test_invariant_list_is_exactly_the_method_models_extra_columns():
    extra = [c for c in METHOD_FEATURE_COLUMNS if c not in FEATURE_COLUMNS]
    assert len(extra) == 17
    assert set(SWAP_INVARIANT_COLUMNS) == set(extra)


@pytest.mark.parametrize(
    "column", [c for c in METHOD_FEATURE_COLUMNS if c not in FEATURE_COLUMNS]
)
def test_each_invariant_column_really_does_not_change_with_the_corners(column):
    """Checked one by one on a genuine swap of two different fighters: a column
    in the passthrough list that DID depend on the corner would break the
    symmetry of the method model in silence."""
    forward = _method_row(FIGHTER_A, FIGHTER_B)
    genuine = _method_row(FIGHTER_B, FIGHTER_A)
    assert forward[column] is not None, column
    assert forward[column] == genuine[column], column
    assert swap_corners(forward)[column] == genuine[column], column


def test_method_row_swap_reproduces_genuine_corner_swap():
    forward = _method_row(FIGHTER_A, FIGHTER_B)
    genuine = _method_row(FIGHTER_B, FIGHTER_A)
    assert swap_corners(forward) == genuine


def test_train_method_symmetrizes_with_the_strict_swap():
    """train_method.py imports api._swap_corners for its served-equivalent
    metrics: every method column has a rule, so the per-class average of a
    genuine (A, B) / (B, A) pair is identical. The frame carries ids and the
    target like the real dataset; they are dropped before the swap."""
    from sklearn.impute import SimpleImputer
    from xgboost import XGBClassifier

    from src.prediction import train_method

    rng = np.random.default_rng(5)
    synthetic = pd.DataFrame(
        rng.normal(size=(200, len(METHOD_FEATURE_COLUMNS))),
        columns=METHOD_FEATURE_COLUMNS,
    )
    imputer = SimpleImputer(strategy="median").fit(synthetic)
    model = XGBClassifier(
        objective="multi:softprob", n_estimators=6, max_depth=2, random_state=0
    )
    model.fit(imputer.transform(synthetic), rng.integers(0, 3, size=200))
    frame = pd.DataFrame(
        [_method_row(FIGHTER_A, FIGHTER_B), _method_row(FIGHTER_B, FIGHTER_A)]
    )
    frame.insert(0, "fight_id", [901, 902])
    frame["target"] = [0, 2]

    _raw, symmetrized = train_method._probability_variants(
        model, imputer, list(METHOD_FEATURE_COLUMNS), frame
    )

    np.testing.assert_allclose(symmetrized[0], symmetrized[1], rtol=0, atol=1e-12)


def test_every_column_any_pipeline_carries_is_accepted():
    for name, columns in FEATURE_SETS.items():
        swap_corners({column: 1.0 for column in columns})
    swap_corners({column: 1.0 for column in WINNER_FEATURE_COLUMNS})
    swap_corners({column: 1.0 for column in METHOD_FEATURE_COLUMNS})


def test_pairs_cover_every_corner_pair_base():
    assert set(CORNER_PAIRS) == set(CORNER_PAIR_BASES)
    for base, (red, blue) in CORNER_PAIRS.items():
        assert (red, blue) == (f"{base}_red", f"{base}_blue")


# --- Strict: unknown columns raise ------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "fight_id",
        "event_date",
        "target",
        "espn_win_rate",  # a pair base without its corner
        "scheduled_rounds_red",
        "some_future_feature",
    ],
)
def test_swap_unknown_column_raises(column):
    row = {"age_diff": 1.0, column: 3.0}
    with pytest.raises(ValueError, match=column):
        swap_corners(row)


def test_swap_names_every_unknown_column_at_once():
    with pytest.raises(ValueError) as error:
        swap_corners({"age_diff": 1.0, "fight_id": 1, "target": 0})
    assert "fight_id" in str(error.value) and "target" in str(error.value)


@pytest.mark.parametrize("present", ["espn_win_rate_red", "ufc_prev_fights_blue"])
def test_swap_half_a_pair_raises(present):
    """Half a pair has nothing to exchange with: swapping it would hand the
    model one fighter's value as if it were the other's."""
    with pytest.raises(ValueError, match=present):
        swap_corners({"age_diff": 1.0, present: 0.5})


# --- The same contract on a DataFrame ----------------------------------------------


def test_frame_swap_matches_the_row_swap():
    rows = [
        _winner_row(FIGHTER_A, FIGHTER_B),
        _winner_row(FIGHTER_B, FIGHTER_A),
        _winner_row(FIGHTER_A, replace_preufc(FIGHTER_A, espn_win_rate=0.4)),
    ]
    frame = pd.DataFrame(rows, index=[10, 11, 12])

    swapped = swap_corners(frame)

    assert isinstance(swapped, pd.DataFrame)
    assert list(swapped.columns) == list(frame.columns)
    assert list(swapped.index) == [10, 11, 12]
    expected = pd.DataFrame([swap_corners(row) for row in rows], index=[10, 11, 12])
    pd.testing.assert_frame_equal(
        swapped.astype(float), expected[list(frame.columns)].astype(float)
    )
    # The genuine (B, A) row, NaN where (A, B) had None.
    assert swapped.loc[10, "espn_win_rate_blue"] == 0.86
    assert np.isnan(swapped.loc[10, "espn_win_rate_red"])


def test_frame_swap_is_strict_too():
    frame = pd.DataFrame([{"age_diff": 1.0, "fight_id": 7}])
    with pytest.raises(ValueError, match="fight_id"):
        swap_corners(frame)


def replace_preufc(fighter: dict, **preufc) -> dict:
    return {**fighter, "preufc": {**fighter["preufc"], **preufc}}
