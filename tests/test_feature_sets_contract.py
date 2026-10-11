"""Phase 4 column contract: the names every phase-4 block builds against.

The legacy set must stay the 27-jun bundle schema, and the method model must not
inherit any winner-only column.
"""

from __future__ import annotations

from src.prediction.features.method_features import METHOD_FEATURE_COLUMNS
from src.prediction.features.types import (
    CORNER_PAIR_BASES,
    DEFAULT_FEATURE_SET,
    FEATURE_COLUMNS,
    FEATURE_SETS,
    PREUFC_BASES,
    PREUFC_COLUMNS,
    PREUFC_DIFF_COLUMNS,
    UFC_COUNT_COLUMNS,
    WINNER_FEATURE_COLUMNS,
    pair_columns,
)


def test_legacy_set_is_the_27jun_schema_and_the_default():
    assert DEFAULT_FEATURE_SET == "legacy"
    assert FEATURE_SETS["legacy"] == FEATURE_COLUMNS
    assert len(FEATURE_COLUMNS) == 20


def test_preufc_bases_match_the_30sep_experiment_order():
    assert PREUFC_BASES == [
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


def test_pairs_are_red_then_blue_per_base():
    assert pair_columns(["x", "y"]) == ["x_red", "x_blue", "y_red", "y_blue"]
    assert UFC_COUNT_COLUMNS == ["ufc_prev_fights_red", "ufc_prev_fights_blue"]
    assert len(PREUFC_COLUMNS) == 18
    assert PREUFC_COLUMNS[:2] == ["espn_has_history_red", "espn_has_history_blue"]


def test_every_pair_column_is_red_or_blue_and_every_diff_ends_in_diff():
    for column in UFC_COUNT_COLUMNS + PREUFC_COLUMNS:
        assert column.endswith(("_red", "_blue"))
        assert not column.endswith("_diff")
    assert all(c.endswith("_diff") for c in PREUFC_DIFF_COLUMNS)
    assert set(CORNER_PAIR_BASES) == {"ufc_prev_fights", *PREUFC_BASES}


def test_sets_have_no_duplicates_and_live_inside_the_winner_columns():
    assert len(WINNER_FEATURE_COLUMNS) == len(set(WINNER_FEATURE_COLUMNS)) == 49
    for name, columns in FEATURE_SETS.items():
        assert len(columns) == len(set(columns)), name
        assert set(columns) <= set(WINNER_FEATURE_COLUMNS), name
        assert columns[:20] == FEATURE_COLUMNS, name
    assert len(FEATURE_SETS["base"]) == 22
    assert len(FEATURE_SETS["preufc"]) == 40
    assert len(FEATURE_SETS["preufc_diff"]) == 31


def test_method_model_does_not_inherit_winner_only_columns():
    winner_only = set(WINNER_FEATURE_COLUMNS) - set(FEATURE_COLUMNS)
    assert not winner_only & set(METHOD_FEATURE_COLUMNS)
