from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

import joblib
import numpy as np
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.prediction.bundle_io import discard_stale_calibrators
from src.prediction.corners import CORNER_PAIRS, PAIR_BASE_BY_COLUMN, swap_corners
from src.prediction.features import (
    DEFAULT_SCHEDULED_ROUNDS,
    FighterHistorySummary,
    _coerce_scheduled_rounds,
    build_feature_row,
    compute_age,
    compute_fighter_history,
    load_base_dataframe,
    load_rankings_dataframe,
)
from src.prediction.features.method_features import (
    METHOD_CLASSES,
    METHOD_FEATURE_COLUMNS,
    build_method_feature_row,
)
from src.scrapers.config import get_settings


load_dotenv()

MODEL_PATH = Path("src/prediction/model.joblib")

# A prediction is flagged low confidence when either fighter has fewer than this
# many prior fights (or no usable history at all). Thin history makes the
# history-derived diffs noisy, so the UI warns instead of presenting the number
# as solid. Still returned as a normal 200 response, just with lowConfidence:true.
MIN_CONFIDENT_FIGHTS = 3

LOGGER = logging.getLogger("prediction.api")


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
    # A calibrator carries its OWN copy of the model it was fitted on, and the
    # predict paths serve `calibrator or model`: one left over from an older model
    # would silently serve THAT model. Drop any calibrator that does not wrap the
    # model next to it, so the raw model is served instead (logged as an ERROR).
    # Checked here, once per load, never per prediction.
    discard_stale_calibrators(bundle)
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


AnchorKind = Literal["fight", "pending", "today", "none"]


class MatchupContext(NamedTuple):
    """The fight context a prediction is anchored to.

    ``anchor`` says which rule picked it and travels to the web as
    ``context.anchor``: "fight" (the bout the caller asked for by id),
    "pending" (the pair's scheduled bout), "today" (no bout to anchor to) or
    "none" (neither fighter has any fight on record). ``anchor_fight_id`` is
    the bout it anchored to (``context.anchorFightId``), None for "today" and
    "none"."""

    matchup_date: date
    weight_class: str | None
    scheduled_rounds: int
    is_title_fight: bool | None
    anchor: AnchorKind
    anchor_fight_id: int | None


def _anchored_to_bout(row: pd.Series, anchor: AnchorKind) -> MatchupContext:
    """Context of a real bout on record: its own date, division, rounds and title."""
    return MatchupContext(
        row["event_date"],
        row["weight_class"],
        _coerce_scheduled_rounds(row["scheduled_rounds"]),
        row.get("is_title_fight"),
        anchor,
        int(row["fight_id"]),
    )


