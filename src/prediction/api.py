from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.prediction.features import FEATURE_COLUMNS, compute_age, compute_fighter_history, diff, load_base_dataframe, load_rankings_dataframe
from src.scrapers.config import get_settings


load_dotenv()

MODEL_PATH = Path("src/prediction/model.joblib")


class InsufficientHistoryError(RuntimeError):
    """Raised when two fighters lack enough fight history to build prediction features.
    Subclasses RuntimeError so existing CLI behaviour is unchanged; the HTTP service
    maps it to a 422 response."""


@dataclass(frozen=True)
class FighterPredictionProfile:
    id: int
    name: str
    nickname: str | None
    headshot_url: str | None
    wins: int
    losses: int
    draws: int
    height_cm: float | None
    reach_cm: float | None
    stance: str | None
    latest_weight_class: str | None
    aggregate_stats: dict[str, float]


def _load_model_bundle() -> dict[str, Any]:
    if not MODEL_PATH.exists():
        raise RuntimeError(f"Model file not found at {MODEL_PATH}")
    bundle = joblib.load(MODEL_PATH)
    if not isinstance(bundle, dict):
        raise RuntimeError("Unexpected model bundle format.")
    return bundle


def _load_fighter_profiles(database_url: str, fighter_ids: list[int]) -> dict[int, FighterPredictionProfile]:
    placeholders = ", ".join(["%s"] * len(fighter_ids))
    query = f"""
        SELECT
            f.id,
            f.name,
            f.nickname,
            f.headshot_url,
            f.wins,
            f.losses,
            f.draws,
            f.height_cm,
            f.reach_cm,
            f.stance,
            (
                SELECT fi.weight_class
                FROM fights fi
                WHERE fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id
                ORDER BY fi.updated_at DESC NULLS LAST, fi.id DESC
                LIMIT 1
            ) AS latest_weight_class,
            COALESCE(SUM(fs.sig_strikes_landed), 0) AS sig_strikes_landed,
            COALESCE(SUM(fs.sig_strikes_attempted), 0) AS sig_strikes_attempted,
            COALESCE(SUM(fs.takedowns_landed), 0) AS takedowns_landed,
            COALESCE(SUM(fs.takedowns_attempted), 0) AS takedowns_attempted,
            COALESCE(SUM(fs.submission_attempts), 0) AS submission_attempts,
            COALESCE(SUM(fs.control_time_seconds), 0) AS control_time_seconds,
            COALESCE(SUM(fs.knockdowns), 0) AS knockdowns,
            COUNT(fs.*) AS total_fight_stats
        FROM fighters f
        LEFT JOIN fight_stats fs ON fs.fighter_id = f.id
        WHERE f.id IN ({placeholders})
        GROUP BY f.id
    """
    from src.scrapers.db import connect, cursor

    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute(query, fighter_ids)
            rows = db_cursor.fetchall()

    profiles: dict[int, FighterPredictionProfile] = {}
    for row in rows:
        total_fight_stats = max(int(row["total_fight_stats"] or 0), 1)
        sig_attempted = float(row["sig_strikes_attempted"] or 0)
        td_attempted = float(row["takedowns_attempted"] or 0)
        sig_landed = float(row["sig_strikes_landed"] or 0)
        td_landed = float(row["takedowns_landed"] or 0)
        profiles[int(row["id"])] = FighterPredictionProfile(
            id=int(row["id"]),
            name=row["name"],
            nickname=row["nickname"],
            headshot_url=row["headshot_url"],
            wins=int(row["wins"]),
            losses=int(row["losses"]),
            draws=int(row["draws"]),
            height_cm=float(row["height_cm"]) if row["height_cm"] is not None else None,
            reach_cm=float(row["reach_cm"]) if row["reach_cm"] is not None else None,
            stance=row["stance"],
            latest_weight_class=row["latest_weight_class"],
            aggregate_stats={
                "sigStrikesLandedPerFight": sig_landed / total_fight_stats,
                "sigStrikeAccuracy": sig_landed / sig_attempted if sig_attempted > 0 else 0.0,
                "knockdownsPerFight": float(row["knockdowns"] or 0) / total_fight_stats,
                "takedownsLandedPerFight": td_landed / total_fight_stats,
                "takedownAccuracy": td_landed / td_attempted if td_attempted > 0 else 0.0,
                "submissionAttemptsPerFight": float(row["submission_attempts"] or 0) / total_fight_stats,
                "controlTimePerFightSeconds": float(row["control_time_seconds"] or 0) / total_fight_stats,
            },
        )
    return profiles


