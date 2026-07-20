"""Feature layer for the win-METHOD model (KO/TKO vs submission vs decision).

The method model reuses the WINNER model's 20 red-minus-blue diffs (how the
corners differ) and adds symmetric pair-level signals (how the PAIRING tends to
end): finishing-rate sums, grappling volume sums, the scheduled round count and
the division's weight/gender. Diffs negate under a corner swap while every
added feature is swap-invariant, so serving can symmetrize exactly by averaging
the forward and swapped predictions per class (same trick as the winner model).

``build_method_feature_row`` is the SINGLE SOURCE OF TRUTH shared by the
training pipeline (``method_training.build_method_training_dataset``) and
serving (``api.predict``), mirroring ``build_feature_row`` for the winner model.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .metrics import _coerce_scheduled_rounds, pair_sum
from .types import FEATURE_COLUMNS, FighterHistorySummary

# Fixed class order: the integer target in method_training_dataset.csv is the
# index into this list, and the bundle persists it as ``method_classes`` so
# serving never has to guess the mapping.
METHOD_CLASSES = ["decision", "ko", "submission"]

# Sum feature -> the FighterHistorySummary attribute BOTH corners contribute.
METHOD_SUM_FEATURES = {
    "pct_wins_by_ko_sum": "pct_wins_by_ko",
    "pct_wins_by_submission_sum": "pct_wins_by_submission",
    "pct_wins_by_decision_sum": "pct_wins_by_decision",
    "knockdowns_per_fight_sum": "knockdowns_per_fight",
    "submission_attempts_per_fight_sum": "submission_attempts_per_fight",
    "takedowns_landed_per_fight_sum": "takedowns_landed_per_fight",
    "control_time_seconds_per_fight_sum": "control_time_seconds_per_fight",
    "sig_strikes_landed_per_fight_sum": "sig_strikes_landed_per_fight",
    "total_prior_fights_sum": "total_prior_fights",
    # Domain signals: the block above is all OFFENCE (how these two win), which
    # says little about how the pairing ENDS. These say how they lose and how
    # long their fights last, which is the more direct signal. Measured
    # walk-forward, they carry about half of the model's total improvement.
    "pct_losses_by_ko_sum": "pct_losses_by_ko",
    "pct_losses_by_submission_sum": "pct_losses_by_submission",
    "avg_fight_duration_s_sum": "avg_fight_duration_s",
    "pct_went_the_distance_sum": "pct_went_the_distance",
}

METHOD_FEATURE_COLUMNS = [
    *FEATURE_COLUMNS,
    *METHOD_SUM_FEATURES,
    "scheduled_rounds",
    "weight_kg",
    "is_female_division",
    # Property of the BOUT, so swap-invariant like every other non-diff column.
    "is_title_fight",
]

# Upper limit of each division in kg (UFC rulebook). Divisions without a fixed
# limit (Open/Catch/Super Heavyweight) map to None so the imputer fills the
# training median instead of a made-up number.
_WEIGHT_CLASS_KG = {
    "strawweight": 52.2,
    "flyweight": 56.7,
    "bantamweight": 61.2,
    "featherweight": 65.8,
    "lightweight": 70.3,
    "welterweight": 77.1,
    "middleweight": 83.9,
    "light heavyweight": 93.0,
    "heavyweight": 120.2,
}

_WOMEN_PREFIXES = ("women's ", "womens ", "women ")


def parse_weight_class(weight_class: Any) -> tuple[float | None, float | None]:
    """``(weight_kg, is_female_division)`` from a ``fights.weight_class`` label.

    NULL/NaN labels yield ``(None, None)``; a known label without a fixed weight
    limit keeps the gender flag but leaves the kg unknown."""
    # NULL weight_class arrives from the DB as float NaN; the isinstance guard
    # covers NaN, None and anything else non-string.
    if not isinstance(weight_class, str) or not weight_class.strip():
        return None, None
    normalized = weight_class.strip().lower().replace("’", "'")
    is_female = 1.0 if normalized.startswith("women") else 0.0
    for prefix in _WOMEN_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    return _WEIGHT_CLASS_KG.get(normalized), is_female


# SINGLE SOURCE OF TRUTH (train + serve): keep in lockstep with METHOD_FEATURE_COLUMNS
def build_method_feature_row(
    base_feature_row: dict[str, float | int | None],
    red_history: FighterHistorySummary | None,
    blue_history: FighterHistorySummary | None,
    *,
    scheduled_rounds: Any,
    weight_class: Any,
    is_title_fight: Any = None,
) -> dict[str, float | int | None]:
    """Method-model feature row (keys == METHOD_FEATURE_COLUMNS, in order).

    ``base_feature_row`` is the winner model's ``build_feature_row`` output for
    the same matchup; it passes through untouched, so the two rows can never
    drift. Like the winner builder this imputes nothing: a missing side keeps
    the sums None and the caller's imputer decides."""

    def hist(history: FighterHistorySummary | None, attribute: str) -> Any:
        return getattr(history, attribute) if history is not None else None

    weight_kg, is_female_division = parse_weight_class(weight_class)
    row: dict[str, float | int | None] = dict(base_feature_row)
    for feature_name, attribute in METHOD_SUM_FEATURES.items():
        row[feature_name] = pair_sum(hist(red_history, attribute), hist(blue_history, attribute))
    row["scheduled_rounds"] = _coerce_scheduled_rounds(scheduled_rounds)
    row["weight_kg"] = weight_kg
    row["is_female_division"] = is_female_division
    # NULL stays None so the imputer fills the training median, same policy as
    # weight_kg — never invent a False for "we don't know".
    row["is_title_fight"] = (
        None if is_title_fight is None or pd.isna(is_title_fight) else float(bool(is_title_fight))
    )
    return row