def _get_latest_matchup_context(
    fights_df: pd.DataFrame,
    red_id: int,
    blue_id: int,
    fight_id: int | None = None,
) -> MatchupContext:
    """Resolve the fight context for the matchup being predicted.

    Returns a ``MatchupContext``: ``(matchup_date, weight_class,
    scheduled_rounds, is_title_fight, anchor, anchor_fight_id)``.

    ``fight_id`` (owner decision, 4-oct-2026): the fight page asks for the
    prediction OF ITS OWN FIGHT. When that bout is in ``fights_df`` and is
    between exactly these two fighters (either corner order), the prediction is
    anchored to it, even if it is already decided: the strict history cut keeps
    its own result (and everything after it) out of both histories. Without
    this, a decided bout with nothing pending is predicted "today" with its own
    result in the history (UFC 332, after the results: the model favourite
    flipped to the real winner in 6 of 12 bouts). Any other ``fight_id``
    (unknown, cancelled -> the loader dropped it, a bout of other fighters) is
    ignored and the pair rule below applies: a cancelled bout has no result, so
    falling back cannot leak one.

    Only a PENDING bout between the two fighters (``winner_id`` AND ``method``
    NULL: the scheduled matchup) is a real fight to anchor to. The temporal
    features are then anchored to its real ``event_date`` and its real weight
    class, ``scheduled_rounds`` and title status are used; with several pending,
    the latest one. A draw or a no contest has no winner either, but its method
    is set (M-DEC, S-DEC, CNC, Overturned...), so it is a past meeting.

    Every other pair is predicted "as if they fought today" (owner decision nº 11,
    4-oct-2026): a pure hypothetical, a pair whose only bout was cancelled (the
    loader drops those) AND a pair that already met but has nothing pending. A
    past meeting is part of both fighters' history, not the anchor: anchoring to
    it showed a 2017 date for Moicano-Ortega and computed every feature as it was
    back then. There is no real bout to borrow from, so the round count is the
    default, the title status is unknown (None, the imputer fills the training
    median) and the weight class comes from the most recent bout of either
    fighter, so the ranking lookup can match the division they fight at now.

    The history cut is strict (``event_date < matchup_date``) and ``date.today()``
    is the server's (UTC on Render): a meeting dated today is not in the history
    until tomorrow."""
    if fight_id is not None:
        requested = fights_df[fights_df["fight_id"] == fight_id]
        requested = requested[
            ((requested["fighter_red_id"] == red_id) & (requested["fighter_blue_id"] == blue_id))
            | ((requested["fighter_red_id"] == blue_id) & (requested["fighter_blue_id"] == red_id))
        ]
        if not requested.empty:
            return _anchored_to_bout(requested.iloc[0], "fight")

    shared = fights_df[
        ((fights_df["fighter_red_id"] == red_id) | (fights_df["fighter_blue_id"] == red_id))
        & ((fights_df["fighter_red_id"] == blue_id) | (fights_df["fighter_blue_id"] == blue_id))
    ]
    # `method` is only read when the pair has met: the degraded frames of a
    # no-history prediction need not carry the column.
    if not shared.empty:
        pending = shared[shared["winner_id"].isna() & shared["method"].isna()]
        if not pending.empty:
            row = pending.sort_values(["event_date", "fight_id"], ascending=[False, False]).iloc[0]
            return _anchored_to_bout(row, "pending")

    latest = fights_df[
        (fights_df["fighter_red_id"].isin([red_id, blue_id]))
        | (fights_df["fighter_blue_id"].isin([red_id, blue_id]))
    ].sort_values(["event_date", "fight_id"], ascending=[False, False])
    if latest.empty:
        # Degraded path: neither fighter has any recorded fight (e.g. two
        # debutants). Today's date still lets the physical features be
        # computed; the caller flags the prediction as low confidence.
        return MatchupContext(date.today(), None, DEFAULT_SCHEDULED_ROUNDS, None, "none", None)
    return MatchupContext(
        date.today(), latest.iloc[0]["weight_class"], DEFAULT_SCHEDULED_ROUNDS, None, "today", None
    )


def _is_low_confidence(
    red_history: FighterHistorySummary | None,
    blue_history: FighterHistorySummary | None,
) -> bool:
    """Flag a prediction as low confidence when either side lacks enough history.

    A None history means a debutant or a fighter without usable fight_stats; a
    thin history (fewer than MIN_CONFIDENT_FIGHTS prior fights) still produces a
    number but a noisy one. Both cases keep returning a normal 200 response, just
    with lowConfidence:true so the UI can warn."""
    if red_history is None or blue_history is None:
        return True
    return (
        red_history.total_prior_fights < MIN_CONFIDENT_FIGHTS
        or blue_history.total_prior_fights < MIN_CONFIDENT_FIGHTS
    )