def _get_latest_matchup_context(fights_df: pd.DataFrame, red_id: int, blue_id: int) -> tuple[date, str | None]:
    shared = fights_df[
        ((fights_df["fighter_red_id"] == red_id) | (fights_df["fighter_blue_id"] == red_id))
        & ((fights_df["fighter_red_id"] == blue_id) | (fights_df["fighter_blue_id"] == blue_id))
    ].sort_values(["event_date", "fight_id"], ascending=[False, False])
    if not shared.empty:
        row = shared.iloc[0]
        return row["event_date"], row["weight_class"]

    latest = fights_df[
        (fights_df["fighter_red_id"].isin([red_id, blue_id]))
        | (fights_df["fighter_blue_id"].isin([red_id, blue_id]))
    ].sort_values(["event_date", "fight_id"], ascending=[False, False])
    if latest.empty:
        raise InsufficientHistoryError("Unable to determine matchup context from fight history.")
    row = latest.iloc[0]
    return row["event_date"], row["weight_class"]


def _build_feature_row(
    fights_df: pd.DataFrame,
    rankings_df: pd.DataFrame,
    red_id: int,
    blue_id: int,
) -> tuple[dict[str, float | int | None], dict[str, Any]]:
    matchup_date, weight_class = _get_latest_matchup_context(fights_df, red_id, blue_id)
    from src.prediction.features import build_fighter_history_dataframe

    history_df = build_fighter_history_dataframe(fights_df)

    red_row = fights_df[
        (fights_df["fighter_red_id"] == red_id) | (fights_df["fighter_blue_id"] == red_id)
    ].sort_values(["event_date", "fight_id"], ascending=[False, False]).iloc[0]
    blue_row = fights_df[
        (fights_df["fighter_red_id"] == blue_id) | (fights_df["fighter_blue_id"] == blue_id)
    ].sort_values(["event_date", "fight_id"], ascending=[False, False]).iloc[0]

    red_birth_date = red_row["red_birth_date"] if red_row["fighter_red_id"] == red_id else red_row["blue_birth_date"]
    blue_birth_date = blue_row["red_birth_date"] if blue_row["fighter_red_id"] == blue_id else blue_row["blue_birth_date"]
    red_height_cm = red_row["red_height_cm"] if red_row["fighter_red_id"] == red_id else red_row["blue_height_cm"]
    blue_height_cm = blue_row["red_height_cm"] if blue_row["fighter_red_id"] == blue_id else blue_row["blue_height_cm"]
    red_reach_cm = red_row["red_reach_cm"] if red_row["fighter_red_id"] == red_id else red_row["blue_reach_cm"]
    blue_reach_cm = blue_row["red_reach_cm"] if blue_row["fighter_red_id"] == blue_id else blue_row["blue_reach_cm"]

    red_history = compute_fighter_history(red_id, matchup_date, history_df, rankings_df, weight_class)
    blue_history = compute_fighter_history(blue_id, matchup_date, history_df, rankings_df, weight_class)
    if red_history is None or blue_history is None:
        raise InsufficientHistoryError("Insufficient fighter history to generate prediction features.")

    red_age = compute_age(red_birth_date, matchup_date)
    blue_age = compute_age(blue_birth_date, matchup_date)

    feature_row = {
        "height_cm_diff": diff(red_height_cm, blue_height_cm),
        "reach_cm_diff": diff(red_reach_cm, blue_reach_cm),
        "age_diff": diff(red_age, blue_age),
        "sig_strikes_landed_per_fight_diff": diff(red_history.sig_strikes_landed_per_fight, blue_history.sig_strikes_landed_per_fight),
        "sig_strike_accuracy_diff": diff(red_history.sig_strike_accuracy, blue_history.sig_strike_accuracy),
        "knockdowns_per_fight_diff": diff(red_history.knockdowns_per_fight, blue_history.knockdowns_per_fight),
        "takedowns_landed_per_fight_diff": diff(red_history.takedowns_landed_per_fight, blue_history.takedowns_landed_per_fight),
        "takedown_accuracy_diff": diff(red_history.takedown_accuracy, blue_history.takedown_accuracy),
        "submission_attempts_per_fight_diff": diff(red_history.submission_attempts_per_fight, blue_history.submission_attempts_per_fight),
        "control_time_seconds_per_fight_diff": diff(red_history.control_time_seconds_per_fight, blue_history.control_time_seconds_per_fight),
        "win_streak_diff": diff(red_history.win_streak, blue_history.win_streak),
        "wins_last_5_diff": diff(red_history.wins_last_5, blue_history.wins_last_5),
        "total_prior_fights_diff": diff(red_history.total_prior_fights, blue_history.total_prior_fights),
        "total_rounds_fought_diff": diff(red_history.total_rounds_fought, blue_history.total_rounds_fought),
        "pct_wins_by_ko_diff": diff(red_history.pct_wins_by_ko, blue_history.pct_wins_by_ko),
        "pct_wins_by_submission_diff": diff(red_history.pct_wins_by_submission, blue_history.pct_wins_by_submission),
        "pct_wins_by_decision_diff": diff(red_history.pct_wins_by_decision, blue_history.pct_wins_by_decision),
        "scheduled_rounds": 3,
        "days_since_last_fight_diff": diff(red_history.days_since_last_fight, blue_history.days_since_last_fight),
        "ranking_position_diff": diff(red_history.ranking_position, blue_history.ranking_position),
    }

    context = {
        "matchupDate": matchup_date.isoformat(),
        "weightClass": weight_class,
        "redHistory": asdict(red_history),
        "blueHistory": asdict(blue_history),
    }
    return feature_row, context


