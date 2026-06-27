from __future__ import annotations

from typing import Any

import pandas as pd

from .classification import classify_target
from .feature_engineering import build_feature_row
from .fighter_history import build_fighter_history_dataframe, compute_fighter_history
from .metrics import compute_age
from .types import DatasetBuildResult, SPOT_CHECK_COUNT


def build_training_dataset(fights_df: pd.DataFrame, rankings_df: pd.DataFrame) -> DatasetBuildResult:
    history_df = build_fighter_history_dataframe(fights_df)
    dataset_rows: list[dict[str, Any]] = []
    spot_checks: list[dict[str, Any]] = []
    excluded_no_target = 0
    excluded_missing_history = 0
    excluded_missing_stats = 0

    for row in fights_df.to_dict("records"):
        target = classify_target(pd.Series(row))
        if target is None:
            excluded_no_target += 1
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
        if any(
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
        ):
            excluded_missing_stats += 1
            continue

        red_age = compute_age(row["red_birth_date"], row["event_date"])
        blue_age = compute_age(row["blue_birth_date"], row["event_date"])

        feature_row = build_feature_row(
            red_history,
            blue_history,
            red_height_cm=row["red_height_cm"],
            blue_height_cm=row["blue_height_cm"],
            red_reach_cm=row["red_reach_cm"],
            blue_reach_cm=row["blue_reach_cm"],
            red_age=red_age,
            blue_age=blue_age,
        )
        # Raw red-blue diffs (NOT oriented by target). Orienting by target
        # canonicalizes every row to winner-loser diffs, which makes the label
        # unlearnable and mismatches inference (api.py uses raw red-blue diffs).
        dataset_rows.append(
            {
                "fight_id": row["fight_id"],
                "event_date": row["event_date"],
                **feature_row,
                "target": target,
            }
        )

        if len(spot_checks) < SPOT_CHECK_COUNT:
            spot_checks.append(
                {
                    "fight_id": row["fight_id"],
                    "event_date": row["event_date"].isoformat(),
                    "red_prior_fights": red_history.total_prior_fights,
                    "blue_prior_fights": blue_history.total_prior_fights,
                    "red_latest_prior_fight_date": red_history.latest_prior_fight_date.isoformat()
                    if red_history.latest_prior_fight_date
                    else None,
                    "blue_latest_prior_fight_date": blue_history.latest_prior_fight_date.isoformat()
                    if blue_history.latest_prior_fight_date
                    else None,
                    "used_only_prior_data": (
                        (red_history.latest_prior_fight_date is None or red_history.latest_prior_fight_date < row["event_date"])
                        and (blue_history.latest_prior_fight_date is None or blue_history.latest_prior_fight_date < row["event_date"])
                    ),
                }
            )

    dataset = pd.DataFrame.from_records(dataset_rows)
    if dataset.empty:
        return DatasetBuildResult(
            dataset=dataset,
            spot_checks=spot_checks,
            total_fights_seen=len(fights_df),
            excluded_no_target=excluded_no_target,
            excluded_missing_history=excluded_missing_history,
            excluded_missing_stats=excluded_missing_stats,
        )
    dataset["event_date"] = pd.to_datetime(dataset["event_date"]).dt.date
    return DatasetBuildResult(
        dataset=dataset.sort_values(["event_date", "fight_id"]).reset_index(drop=True),
        spot_checks=spot_checks,
        total_fights_seen=len(fights_df),
        excluded_no_target=excluded_no_target,
        excluded_missing_history=excluded_missing_history,
        excluded_missing_stats=excluded_missing_stats,
    )