def _build_feature_row(
    fights_df: pd.DataFrame,
    rankings_df: pd.DataFrame,
    red_id: int,
    blue_id: int,
    physical: dict[int, dict[str, Any]],
    history_df: pd.DataFrame | None = None,
    fight_id: int | None = None,
) -> tuple[dict[str, float | int | None], dict[str, float | int | None], dict[str, Any], bool]:
    matchup = _get_latest_matchup_context(fights_df, red_id, blue_id, fight_id=fight_id)
    matchup_date, weight_class, scheduled_rounds, is_title_fight = matchup[:4]
    from src.prediction.features import build_fighter_history_dataframe

    # The long-lived service builds this once per data refresh and threads it in;
    # the CLI path passes nothing and we build it on demand (no signature break).
    if history_df is None:
        history_df = build_fighter_history_dataframe(fights_df)

    red_history = compute_fighter_history(red_id, matchup_date, history_df, rankings_df, weight_class)
    blue_history = compute_fighter_history(blue_id, matchup_date, history_df, rankings_df, weight_class)

    # Degraded path: a debutant (0 fights) or a fighter without usable
    # fight_stats yields a None history; a thin history is also flagged. Instead
    # of failing we mark the prediction low confidence and leave the missing
    # history diffs as None so the model's median SimpleImputer fills them, while
    # still using the physical features (height/reach/age) from the fighters table.
    low_confidence = _is_low_confidence(red_history, blue_history)

    red_phys = physical.get(red_id, {})
    blue_phys = physical.get(blue_id, {})
    red_height_cm = red_phys.get("height_cm")
    blue_height_cm = blue_phys.get("height_cm")
    red_reach_cm = red_phys.get("reach_cm")
    blue_reach_cm = blue_phys.get("reach_cm")
    red_age = compute_age(red_phys.get("birth_date"), matchup_date)
    blue_age = compute_age(blue_phys.get("birth_date"), matchup_date)

    # scheduled_rounds is no longer a model feature (dropped as zero-importance);
    # it is kept only for the context payload below. The builder imputes nothing
    # for a missing history (hist() -> None -> None diff), so the imputer fills the
    # training median downstream; the low_confidence flag and imputation policy
    # stay here in the caller, not in the shared builder.
    feature_row = build_feature_row(
        red_history,
        blue_history,
        red_height_cm=red_height_cm,
        blue_height_cm=blue_height_cm,
        red_reach_cm=red_reach_cm,
        blue_reach_cm=blue_reach_cm,
        red_age=red_age,
        blue_age=blue_age,
    )

    # The method-model row shares the winner diffs and adds the swap-invariant
    # pair features; built here because this is the only place that has the two
    # histories plus the real matchup context (rounds + weight class) together.
    method_feature_row = build_method_feature_row(
        feature_row,
        red_history,
        blue_history,
        scheduled_rounds=scheduled_rounds,
        weight_class=weight_class,
        is_title_fight=is_title_fight,
    )

    context = {
        "anchor": matchup.anchor,
        "anchorFightId": matchup.anchor_fight_id,
        "matchupDate": matchup_date.isoformat(),
        "weightClass": weight_class,
        "scheduledRounds": scheduled_rounds,
        "redHistory": asdict(red_history) if red_history is not None else None,
        "blueHistory": asdict(blue_history) if blue_history is not None else None,
        "lowConfidence": low_confidence,
    }
    return feature_row, method_feature_row, context, low_confidence


def _raw_contributions(model: Any, transformed_row: np.ndarray) -> np.ndarray | None:
    """TreeSHAP contributions of one transformed row (bias column dropped)."""
    get_booster = getattr(model, "get_booster", None)
    if get_booster is None:
        return None
    import xgboost as xgb

    booster = get_booster()
    # Match the DMatrix naming to the booster's OWN feature_names: the production
    # model is trained on a numpy array (feature_names is None), so forcing names
    # here would raise a feature-name mismatch; a DataFrame-trained booster carries
    # names and must get them. Either way pred_contribs returns contributions in
    # column order (= the bundle's feature_columns), so we map back by index below.
    dmatrix = xgb.DMatrix(transformed_row, feature_names=booster.feature_names)
    # Shape (1, n_features + 1); the trailing column is the bias term, dropped here.
    return booster.predict(dmatrix, pred_contribs=True)[0][:-1]


def _attribution_factors(feature_columns: list[str]) -> list[tuple[str, list[int]]]:
    """The factors shown to the user, as (name, indices into feature_columns).

    A ``*_diff`` (or any other single column) is its own factor, named after the
    column. A per-corner pair ``{base}_red`` / ``{base}_blue`` is ONE factor named
    after its base, at the position of its first column: split in two, each half
    compares both fighters in the same slot, the concept shows up twice and each
    half ranks lower than the whole. Half a pair (never produced by training,
    which keeps or drops pairs whole) stays a factor of its own."""
    index_of = {column: index for index, column in enumerate(feature_columns)}
    factors: list[tuple[str, list[int]]] = []
    for index, column in enumerate(feature_columns):
        base = PAIR_BASE_BY_COLUMN.get(column)
        if base is None:
            factors.append((column, [index]))
            continue
        red, blue = CORNER_PAIRS[base]
        if red not in index_of or blue not in index_of:
            factors.append((column, [index]))
        elif index == min(index_of[red], index_of[blue]):
            factors.append((base, [index_of[red], index_of[blue]]))
    return factors