def _compute_top_features(
    model: Any,
    feature_columns: list[str],
    transformed_row: np.ndarray,
    raw_feature_row: dict[str, Any],
) -> list[dict[str, Any]]:
    importances = getattr(model, "feature_importances_", None)
    if importances is None:
        return []
    contributions = []
    for index, feature_name in enumerate(feature_columns):
        feature_value = float(transformed_row[0][index])
        importance = float(importances[index])
        contributions.append(
            {
                "name": feature_name,
                "value": raw_feature_row.get(feature_name),
                "importance": importance,
                "impact": abs(feature_value * importance),
            }
        )
    contributions.sort(key=lambda item: item["impact"], reverse=True)
    return contributions[:5]


def predict(
    red_fighter_id: int,
    blue_fighter_id: int,
    *,
    bundle: dict[str, Any] | None = None,
    fights_df: pd.DataFrame | None = None,
    rankings_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if bundle is None:
        bundle = _load_model_bundle()
    if fights_df is None:
        fights_df = load_base_dataframe(settings.database_url)
    if rankings_df is None:
        rankings_df = load_rankings_dataframe(settings.database_url)
    feature_row, context = _build_feature_row(fights_df, rankings_df, red_fighter_id, blue_fighter_id)
    feature_columns = bundle["feature_columns"]
    imputer = bundle["imputer"]
    model = bundle["model"]

    feature_frame = pd.DataFrame([{column: feature_row.get(column) for column in FEATURE_COLUMNS}])
    transformed = imputer.transform(feature_frame[feature_columns])
    probabilities = model.predict_proba(transformed)[0]
    red_probability = float(probabilities[1])
    blue_probability = float(probabilities[0])
    top_features = _compute_top_features(model, feature_columns, transformed, feature_row)
    profiles = _load_fighter_profiles(settings.database_url, [red_fighter_id, blue_fighter_id])

    return {
        "redProbability": red_probability,
        "blueProbability": blue_probability,
        "topFeatures": top_features,
        "featureValues": feature_row,
        "context": context,
        "fighters": {
            "red": asdict(profiles[red_fighter_id]),
            "blue": asdict(profiles[blue_fighter_id]),
        },
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--red", type=int, required=True)
    parser.add_argument("--blue", type=int, required=True)
    args = parser.parse_args()

    result = predict(args.red, args.blue)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()