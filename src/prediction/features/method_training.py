"""Training-dataset builder for the win-METHOD model.

Mirrors ``training.build_training_dataset`` (same leak-free per-corner history
computation, same missing-history/missing-stats exclusion policy) but labels
each fight with HOW it ended instead of WHO won:

* target = index into ``METHOD_CLASSES`` (decision / ko / submission), derived
  from ``fights.method`` via ``classify_win_method``.
* Draws COUNT as decisions (method is a scorecard code, winner_id is NULL), so
  no winner is required — the method outcome exists regardless.
* CNC / DQ / Overturned / upcoming (NULL method) rows are excluded: they have
  no clean method outcome to learn.

The exclusion policy for histories/stats deliberately duplicates the winner
pipeline's checks instead of importing its loop: ``training.py`` is the frozen
winner pipeline ("DO NOT MODIFY" territory) and sharing internals would couple
the two lifecycles. The golden tests pin both policies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .classification import classify_win_method
from .feature_engineering import build_feature_row
from .fighter_history import build_fighter_history_dataframe, compute_fighter_history
from .method_features import METHOD_CLASSES, build_method_feature_row
from .metrics import compute_age


@dataclass(frozen=True)
class MethodDatasetBuildResult:
    dataset: pd.DataFrame
    total_fights_seen: int
    excluded_no_method: int
    excluded_missing_history: int
    excluded_missing_stats: int


def _missing_core_stats(red_history, blue_history) -> bool:
    """Same core per-fight aggregate requirement as the winner pipeline."""
    return any(
        value is None
        for value in (
            red_history.sig_strikes_landed_per_fight,
            red_history.sig_strike_accuracy,
            red_history.knockdowns_per_fight,
            red_history.takedowns_landed_per_fight,
            red_history.takedown_accuracy,
            red_history.submission_attempts_per_fight,
            red_history.control_time_seconds_per_fight,
            blue_history.sig_strikes_landed_per_fight,
            blue_history.sig_strike_accuracy,
            blue_history.knockdowns_per_fight,
            blue_history.takedowns_landed_per_fight,
            blue_history.takedown_accuracy,
            blue_history.submission_attempts_per_fight,
            blue_history.control_time_seconds_per_fight,
        )
    )


def build_method_training_dataset(
    fights_df: pd.DataFrame, rankings_df: pd.DataFrame
) -> MethodDatasetBuildResult:
    history_df = build_fighter_history_dataframe(fights_df)
    dataset_rows: list[dict[str, Any]] = []
    excluded_no_method = 0
    excluded_missing_history = 0
    excluded_missing_stats = 0

    for row in fights_df.to_dict("records"):
        method_class = classify_win_method(row["method"])
        if method_class is None:
            excluded_no_method += 1
            continue
        red_history = compute_fighter_history(
            fighter_id=row["fighter_red_id"],
            current_event_date=row["event_date"],
            history_df=history_df,
            rankings_df=rankings_df,
            weight_class=row["weight_class"],
        )
        blue_history = compute_fighter_history(
            fighter_id=row["fighter_blue_id"],
            current_event_date=row["event_date"],
            history_df=history_df,
            rankings_df=rankings_df,
            weight_class=row["weight_class"],
        )
        if red_history is None or blue_history is None:
            excluded_missing_history += 1
            continue
        if _missing_core_stats(red_history, blue_history):
            excluded_missing_stats += 1
            continue

        base_row = build_feature_row(
            red_history,
            blue_history,
            red_height_cm=row["red_height_cm"],
            blue_height_cm=row["blue_height_cm"],
            red_reach_cm=row["red_reach_cm"],
            blue_reach_cm=row["blue_reach_cm"],
            red_age=compute_age(row["red_birth_date"], row["event_date"]),
            blue_age=compute_age(row["blue_birth_date"], row["event_date"]),
        )
        method_row = build_method_feature_row(
            base_row,
            red_history,
            blue_history,
            scheduled_rounds=row["scheduled_rounds"],
            weight_class=row["weight_class"],
        )
        dataset_rows.append(
            {
                "fight_id": row["fight_id"],
                "event_date": row["event_date"],
                **method_row,
                "target": METHOD_CLASSES.index(method_class),
            }
        )

    dataset = pd.DataFrame.from_records(dataset_rows)
    if not dataset.empty:
        dataset["event_date"] = pd.to_datetime(dataset["event_date"]).dt.date
        dataset = dataset.sort_values(["event_date", "fight_id"]).reset_index(drop=True)
    return MethodDatasetBuildResult(
        dataset=dataset,
        total_fights_seen=len(fights_df),
        excluded_no_method=excluded_no_method,
        excluded_missing_history=excluded_missing_history,
        excluded_missing_stats=excluded_missing_stats,
    )