def _raw_pair_difference(
    raw_row: dict[str, Any] | None, red_column: str, blue_column: str
) -> float | None:
    """Raw red-minus-blue value of a pair, from the row BEFORE imputation; None
    when either side is unknown. The imputed median of someone without a record
    is a made-up number and must never be reported as the difference."""
    if raw_row is None:
        return None
    red, blue = raw_row.get(red_column), raw_row.get(blue_column)
    if red is None or blue is None or pd.isna(red) or pd.isna(blue):
        return None
    value = float(red) - float(blue)
    return value if np.isfinite(value) else None


def _compute_top_features(
    model: Any,
    feature_columns: list[str],
    transformed_row: np.ndarray,
    swapped_transformed_row: np.ndarray,
    raw_row: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float] | None]:
    """SYMMETRIZED per-prediction feature attributions from the XGBoost booster.

    ``booster.predict(..., pred_contribs=True)`` is TreeSHAP: each feature's
    additive contribution to the raw margin (the log-odds that *red* wins). The
    served probability is the corner-symmetrized average, so a single forward
    pass would not mirror under a corner swap; averaging the forward
    contributions with the NEGATED swapped-row contributions does —
    attribution(A, B) == -attribution(B, A) feature by feature, matching the
    probability identity. The bias term cancels in that average, so the
    symmetrized contributions sum EXACTLY to the symmetrized margin: the UI can
    close the balance with a "rest of factors" bar.

    Per-corner pairs are merged into one factor per base (see
    ``_attribution_factors``): its contribution is the symmetrized contribution
    of the red column plus that of the blue one, which stays antisymmetric under
    a corner swap and only regroups terms, so the balance still closes. Its value
    is the raw red-minus-blue difference from ``raw_row`` (the row before
    imputation), or None when a side is unknown or no raw row is given. The full
    map uses the same factor names as the ranking, so a pair is never counted
    twice by the UI.

    Returns the ranked top five (signed contribution, direction, and for a
    ``*_diff`` the (imputed) forward value the model actually saw) plus the FULL
    name -> contribution map (None when the estimator has no booster).

    No non-finite number leaves this function. A value the booster saw as NaN
    (XGBoost routes a missing value natively) or as infinite has no JSON form:
    it is reported as None and the factor stays, because its contribution is
    real. A non-finite CONTRIBUTION is not a measurement: it cannot be drawn as
    a bar, a NaN breaks the |contribution| sort for every other factor, and the
    frontend's schema would reject the whole prediction over it. That factor
    leaves the ranking and the map, with a warning.

    Informative only: this explains the frozen base model's margin and does not
    change the returned probability, which comes from the monotonic calibrator.
    Because the calibrator is monotonic in that margin, the sign/direction stays
    valid for the calibrated probability too."""
    forward = _raw_contributions(model, transformed_row)
    if forward is None:
        return [], None
    swapped = _raw_contributions(model, swapped_transformed_row)
    symmetrized = (forward - swapped) / 2.0
    contributions_map: dict[str, float] = {}
    ranked: list[dict[str, Any]] = []
    dropped: list[str] = []
    for feature_name, indices in _attribution_factors(feature_columns):
        if len(indices) == 1:
            index = indices[0]
            contribution = float(symmetrized[index])
            value: float | None = float(transformed_row[0][index])
        else:
            red_index, blue_index = indices
            contribution = float(symmetrized[red_index]) + float(
                symmetrized[blue_index]
            )
            value = _raw_pair_difference(
                raw_row, feature_columns[red_index], feature_columns[blue_index]
            )
        if not np.isfinite(contribution):
            dropped.append(feature_name)
            continue
        contributions_map[feature_name] = contribution
        ranked.append(
            {
                "name": feature_name,
                "value": value if value is not None and np.isfinite(value) else None,
                "contribution": contribution,
                "direction": "red" if contribution >= 0 else "blue",
            }
        )
    if dropped:
        LOGGER.warning(
            "Non-finite SHAP contribution, left out of the ranking: %s", dropped
        )
    ranked.sort(key=lambda item: abs(item["contribution"]), reverse=True)
    return ranked[:5], contributions_map


