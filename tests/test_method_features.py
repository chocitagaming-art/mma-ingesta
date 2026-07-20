"""Tests for the win-METHOD feature layer (classification fix + method row).

The golden corpus below is the REAL set of distinct ``fights.method`` values in
Neon as of 2026-07-19 (71 literals) plus the ESPN provisional codes and edge
cases. Two guarantees are pinned here:

1. The classification fix recognises the real ufcstats codes (``U-DEC``,
   ``SUB - Armbar``...) that the old substring matching silently dropped.
2. WINNER-MODEL PARITY: the "ko" branch is byte-identical to the old logic, so
   ``pct_wins_by_ko`` (the only method-derived winner feature) cannot move. The
   parity test re-implements the OLD classifier inline and asserts the "ko"
   outcome matches on every corpus value.
"""

from datetime import date

import numpy as np
import pandas as pd

from src.prediction.features import (
    FEATURE_COLUMNS,
    FighterHistorySummary,
    build_feature_row,
    classify_win_method,
)
from src.prediction.features.method_features import (
    METHOD_CLASSES,
    METHOD_FEATURE_COLUMNS,
    METHOD_SUM_FEATURES,
    build_method_feature_row,
    parse_weight_class,
)
from src.prediction.features.method_training import build_method_training_dataset


# --- Golden corpus: every distinct fights.method in Neon (2026-07-19) ---------

_KO_LITERALS = [
    "KO/TKO",
    "KO/TKO - Elbow",
    "KO/TKO - Elbows",
    "KO/TKO - Flying Knee",
    "KO/TKO - Headbutt",
    "KO/TKO - Headbutts",
    "KO/TKO - Kick",
    "KO/TKO - Kicks",
    "KO/TKO - Knee",
    "KO/TKO - Knees",
    "KO/TKO - Punch",
    "KO/TKO - Punches",
    "KO/TKO - Slam",
    "KO/TKO - Spinning Back Elbow",
    "KO/TKO - Spinning Back Fist",
    "KO/TKO - Spinning Back Kick",
]

_SUB_LITERALS = [
    "SUB",
    "SUB - Anaconda Choke",
    "SUB - Ankle Lock",
    "SUB - Arm Triangle",
    "SUB - Armbar",
    "SUB - Bulldog Choke",
    "SUB - Calf Slicer",
    "SUB - D'Arce Choke",
    "SUB - Ezekiel Choke",
    "SUB - Forearm Choke",
    "SUB - Gi Choke",
    "SUB - Guillotine Choke",
    "SUB - Heel Hook",
    "SUB - Injury",
    "SUB - Inverted Triangle",
    "SUB - Keylock",
    "SUB - Kimura",
    "SUB - Kneebar",
    "SUB - Neck Crank",
    "SUB - North-South Choke",
    "SUB - Omoplata",
    "SUB - Other",
    "SUB - Other - Choke",
    "SUB - Other - Lock",
    "SUB - Pace/Pillory Choke",
    "SUB - Peruvian Necktie",
    "SUB - Rear Naked Choke",
    "SUB - Scarf Hold",
    "SUB - Schultz Front Headlock",
    "SUB - Shoulder Choke",
    "SUB - Straight Armbar",
    "SUB - Suloev Stretch",
    "SUB - Toe Hold",
    "SUB - Triangle Armbar",
    "SUB - Triangle Choke",
    "SUB - Twister",
    "SUB - Von Flue Choke",
]

_DEC_LITERALS = ["U-DEC", "S-DEC", "M-DEC"]

# Neither a clean KO/SUB/DEC win: no-contests, DQs, overturned results.
_OTHER_LITERALS = [
    "CNC",
    "DQ",
    "DQ - Rear Naked Choke",
    "Other",
    "Overturned",
    "Overturned - Arm Triangle",
    "Overturned - D'Arce Choke",
    "Overturned - Elbows",
    "Overturned - Guillotine Choke",
    "Overturned - Kick",
    "Overturned - Knee",
    "Overturned - Punch",
    "Overturned - Punches",
    "Overturned - Rear Naked Choke",
    "Overturned - Triangle Choke",
]

