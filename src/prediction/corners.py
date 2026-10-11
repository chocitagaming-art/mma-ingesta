"""Corner swap shared by serving, evaluation and the method trainer.

Every probability the service returns is the average of the forward estimate and
the mirror estimate of the SAME matchup with the corners exchanged, and the mirror
is computed from ``swap_corners(row)``. That only symmetrizes the prediction if
``swap_corners(row(A, B))`` is exactly ``row(B, A)``, the row the builders would
produce with the two fighters physically swapped. Each column kind has its own
rule:

* ``*_diff`` (red minus blue): negated. A missing diff stays None (NaN stays NaN),
  so the imputer fills the same training median in both orientations; the None
  pattern is identical across corners because a diff is missing iff either side is.
* per-corner pairs ``{base}_red`` / ``{base}_blue`` (``CORNER_PAIR_BASES``):
  their two values are exchanged, never negated.
* the method model's pair-level columns (sums, rounds, division, title fight):
  passed through, they describe the bout, not a corner.

Anything else raises ValueError. The old swap passed unknown columns through on
the assumption that they were corner-independent; with per-corner columns that
kept red's values in red's slots, broke the symmetry in silence and all but erased
the block's signal from the served probability (phase-4 map, 0.78 -> 0.54).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any, overload

import pandas as pd

from src.prediction.features.types import CORNER_PAIR_BASES

DIFF_SUFFIX = "_diff"

# base -> (red column, blue column), from the phase-4 column contract.
CORNER_PAIRS: dict[str, tuple[str, str]] = {
    base: (f"{base}_red", f"{base}_blue") for base in CORNER_PAIR_BASES
}

# column -> the column it exchanges with, and column -> its pair's base.
_PARTNER: dict[str, str] = {
    **{red: blue for red, blue in CORNER_PAIRS.values()},
    **{blue: red for red, blue in CORNER_PAIRS.values()},
}
PAIR_BASE_BY_COLUMN: dict[str, str] = {
    column: base for base, pair in CORNER_PAIRS.items() for column in pair
}

# Written out on purpose, not derived: these are the method model's columns beyond
# FEATURE_COLUMNS (method_features.METHOD_FEATURE_COLUMNS), each one checked to be
# a property of the PAIRING or the BOUT and therefore unchanged by a corner swap
# (tests/test_corners.py checks them one by one on a genuine swap). A new method
# column must be added here by hand, after deciding how it swaps; until then the
# strict swap rejects it instead of guessing.
SWAP_INVARIANT_COLUMNS = frozenset(
    {
        # sum of both corners: symmetric by construction (pair_sum).
        "pct_wins_by_ko_sum",
        "pct_wins_by_submission_sum",
        "pct_wins_by_decision_sum",
        "knockdowns_per_fight_sum",
        "submission_attempts_per_fight_sum",
        "takedowns_landed_per_fight_sum",
        "control_time_seconds_per_fight_sum",
        "sig_strikes_landed_per_fight_sum",
        "total_prior_fights_sum",
        "pct_losses_by_ko_sum",
        "pct_losses_by_submission_sum",
        "avg_fight_duration_s_sum",
        "pct_went_the_distance_sum",
        # properties of the bout itself.
        "scheduled_rounds",
        "weight_kg",
        "is_female_division",
        "is_title_fight",
    }
)


def _check_columns(columns: Iterable[str]) -> None:
    """Raise ValueError unless every column has a known swap rule and every pair
    is complete. Names every offending column at once."""
    present = set(columns)
    unknown = sorted(
        (
            column
            for column in present
            if not isinstance(column, str)
            or (
                not column.endswith(DIFF_SUFFIX)
                and column not in _PARTNER
                and column not in SWAP_INVARIANT_COLUMNS
            )
        ),
        key=str,
    )
    if unknown:
        raise ValueError(
            "swap_corners: no corner-swap rule for column(s) "
            f"{unknown}. Only *_diff, the {{base}}_red/{{base}}_blue pairs of "
            "CORNER_PAIR_BASES and the swap-invariant method columns are allowed; "
            "select the feature columns before swapping."
        )
    half_pairs = sorted(
        column
        for column in present
        if column in _PARTNER and _PARTNER[column] not in present
    )
    if half_pairs:
        raise ValueError(
            f"swap_corners: incomplete corner pair(s) {half_pairs}: a pair column "
            "can only be swapped together with its other corner."
        )


def _negate(value: Any) -> Any:
    # Same rule as the 27-jun swap: None stays None, everything else is negated
    # (a float NaN negates to NaN).
    return -value if value is not None else None


def _negate_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(
        series
    ):
        return -series
    # object columns (None mixed with numbers): element by element, like a row.
    return series.map(
        lambda value: value
        if value is None or (isinstance(value, float) and math.isnan(value))
        else -value
    )


@overload
def swap_corners(data: pd.DataFrame) -> pd.DataFrame: ...


@overload
def swap_corners(data: Mapping[str, Any]) -> dict[str, Any]: ...


def swap_corners(data):
    """Mirror a feature row (a mapping) or a feature frame to the opposite corner
    assignment. Strict: raises ValueError on any column without a swap rule (ids,
    dates, targets included: select the feature columns first) and on half a
    pair. The input is not modified; keys / columns keep their order."""
    if isinstance(data, pd.DataFrame):
        _check_columns(data.columns)
        swapped_columns = {}
        for column in data.columns:
            if column.endswith(DIFF_SUFFIX):
                swapped_columns[column] = _negate_series(data[column]).to_numpy()
            elif column in _PARTNER:
                swapped_columns[column] = data[_PARTNER[column]].to_numpy()
            else:
                swapped_columns[column] = data[column].to_numpy()
        return pd.DataFrame(swapped_columns, index=data.index, columns=data.columns)

    _check_columns(data.keys())
    swapped: dict[str, Any] = {}
    for column, value in data.items():
        if column.endswith(DIFF_SUFFIX):
            swapped[column] = _negate(value)
        elif column in _PARTNER:
            swapped[column] = data[_PARTNER[column]]
        else:
            swapped[column] = value
    return swapped