# The strict, shared corner swap lives in corners.py (diffs negate, per-corner
# pairs exchange, the method model's symmetric columns pass, anything else
# raises). Kept under its old name: evaluate.py and train_method.py import it
# from here, so they symmetrize exactly like serving without touching an import.
_swap_corners = swap_corners


def _red_win_probability(
    feature_row: dict[str, float | int | None],
    imputer: Any,
    model: Any,
    feature_columns: list[str],
) -> tuple[float, np.ndarray]:
    """Return P(red wins) for a raw feature row plus the transformed matrix.

    The frame is built from the BUNDLE's feature_columns, not from the constant
    FEATURE_COLUMNS: a bundle with columns beyond the 27-jun schema (the phase-4
    pairs) would otherwise be a KeyError, a 500 for every prediction. A column the
    row does not carry yet comes in as None, for the bundle's imputer (or the
    booster's native NaN routing) to handle."""
    feature_frame = pd.DataFrame([{column: feature_row.get(column) for column in feature_columns}])
    transformed = imputer.transform(feature_frame[feature_columns])
    probabilities = model.predict_proba(transformed)[0]
    return float(probabilities[1]), transformed


def _predict_method(
    bundle: dict[str, Any], method_feature_row: dict[str, float | int | None]
) -> dict[str, Any] | None:
    """Corner-symmetrized method probabilities, or None for a pre-method bundle.

    The method classes do not change when the corners are swapped, so the exact
    symmetrization is the plain per-class average of the forward and swapped
    predictions (the ``*_diff`` features negate under ``swap_corners`` and every
    added method feature is swap-invariant): probabilities(A, B) ==
    probabilities(B, A). Probabilities come from the calibrated estimator when
    the bundle carries one, like the winner path. Returning None (instead of
    raising) keeps /predict fully backward-compatible with bundles that predate
    the method model — the frontend treats the field as optional. The same None
    stands in for probabilities that come out non-finite."""
    method_model = bundle.get("method_model")
    method_imputer = bundle.get("method_imputer")
    if method_model is None or method_imputer is None:
        return None
    feature_columns = list(bundle.get("method_feature_columns") or METHOD_FEATURE_COLUMNS)
    classes = list(bundle.get("method_classes") or METHOD_CLASSES)
    estimator = bundle.get("method_calibrator") or method_model

    # Ensemble with a heavily regularized multinomial logistic regression. The
    # signal in these features is faint and mostly LINEAR, so a shrunken linear
    # model estimates probabilities better than the trees do; averaging the two
    # roughly halves the log-loss gap. Averaging is linear, so it commutes with
    # the forward/swapped average below and corner symmetry stays EXACT.
    # The linear half is optional: without it this behaves exactly like a
    # pre-ensemble bundle, which keeps a model rollback trivial.
    linear_estimator = bundle.get("method_calibrator_linear") or bundle.get("method_model_linear")
    estimators = [estimator]
    weights = [1.0]
    if linear_estimator is not None:
        # Both halves train on the same integer target so classes_ matches; if it
        # ever did not, averaging column-wise would blend different classes.
        main_order = getattr(estimator, "classes_", None)
        linear_order = getattr(linear_estimator, "classes_", None)
        if main_order is None or linear_order is None or list(main_order) == list(linear_order):
            configured = bundle.get("method_ensemble_weights") or [0.5, 0.5]
            estimators = [estimator, linear_estimator]
            weights = [float(weight) for weight in configured]

    def class_probabilities(row: dict[str, float | int | None]) -> np.ndarray:
        frame = pd.DataFrame([{column: row.get(column) for column in feature_columns}])
        transformed = method_imputer.transform(frame[feature_columns])
        return sum(
            weight * member.predict_proba(transformed)[0]
            for weight, member in zip(weights, estimators)
        )

    forward = class_probabilities(method_feature_row)
    swapped = class_probabilities(swap_corners(method_feature_row))
    symmetrized = (forward + swapped) / 2.0
    # predict_proba columns follow estimator.classes_ (the integer targets);
    # map each back to its class name instead of assuming they are sorted.
    class_order = getattr(estimator, "classes_", None)
    if class_order is None:
        class_order = list(range(len(classes)))
    probabilities = {
        classes[int(class_index)]: float(symmetrized[position])
        for position, class_index in enumerate(class_order)
    }
    # Non-finite probabilities mean a broken model or calibrator (a NaN input is
    # filled by the method imputer before it gets here). Shipping them would make
    # `predicted` meaningless (max() over NaN) and the web would discard the
    # block anyway. The method is secondary: drop the whole block, exactly as for
    # a pre-method bundle, and say so in the log.
    if not all(np.isfinite(value) for value in probabilities.values()):
        LOGGER.warning(
            "Non-finite method probabilities, methodPrediction dropped: %s",
            probabilities,
        )
        return None
    predicted = max(probabilities, key=probabilities.get)
    return {
        "probabilities": probabilities,
        "predicted": predicted,
        "trainedAt": bundle.get("method_trained_at"),
    }


