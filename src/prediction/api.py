from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.prediction.features import (
    DEFAULT_SCHEDULED_ROUNDS,
    FEATURE_COLUMNS,
    _coerce_scheduled_rounds,
    build_feature_row,
    compute_age,
    compute_fighter_history,
    load_base_dataframe,
    load_rankings_dataframe,
)
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


def model_trained_at(
    bundle: dict[str, Any], model_path: Path = MODEL_PATH
) -> str | None:
    """Training date for the UI. Prefer the value stamped into the bundle;
    fall back to the model file's mtime for older bundles that predate the key."""
    stamped = bundle.get("trained_at")
    if stamped:
        return str(stamped)
    try:
        mtime = os.path.getmtime(model_path)
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()


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


def _load_fighter_physical(database_url: str, fighter_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Load physical attributes straight from the fighters table.

    These (height_cm/reach_cm/birth_date) do not depend on fight history, so they
    remain available for debutants or fighters without fight_stats. Used by the
    degraded prediction path where the history-derived diffs are imputed."""
    placeholders = ", ".join(["%s"] * len(fighter_ids))
    query = f"""
        SELECT id, birth_date, height_cm, reach_cm
        FROM fighters
        WHERE id IN ({placeholders})
    """
    from src.scrapers.db import connect, cursor

    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute(query, fighter_ids)
            rows = db_cursor.fetchall()

    physical: dict[int, dict[str, Any]] = {}
    for row in rows:
        physical[int(row["id"])] = {
            "birth_date": row["birth_date"],
            "height_cm": float(row["height_cm"]) if row["height_cm"] is not None else None,
            "reach_cm": float(row["reach_cm"]) if row["reach_cm"] is not None else None,
        }
    return physical


def _get_latest_matchup_context(
    fights_df: pd.DataFrame, red_id: int, blue_id: int
) -> tuple[date, str | None, int]:
    """Resolve the real fight context for the matchup being predicted.

    Returns ``(matchup_date, weight_class, scheduled_rounds)``.

    When the two fighters have an actual bout on record (the fight being
    predicted), the temporal features are anchored to that bout's real
    ``event_date`` and its real ``scheduled_rounds`` are used instead of a
    hardcoded 3. A not-yet-decided bout (``winner_id`` IS NULL) is preferred over
    a past meeting, so an upcoming rematch anchors to the upcoming date rather
    than to the previous fight. For a pure hypothetical (no bout on record
    between the two) there is no real fight date or round count, so we anchor the
    temporal features to today ("if they fought now") and use a default round
    count, while still borrowing a weight class from each fighter's most recent
    bout so the ranking lookup can match the right division."""
    shared = fights_df[
        ((fights_df["fighter_red_id"] == red_id) | (fights_df["fighter_blue_id"] == red_id))
        & ((fights_df["fighter_red_id"] == blue_id) | (fights_df["fighter_blue_id"] == blue_id))
    ]
    if not shared.empty:
        # Prefer the still-unfought bout (the scheduled matchup); otherwise the
        # most recent meeting on record.
        upcoming = shared[shared["winner_id"].isna()]
        candidates = upcoming if not upcoming.empty else shared
        row = candidates.sort_values(["event_date", "fight_id"], ascending=[False, False]).iloc[0]
        return row["event_date"], row["weight_class"], _coerce_scheduled_rounds(row["scheduled_rounds"])

    latest = fights_df[
        (fights_df["fighter_red_id"].isin([red_id, blue_id]))
        | (fights_df["fighter_blue_id"].isin([red_id, blue_id]))
    ].sort_values(["event_date", "fight_id"], ascending=[False, False])
    if latest.empty:
        # Degraded path: neither fighter has any recorded fight (e.g. two
        # debutants). Fall back to today's date so physical features can still be
        # computed; the caller flags the prediction as low confidence.
        return date.today(), None, DEFAULT_SCHEDULED_ROUNDS
    # Hypothetical matchup with no bout on record: anchor temporal features to
    # today rather than to either fighter's last past fight, keep a weight class
    # for the ranking lookup, and use the default scheduled round count.
    return date.today(), latest.iloc[0]["weight_class"], DEFAULT_SCHEDULED_ROUNDS


def _build_feature_row(
    fights_df: pd.DataFrame,
    rankings_df: pd.DataFrame,
    red_id: int,
    blue_id: int,
    physical: dict[int, dict[str, Any]],
) -> tuple[dict[str, float | int | None], dict[str, Any], bool]:
    matchup_date, weight_class, scheduled_rounds = _get_latest_matchup_context(fights_df, red_id, blue_id)
    from src.prediction.features import build_fighter_history_dataframe

    history_df = build_fighter_history_dataframe(fights_df)

    red_history = compute_fighter_history(red_id, matchup_date, history_df, rankings_df, weight_class)
    blue_history = compute_fighter_history(blue_id, matchup_date, history_df, rankings_df, weight_class)

    # Degraded path: a debutant (0 fights) or a fighter without usable
    # fight_stats yields a None history. Instead of failing with a 422 we flag
    # the prediction as low confidence and leave the missing history diffs as
    # None so the model's median SimpleImputer fills them, while still using the
    # physical features (height/reach/age) sourced from the fighters table.
    low_confidence = red_history is None or blue_history is None

    red_phys = physical.get(red_id, {})
    blue_phys = physical.get(blue_id, {})
    red_height_cm = red_phys.get("height_cm")
    blue_height_cm = blue_phys.get("height_cm")
    red_reach_cm = red_phys.get("reach_cm")
    blue_reach_cm = blue_phys.get("reach_cm")
    red_age = compute_age(red_phys.get("birth_date"), matchup_date)
    blue_age = compute_age(blue_phys.get("birth_date"), matchup_date)

    # scheduled_rounds is already coerced by _get_latest_matchup_context. The
    # builder imputes nothing for a missing history (hist() -> None -> None diff),
    # so the imputer fills the training median downstream; the low_confidence flag
    # and imputation policy stay here in the caller, not in the shared builder.
    feature_row = build_feature_row(
        red_history,
        blue_history,
        red_height_cm=red_height_cm,
        blue_height_cm=blue_height_cm,
        red_reach_cm=red_reach_cm,
        blue_reach_cm=blue_reach_cm,
        red_age=red_age,
        blue_age=blue_age,
        scheduled_rounds=scheduled_rounds,
    )

    context = {
        "matchupDate": matchup_date.isoformat(),
        "weightClass": weight_class,
        "scheduledRounds": scheduled_rounds,
        "redHistory": asdict(red_history) if red_history is not None else None,
        "blueHistory": asdict(blue_history) if blue_history is not None else None,
        "lowConfidence": low_confidence,
    }
    return feature_row, context, low_confidence


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


def _swap_corners(feature_row: dict[str, float | int | None]) -> dict[str, float | int | None]:
    """Mirror a feature row to the opposite corner assignment.

    Every model feature is a red-minus-blue diff except ``scheduled_rounds``;
    swapping the two corners negates each diff (``diff(a, b) == -diff(b, a)``)
    and leaves the corner-independent scheduled round count untouched. A missing
    diff stays None so the imputer fills the same training median in both
    orientations. The None pattern is identical across corners (a diff is missing
    iff either fighter's value is missing), so this reproduces the genuine
    swapped row exactly."""
    swapped: dict[str, float | int | None] = {}
    for column, value in feature_row.items():
        if column.endswith("_diff") and value is not None:
            swapped[column] = -value
        else:
            swapped[column] = value
    return swapped


def _red_win_probability(
    feature_row: dict[str, float | int | None],
    imputer: Any,
    model: Any,
    feature_columns: list[str],
) -> tuple[float, np.ndarray]:
    """Return P(red wins) for a raw feature row plus the transformed matrix."""
    feature_frame = pd.DataFrame([{column: feature_row.get(column) for column in FEATURE_COLUMNS}])
    transformed = imputer.transform(feature_frame[feature_columns])
    probabilities = model.predict_proba(transformed)[0]
    return float(probabilities[1]), transformed


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
    physical = _load_fighter_physical(settings.database_url, [red_fighter_id, blue_fighter_id])
    feature_row, context, low_confidence = _build_feature_row(
        fights_df, rankings_df, red_fighter_id, blue_fighter_id, physical
    )
    feature_columns = bundle["feature_columns"]
    imputer = bundle["imputer"]
    model = bundle["model"]

    # Probability calibration (#17): when the bundle carries a fitted
    # `calibrator` (a prefit CalibratedClassifierCV over the frozen base model,
    # see calibrate.py) use it for the reported probabilities so they reflect the
    # observed win frequencies. Fall back to the raw model when no calibrator is
    # present (e.g. an older bundle). Feature importances still come from the base
    # model below. The calibrator is monotonic in the base score, so the corner
    # symmetry below is preserved exactly.
    proba_estimator = bundle.get("calibrator") or model

    # Corner symmetry (#26): the model was trained on raw red-blue diffs, so the
    # bare P(red wins) is not invariant to which fighter is labelled "red". We
    # average the forward estimate with the mirror estimate (the swapped row,
    # where every diff is negated). With red_sym = (p_forward + (1 - p_swapped)) /
    # 2 the prediction satisfies redProbability(A, B) == blueProbability(B, A)
    # exactly, so predict(A, B) and predict(B, A) sum to 1. Both terms pass
    # through the same estimator, so calibration keeps this identity.
    forward_red_prob, transformed = _red_win_probability(feature_row, imputer, proba_estimator, feature_columns)
    swapped_red_prob, _ = _red_win_probability(
        _swap_corners(feature_row), imputer, proba_estimator, feature_columns
    )
    red_probability = (forward_red_prob + (1.0 - swapped_red_prob)) / 2.0
    blue_probability = 1.0 - red_probability
    top_features = _compute_top_features(model, feature_columns, transformed, feature_row)
    profiles = _load_fighter_profiles(settings.database_url, [red_fighter_id, blue_fighter_id])

    return {
        "redProbability": red_probability,
        "blueProbability": blue_probability,
        "topFeatures": top_features,
        "featureValues": feature_row,
        "context": context,
        "lowConfidence": low_confidence,
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