# Provisional codes written by espn_live_results between a live event and the
# ufcstats consolidation (METHOD_BY_DETAIL). The old classifier was written for
# exactly these; they must keep working.
_ESPN_PROVISIONALS = {"KO/TKO": "ko", "Submission": "submission", "Decision": "decision"}


def _old_classify(method):
    """The pre-fix classifier, verbatim, for the winner-parity assertion."""
    if not isinstance(method, str) or not method:
        return None
    lowered = method.lower()
    if "ko" in lowered or "tko" in lowered:
        return "ko"
    if "submission" in lowered:
        return "submission"
    if "decision" in lowered:
        return "decision"
    return None


def _corpus():
    return (
        _KO_LITERALS
        + _SUB_LITERALS
        + _DEC_LITERALS
        + _OTHER_LITERALS
        + list(_ESPN_PROVISIONALS)
    )


def test_classify_win_method_recognises_real_ufcstats_codes():
    for literal in _KO_LITERALS:
        assert classify_win_method(literal) == "ko", literal
    for literal in _SUB_LITERALS:
        assert classify_win_method(literal) == "submission", literal
    for literal in _DEC_LITERALS:
        assert classify_win_method(literal) == "decision", literal
    for literal in _OTHER_LITERALS:
        assert classify_win_method(literal) is None, literal
    for literal, expected in _ESPN_PROVISIONALS.items():
        assert classify_win_method(literal) == expected, literal


def test_classify_win_method_null_like_inputs():
    assert classify_win_method(None) is None
    assert classify_win_method("") is None
    assert classify_win_method(float("nan")) is None
    assert classify_win_method(np.nan) is None


def test_ko_branch_parity_with_old_classifier():
    """WINNER-MODEL GUARD: pct_wins_by_ko depends on classify_win_method only
    through the "is it ko" outcome. That outcome must be identical to the old
    classifier on every known method literal, so the winner model's features
    cannot shift under this fix."""
    for literal in _corpus():
        old_is_ko = _old_classify(literal) == "ko"
        new_is_ko = classify_win_method(literal) == "ko"
        assert old_is_ko == new_is_ko, literal


# --- parse_weight_class -------------------------------------------------------


def test_parse_weight_class_men_women_and_unknowns():
    assert parse_weight_class("Lightweight") == (70.3, 0.0)
    assert parse_weight_class("Heavyweight") == (120.2, 0.0)
    assert parse_weight_class("Women's Strawweight") == (52.2, 1.0)
    assert parse_weight_class("Women's Bantamweight") == (61.2, 1.0)
    assert parse_weight_class("Women's Featherweight") == (65.8, 1.0)
    # Curly-apostrophe variant must not fall through to the male branch.
    assert parse_weight_class("Women’s Flyweight") == (56.7, 1.0)
    # No fixed weight limit -> unknown kg, but the division gender is known.
    assert parse_weight_class("Open Weight") == (None, 0.0)
    assert parse_weight_class("Catch Weight") == (None, 0.0)
    assert parse_weight_class("Super Heavyweight") == (None, 0.0)
    # Absent label -> both unknown (imputer fills the training median).
    assert parse_weight_class(None) == (None, None)
    assert parse_weight_class("") == (None, None)
    assert parse_weight_class(float("nan")) == (None, None)


# --- build_method_feature_row -------------------------------------------------