def predict(
    red_fighter_id: int,
    blue_fighter_id: int,
    *,
    bundle: dict[str, Any] | None = None,
    fights_df: pd.DataFrame | None = None,
    rankings_df: pd.DataFrame | None = None,
    history_df: pd.DataFrame | None = None,
    fight_id: int | None = None,
) -> dict[str, Any]:
    """Win (and method) prediction for red vs blue.

    ``fight_id`` names the bout being predicted (the fight page sends its own):
    when it is these two fighters' bout, the prediction is anchored to it even
    if it is already decided; otherwise it is ignored and the pair rule applies
    (see ``_get_latest_matchup_context``)."""
    settings = get_settings()
    if bundle is None:
        bundle = _load_model_bundle()
    if fights_df is None:
        fights_df = load_base_dataframe(settings.database_url)
    if rankings_df is None:
        rankings_df = load_rankings_dataframe(settings.database_url)
    physical = _load_fighter_physical(settings.database_url, [red_fighter_id, blue_fighter_id])
    feature_row, method_feature_row, context, low_confidence = _build_feature_row(
        fights_df,
        rankings_df,
        red_fighter_id,
        blue_fighter_id,
        physical,
        history_df=history_df,
        fight_id=fight_id,
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
    # where every diff is negated and every per-corner pair exchanged, see
    # corners.py). With red_sym = (p_forward + (1 - p_swapped)) /
    # 2 the prediction satisfies redProbability(A, B) == blueProbability(B, A)
    # exactly, so predict(A, B) and predict(B, A) sum to 1. Both terms pass
    # through the same estimator, so calibration keeps this identity.
    forward_red_prob, transformed = _red_win_probability(feature_row, imputer, proba_estimator, feature_columns)
    swapped_red_prob, swapped_transformed = _red_win_probability(
        swap_corners(feature_row), imputer, proba_estimator, feature_columns
    )
    red_probability = (forward_red_prob + (1.0 - swapped_red_prob)) / 2.0
    blue_probability = 1.0 - red_probability
    # Pairs are reported as ONE factor whose value is the raw red-minus-blue
    # difference, read from the row before imputation.
    top_features, feature_contributions = _compute_top_features(
        model, feature_columns, transformed, swapped_transformed, raw_row=feature_row
    )
    method_prediction = _predict_method(bundle, method_feature_row)
    profiles = _load_fighter_profiles(settings.database_url, [red_fighter_id, blue_fighter_id])

    return {
        "redProbability": red_probability,
        "blueProbability": blue_probability,
        "topFeatures": top_features,
        "featureContributions": feature_contributions,
        "featureValues": feature_row,
        "methodPrediction": method_prediction,
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
    parser.add_argument(
        "--fight-id",
        type=int,
        default=None,
        help="Anchor to this bout of the two fighters, even if already decided.",
    )
    args = parser.parse_args()

    result = predict(args.red, args.blue, fight_id=args.fight_id)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()