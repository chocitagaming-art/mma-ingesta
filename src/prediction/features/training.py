from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date
from typing import Any

import pandas as pd

from .classification import classify_target
from .feature_engineering import build_feature_row
from .fighter_history import (
    build_fighter_history_dataframe,
    compute_fighter_history,
    count_prior_ufc_fights,
)
from .metrics import compute_age
from .preufc import build_preufc_block, preufc_diff_values
from .types import (
    UFC_COUNT_COLUMNS,
    WINNER_FEATURE_COLUMNS,
    DatasetBuildResult,
    FighterHistorySummary,
    SPOT_CHECK_COUNT,
)

# The winner CSV (v2, phase 4), column by column.
WINNER_CSV_COLUMNS = ["fight_id", "event_date", *WINNER_FEATURE_COLUMNS, "target"]

# The 7 per-corner aggregates the pre-phase-4 gate required (14 values per
# fight). Only the two accuracies can actually be None: the rest divide by
# stats_fight_count, which is at least 1 whenever a summary exists.
_GATED_STATS = (
    "sig_strikes_landed_per_fight",
    "sig_strike_accuracy",
    "knockdowns_per_fight",
    "takedowns_landed_per_fight",
    "takedown_accuracy",
    "submission_attempts_per_fight",
    "control_time_seconds_per_fight",
)


def _has_missing_stats(
    red_history: FighterHistorySummary, blue_history: FighterHistorySummary
) -> bool:
    return any(
        getattr(history, attribute) is None
        for history in (red_history, blue_history)
        for attribute in _GATED_STATS
    )


def _latest_prior_fight_date(history: FighterHistorySummary | None) -> date | None:
    return history.latest_prior_fight_date if history is not None else None


def _iso_or_none(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _spot_check(
    row: dict[str, Any],
    red_history: FighterHistorySummary | None,
    blue_history: FighterHistorySummary | None,
    red_prior_fights: int,
    blue_prior_fights: int,
) -> dict[str, Any]:
    """Leak check of one row; a corner without a summary has no latest date.

    The prior-fight counts come from count_prior_ufc_fights, which equals
    total_prior_fights whenever the summary exists and is 0 for a debutant."""
    event_date = row["event_date"]
    red_latest = _latest_prior_fight_date(red_history)
    blue_latest = _latest_prior_fight_date(blue_history)
    return {
        "fight_id": row["fight_id"],
        "event_date": event_date.isoformat(),
        "red_prior_fights": red_prior_fights,
        "blue_prior_fights": blue_prior_fights,
        "red_latest_prior_fight_date": _iso_or_none(red_latest),
        "blue_latest_prior_fight_date": _iso_or_none(blue_latest),
        "used_only_prior_data": (
            (red_latest is None or red_latest < event_date)
            and (blue_latest is None or blue_latest < event_date)
        ),
    }


def build_training_dataset(
    fights_df: pd.DataFrame,
    rankings_df: pd.DataFrame,
    *,
    espn_by_fighter: Mapping[int, pd.DataFrame],
    known_fighter_ids: Collection[int],
) -> DatasetBuildResult:
    """The winner CSV v2: WINNER_CSV_COLUMNS, one row per fight with a winner.

    ``espn_by_fighter`` (db.index_espn_history) and ``known_fighter_ids`` feed the
    pre-UFC block. They are required on purpose: whoever builds the CSV says which
    ESPN history it reads (the database, or a preufc_snapshot), and nobody gets the
    block silently empty."""
    history_df = build_fighter_history_dataframe(fights_df)
    dataset_rows: list[dict[str, Any]] = []
    spot_checks: list[dict[str, Any]] = []
    excluded_no_target = 0
    # Phase 4: a missing summary or an accuracy None no longer drops the fight.
    # These count the rows each old gate used to exclude, so the log still tells
    # the two populations apart.
    included_no_ufc_history = 0
    included_nan_stats = 0

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
        # A corner without a summary (a debutant, or prior fights without
        # fight_stats) gets every history diff None from build_feature_row, and an
        # accuracy None (zero attempts) leaves just its own diff None: NaN in the
        # CSV. The physical diffs are computed as always.
        if red_history is None or blue_history is None:
            included_no_ufc_history += 1
        elif _has_missing_stats(red_history, blue_history):
            included_nan_stats += 1

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
        # Prior UFC fights per corner (0 for a debutant, never NaN): with the
        # history diffs NaN, this is what says which corner is the new one.
        red_ufc_prev = count_prior_ufc_fights(
            history_df, row["fighter_red_id"], row["event_date"]
        )
        blue_ufc_prev = count_prior_ufc_fights(
            history_df, row["fighter_blue_id"], row["event_date"]
        )
        # Pre-UFC block: ESPN rows strictly before the date of the bout, and the
        # nine None of an unknown corner (preufc.build_preufc_block).
        preufc_block = build_preufc_block(
            row["fighter_red_id"],
            row["fighter_blue_id"],
            row["event_date"],
            espn_by_fighter,
            known_fighter_ids,
        )
        # Raw red-blue diffs (NOT oriented by target). Orienting by target
        # canonicalizes every row to winner-loser diffs, which makes the label
        # unlearnable and mismatches inference (api.py uses raw red-blue diffs).
        dataset_rows.append(
            {
                "fight_id": row["fight_id"],
                "event_date": row["event_date"],
                **feature_row,
                **dict(zip(UFC_COUNT_COLUMNS, (red_ufc_prev, blue_ufc_prev))),
                **preufc_block,
                **preufc_diff_values(preufc_block),
                "target": target,
            }
        )

        # Leak checks only where both corners have a UFC summary: on a debutant
        # corner there is no prior date to compare.
        has_both_summaries = red_history is not None and blue_history is not None
        if has_both_summaries and len(spot_checks) < SPOT_CHECK_COUNT:
            spot_checks.append(
                _spot_check(row, red_history, blue_history, red_ufc_prev, blue_ufc_prev)
            )

    dataset = pd.DataFrame.from_records(dataset_rows)
    if dataset.empty:
        return DatasetBuildResult(
            dataset=dataset,
            spot_checks=spot_checks,
            total_fights_seen=len(fights_df),
            excluded_no_target=excluded_no_target,
            excluded_missing_history=0,
            excluded_missing_stats=0,
            included_no_ufc_history=included_no_ufc_history,
            included_nan_stats=included_nan_stats,
        )
    dataset["event_date"] = pd.to_datetime(dataset["event_date"]).dt.date
    dataset = dataset[WINNER_CSV_COLUMNS]
    return DatasetBuildResult(
        dataset=dataset.sort_values(["event_date", "fight_id"]).reset_index(drop=True),
        spot_checks=spot_checks,
        total_fights_seen=len(fights_df),
        excluded_no_target=excluded_no_target,
        excluded_missing_history=0,
        excluded_missing_stats=0,
        included_no_ufc_history=included_no_ufc_history,
        included_nan_stats=included_nan_stats,
    )