_ALL_HISTORY_ATTRS = [
    "total_prior_fights",
    "total_rounds_fought",
    "sig_strikes_landed_per_fight",
    "sig_strike_accuracy",
    "knockdowns_per_fight",
    "takedowns_landed_per_fight",
    "takedown_accuracy",
    "submission_attempts_per_fight",
    "control_time_seconds_per_fight",
    "win_streak",
    "wins_last_5",
    "pct_wins_by_ko",
    "pct_wins_by_submission",
    "pct_wins_by_decision",
    "days_since_last_fight",
    "ranking_position",
    "sig_strikes_absorbed_per_fight",
    "sig_strike_defense",
    "takedowns_absorbed_per_fight",
    "takedown_defense",
    "avg_opponent_prior_win_rate",
]


def _summaries():
    # red = (i+1)*10, blue = (i+1)*1 -> every sum (i+1)*11 is DISTINCT per attr,
    # so a swapped attribute cannot accidentally satisfy the golden assertions.
    red_vals = {attr: (i + 1) * 10.0 for i, attr in enumerate(_ALL_HISTORY_ATTRS)}
    blue_vals = {attr: (i + 1) * 1.0 for i, attr in enumerate(_ALL_HISTORY_ATTRS)}
    red = FighterHistorySummary(latest_prior_fight_date=date(2021, 1, 1), **red_vals)
    blue = FighterHistorySummary(latest_prior_fight_date=date(2020, 1, 1), **blue_vals)
    return red, blue, red_vals, blue_vals


def _base_row(red, blue):
    return build_feature_row(
        red,
        blue,
        red_height_cm=190.0,
        blue_height_cm=170.0,
        red_reach_cm=200.0,
        blue_reach_cm=180.0,
        red_age=35.0,
        blue_age=28.0,
    )


def test_build_method_feature_row_golden_mapping():
    red, blue, red_vals, blue_vals = _summaries()
    row = build_method_feature_row(
        _base_row(red, blue),
        red,
        blue,
        scheduled_rounds=5,
        weight_class="Women's Bantamweight",
    )

    # Keys + order are exactly METHOD_FEATURE_COLUMNS (base diffs first).
    assert list(row) == METHOD_FEATURE_COLUMNS
    assert METHOD_FEATURE_COLUMNS[: len(FEATURE_COLUMNS)] == FEATURE_COLUMNS

    # The base diffs pass through untouched.
    assert row["height_cm_diff"] == 20.0
    assert row["pct_wins_by_ko_diff"] == red_vals["pct_wins_by_ko"] - blue_vals["pct_wins_by_ko"]

    # Each sum feature must read ITS attribute (red-plus-blue), not another.
    for feature_name, attribute in METHOD_SUM_FEATURES.items():
        expected = red_vals[attribute] + blue_vals[attribute]
        assert row[feature_name] == expected, f"{feature_name} must read {attribute!r}"
    sums = [red_vals[a] + blue_vals[a] for a in METHOD_SUM_FEATURES.values()]
    assert len(set(sums)) == len(sums)

    assert row["scheduled_rounds"] == 5
    assert row["weight_kg"] == 61.2
    assert row["is_female_division"] == 1.0


def test_build_method_feature_row_missing_history_and_dirty_rounds():
    red, _blue, _red_vals, _blue_vals = _summaries()
    row = build_method_feature_row(
        build_feature_row(
            red,
            None,
            red_height_cm=180.0,
            blue_height_cm=178.0,
            red_reach_cm=None,
            blue_reach_cm=None,
            red_age=30.0,
            blue_age=28.0,
        ),
        red,
        None,
        scheduled_rounds=None,
        weight_class=None,
    )
    # A missing history side keeps every sum None (imputer decides later).
    for feature_name in METHOD_SUM_FEATURES:
        assert row[feature_name] is None, feature_name
    # Dirty/absent scheduled_rounds coerces to the default (3), never None.
    assert row["scheduled_rounds"] == 3
    assert row["weight_kg"] is None
    assert row["is_female_division"] is None


def test_method_row_corner_swap_symmetry():
    """Under a corner swap every *_diff negates and every symmetric feature
    (sums, scheduled_rounds, weight, division gender) stays put — this is what
    makes the serving-side forward/swapped averaging an exact symmetrization."""
    from src.prediction.api import _swap_corners

    red, blue, _red_vals, _blue_vals = _summaries()
    forward = build_method_feature_row(
        _base_row(red, blue), red, blue, scheduled_rounds=3, weight_class="Lightweight"
    )
    # The genuine swapped row: corners exchanged at the builder level.
    swapped_real = build_method_feature_row(
        build_feature_row(
            blue,
            red,
            red_height_cm=170.0,
            blue_height_cm=190.0,
            red_reach_cm=180.0,
            blue_reach_cm=200.0,
            red_age=28.0,
            blue_age=35.0,
        ),
        blue,
        red,
        scheduled_rounds=3,
        weight_class="Lightweight",
    )
    assert _swap_corners(forward) == swapped_real


# --- build_method_training_dataset -------------------------------------------


def _fight(
    fight_id,
    event_date,
    red_id,
    blue_id,
    winner_id,
    method,
    weight_class="Lightweight",
    scheduled_rounds=3,
):
    return {
        "fight_id": fight_id,
        "event_date": event_date,
        "fighter_red_id": red_id,
        "fighter_blue_id": blue_id,
        "winner_id": winner_id,
        "method": method,
        "end_round": 3,
        "scheduled_rounds": scheduled_rounds,
        "weight_class": weight_class,
        "red_birth_date": date(1990, 1, 1),
        "blue_birth_date": date(1991, 1, 1),
        "red_height_cm": 180.0,
        "blue_height_cm": 178.0,
        "red_reach_cm": 183.0,
        "blue_reach_cm": 181.0,
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


def test_build_method_training_dataset_targets_and_exclusions():
    fights = pd.DataFrame(
        [
            # Prior history so fighters 1 and 2 have usable summaries later.
            _fight(1, date(2019, 1, 1), 1, 3, 1, "KO/TKO - Punches"),
            _fight(2, date(2019, 6, 1), 2, 4, 2, "SUB - Armbar"),
            # Labeled fights (both corners now have prior history + stats).
            _fight(3, date(2020, 1, 1), 1, 2, 1, "KO/TKO - Punch"),
            _fight(4, date(2021, 1, 1), 1, 2, None, "U-DEC"),  # draw -> decision
            _fight(5, date(2021, 6, 1), 1, 2, 2, "SUB - Rear Naked Choke"),
            # Excluded: no clean method bucket.
            _fight(6, date(2021, 9, 1), 1, 2, None, "CNC"),
            _fight(7, date(2021, 10, 1), 1, 2, 1, "Overturned - Punches"),
            # Excluded: upcoming (no method yet).
            _fight(8, date(2026, 1, 1), 1, 2, None, None),
        ]
    )
    result = build_method_training_dataset(fights, pd.DataFrame())
    dataset = result.dataset

    # Fights 1-2 lack prior history for a corner; 3-5 are labeled and eligible.
    assert result.total_fights_seen == 8
    assert result.excluded_no_method == 3  # CNC + Overturned + upcoming NULL
    assert result.excluded_missing_history == 2  # the two seed fights
    assert sorted(dataset["fight_id"].tolist()) == [3, 4, 5]

    by_id = dataset.set_index("fight_id")
    assert by_id.loc[3, "target"] == METHOD_CLASSES.index("ko")
    assert by_id.loc[4, "target"] == METHOD_CLASSES.index("decision")  # the draw
    assert by_id.loc[5, "target"] == METHOD_CLASSES.index("submission")

    # Feature columns all present, in order, after the id/date prefix.
    assert list(dataset.columns) == ["fight_id", "event_date", *METHOD_FEATURE_COLUMNS, "target"]

    # Chronological order preserved for the downstream chronological split.
    assert dataset["event_date"].tolist() == sorted(dataset["event_date"].tolist())
