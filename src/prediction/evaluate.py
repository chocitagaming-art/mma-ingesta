"""Diagnostic evaluation of the trained UFC fight-winner model.

This script does NOT retrain. It loads the persisted model bundle
(``src/prediction/model.joblib``) and reconstructs the EXACT same chronological
test slice that ``train.py`` builds (same dataset, same three-way split, same
feature columns and the same fitted imputer that was saved alongside the model).

It scores that test slice the way PRODUCTION does: with the calibrator when the
bundle carries one (``bundle.get('calibrator') or bundle['model']``) and with the
corner symmetrization from ``api.predict`` (``p_sym = (p(row) + (1 -
p(swap_corners(row)))) / 2``). It reports the four variants {raw, symmetrized} x
{uncalibrated, calibrated} and marks ``symmetrized + calibrated`` as the
production-equivalent headline.

On that test slice it also reports:
  * Brier score (``sklearn.metrics.brier_score_loss``)
  * Log loss (``sklearn.metrics.log_loss``)
  * A 10-bin calibration curve (``sklearn.calibration.calibration_curve``):
    mean predicted probability vs. observed positive fraction per bin.
  * Segment breakdowns (accuracy + Brier) by weight class / division,
    by scheduled rounds (3 vs 5) and by era (year ranges).
  * A breakdown by UFC experience (debutante / novato / veterano, the phase-3
    tiers) with n, accuracy, log loss, Brier, AUC and calibration (mean
    predicted vs. observed rate, plus a simple ECE) for EVERY variant. Phase 4
    brings debutant fights into the dataset and must leave the veterans no worse
    while making the debutants better: this is the instrument that says so.

Results are written idempotently into ``src/prediction/model_metrics.md`` under
the ``## Diagnostico (evaluate.py)`` section (the section is replaced, never
duplicated, so re-running the script does not accumulate sections). A summary is
also printed to stdout.

Run with: ``python -m src.prediction.evaluate``
Print only, leaving model_metrics.md untouched (e.g. to measure phase 4):
``python -m src.prediction.evaluate --no-write``
Another CSV or bundle (the phase-4 measurement scores scratch bundles):
``python -m src.prediction.evaluate --dataset PATH --bundle PATH --no-write``.
The columns come from the bundle: its feature_columns, and the CSV must carry
its feature set (legacy for a pre-phase-4 bundle).
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

from src.prediction.api import MIN_CONFIDENT_FIGHTS, _swap_corners
from src.prediction.bundle_io import discard_stale_calibrators, winner_training_config
from src.prediction.train import (
    DATASET_PATH,
    METRICS_PATH,
    MODEL_PATH,
    bundle_dataset_columns,
    chronological_three_way_split,
    feature_set_columns,
    get_available_feature_columns,
    load_dataset,
)

SECTION_HEADER = "## Diagnostico (evaluate.py)"
N_CALIBRATION_BINS = 10
DECISION_THRESHOLD = 0.5

# The four reported variants and their order. {raw, symmetrized} x {uncalibrated,
# calibrated}. 'symmetrized + calibrated' is the production-equivalent headline
# (mirrors api.predict, which symmetrizes corners and scores via the calibrator).
_VARIANT_LABELS = {
    "raw_uncalibrated": "raw, uncalibrated",
    "symmetrized_uncalibrated": "symmetrized, uncalibrated",
    "raw_calibrated": "raw, calibrated",
    "symmetrized_calibrated": "symmetrized + calibrated (PRODUCTION-EQUIVALENT)",
}
HEADLINE_VARIANT = "symmetrized_calibrated"

# UFC experience tiers, decided by the LEAST experienced corner (the min of both
# corners' prior UFC fights at the fight date). They are the phase-3 definitions
# (docs/experiments/preufc-dwcs-2026-09-30): its target subset, the "novatos", is
# min < MIN_CONFIDENT_FIGHTS (the api.py lowConfidence rule), and a debutant
# corner has 0 prior UFC fights. Phase 4 is judged per tier: veterans must not get
# worse, debutants must improve.
TIER_DEBUTANT = "debutante (min 0)"
TIER_ROOKIE = f"novato (min 1-{MIN_CONFIDENT_FIGHTS - 1})"
TIER_VETERAN = f"veterano (min >= {MIN_CONFIDENT_FIGHTS})"
TIER_UNKNOWN = "Unknown"
# Debutants + rookies = the phase-3 target subset, reported as its own row so the
# figures line up with the population that experiment measured.
ROOKIES_PHASE3 = f"novatos fase 3 (min < {MIN_CONFIDENT_FIGHTS})"
_EXPERIENCE_FIGHTS_COLUMNS = [
    "fight_id",
    "event_date",
    "fighter_red_id",
    "fighter_blue_id",
    "winner_id",
    "status",
]
_PRIOR_COUNT_COLUMNS = ["fight_id", "red_prior_fights", "blue_prior_fights"]


def era_bucket(year: int) -> str:
    """Map a calendar year to an era label (a range of years)."""
    if year <= 2004:
        return "1995-2004"
    if year <= 2009:
        return "2005-2009"
    if year <= 2014:
        return "2010-2014"
    if year <= 2019:
        return "2015-2019"
    if year <= 2024:
        return "2020-2024"
    return "2025+"


def load_model_bundle(path: Path | str | None = None) -> dict:
    """Load the persisted model bundle (model + imputer + feature_columns);
    default MODEL_PATH."""
    path = MODEL_PATH if path is None else Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Trained model not found at {path}. Run `python -m src.prediction.train` first."
        )
    bundle = joblib.load(path)
    for key in ("model", "imputer", "feature_columns"):
        if key not in bundle:
            raise RuntimeError(f"Model bundle is missing required key: {key!r}")
    # Same load as production (api._load_model_bundle): a calibrator that does not
    # wrap the model next to it is dropped, so the "calibrated" variants measure
    # what is actually served and not the old model the calibrator carries inside.
    discard_stale_calibrators(bundle)
    return bundle


def fetch_fight_metadata(fight_ids: list[int]) -> pd.DataFrame:
    """Fetch weight_class and scheduled_rounds from the DB for segmentation.

    Read-only. Returns an empty frame (so the caller degrades gracefully) when
    DATABASE_URL is absent or the query fails. This metadata is used ONLY to
    label segments; it is never fed to the model.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url or not fight_ids:
        return pd.DataFrame(columns=["fight_id", "weight_class", "db_scheduled_rounds"])
    try:
        from src.scrapers.db import connect, cursor

        query = """
            SELECT id AS fight_id, weight_class, scheduled_rounds AS db_scheduled_rounds
            FROM fights
            WHERE id = ANY(%s)
        """
        with connect(database_url) as connection:
            with cursor(connection) as db_cursor:
                db_cursor.execute(query, ([int(value) for value in fight_ids],))
                rows = db_cursor.fetchall()
        return pd.DataFrame(rows)
    except Exception as error:  # noqa: BLE001 - diagnostics must not hard-fail on DB issues
        print(f"[warn] Could not fetch fight metadata from DB ({error}); "
              "segment breakdowns will fall back to the CSV.")
        return pd.DataFrame(columns=["fight_id", "weight_class", "db_scheduled_rounds"])


def fetch_fights_for_experience() -> pd.DataFrame:
    """Fetch every UFC bout (date, corners, winner, status) to count experience.

    Read-only, and it needs the WHOLE fights table, not only the test slice: a
    test fight's experience is made of the bouts before it. Returns an empty frame
    (every test row then falls back to the Unknown tier) when DATABASE_URL is
    absent or the query fails. Used ONLY to label segments; never fed to the model.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return pd.DataFrame(columns=_EXPERIENCE_FIGHTS_COLUMNS)
    try:
        from src.scrapers.db import connect, cursor

        query = """
            SELECT fights.id AS fight_id, events.event_date,
                   fights.fighter_red_id, fights.fighter_blue_id,
                   fights.winner_id, fights.status
            FROM fights
            INNER JOIN events ON events.id = fights.event_id
            WHERE events.event_date IS NOT NULL
        """
        with connect(database_url) as connection:
            with cursor(connection) as db_cursor:
                db_cursor.execute(query)
                rows = db_cursor.fetchall()
        return pd.DataFrame(rows, columns=_EXPERIENCE_FIGHTS_COLUMNS)
    except Exception as error:  # noqa: BLE001 - diagnostics must not hard-fail on DB issues
        print(f"[warn] Could not fetch fights from DB ({error}); "
              "the experience breakdown will be Unknown.")
        return pd.DataFrame(columns=_EXPERIENCE_FIGHTS_COLUMNS)


def count_prior_ufc_fights(fights: pd.DataFrame) -> pd.DataFrame:
    """Prior UFC fights of each corner, one row per input bout (same order).

    A bout adds experience when it was actually decided: it has a winner and it
    is not cancelled. That is the population the phase-3 counter walked
    (FIGHTS_SQL in its build_bench_dataset.py), so the tiers match its figures.
    A draw or a no contest adds nothing, as there. (api.py's own count, from
    fighter_history.py, also takes draws, no contests and cancelled rows, so
    near the threshold a bout can be a rookie here and not lowConfidence there.)

    Only bouts dated STRICTLY before the fight count: never the fight itself, nor
    anything later (no look-ahead). Two bouts of one fighter on the SAME day (the
    1995-1999 tournaments: 52 cases, per the phase-3 README) do not count each
    other, because nothing says in which order they were fought: fight_id is an
    insertion order, not the bout order. It is the same strict date cut as the
    model's own history (fighter_history.py). The phase-3 walker went by
    (event_date, fight_id) instead; the two rules only differ on those tournament
    days, decades before any test slice.
    """
    if fights.empty:
        return pd.DataFrame(columns=_PRIOR_COUNT_COLUMNS)
    dates = pd.to_datetime(fights["event_date"]).to_numpy(dtype="datetime64[ns]")
    counted = (
        fights["winner_id"].notna().to_numpy()
        & ~fights["status"].isin(["cancelled"]).to_numpy()
        & ~np.isnat(dates)
    )
    appearances = pd.DataFrame(
        {
            "fighter_id": np.concatenate(
                [
                    fights["fighter_red_id"].to_numpy()[counted],
                    fights["fighter_blue_id"].to_numpy()[counted],
                ]
            ),
            "event_date": np.concatenate([dates[counted], dates[counted]]),
        }
    )
    history = {
        fighter_id: np.sort(group.to_numpy(dtype="datetime64[ns]"))
        for fighter_id, group in appearances.groupby("fighter_id")["event_date"]
    }

    def prior_fights(fighter_id, fight_date: np.datetime64) -> int | None:
        if np.isnat(fight_date):
            return None
        fighter_dates = history.get(fighter_id)
        if fighter_dates is None:
            return 0
        # side="left" counts the dates strictly lower than fight_date: the fight
        # itself and any other bout of that same day are not "before".
        return int(np.searchsorted(fighter_dates, fight_date, side="left"))

    return pd.DataFrame(
        {
            "fight_id": fights["fight_id"].to_numpy(),
            "red_prior_fights": pd.array(
                [prior_fights(f, d) for f, d in zip(fights["fighter_red_id"], dates)],
                dtype="Int64",
            ),
            "blue_prior_fights": pd.array(
                [prior_fights(f, d) for f, d in zip(fights["fighter_blue_id"], dates)],
                dtype="Int64",
            ),
        }
    )


def experience_tier(red_prior_fights, blue_prior_fights) -> str:
    """Tier of a bout by its LEAST experienced corner; Unknown without a count."""
    if pd.isna(red_prior_fights) or pd.isna(blue_prior_fights):
        return TIER_UNKNOWN
    fewest = min(int(red_prior_fights), int(blue_prior_fights))
    if fewest == 0:
        return TIER_DEBUTANT
    if fewest < MIN_CONFIDENT_FIGHTS:
        return TIER_ROOKIE
    return TIER_VETERAN


def attach_experience(test_df: pd.DataFrame, fights: pd.DataFrame) -> pd.DataFrame:
    """Add each corner's prior UFC fights and the experience tier to the test slice.

    Looked up by fight_id with ``map`` (never a merge), so the rows keep their
    order and stay aligned with the positional variant arrays. A test fight
    missing from ``fights``, or no DB access at all, gets the Unknown tier.
    """
    test_df = test_df.copy()
    counts = count_prior_ufc_fights(fights).drop_duplicates("fight_id")
    counts = counts.set_index("fight_id")
    for column in ("red_prior_fights", "blue_prior_fights"):
        test_df[column] = test_df["fight_id"].map(counts[column]).astype("Int64")
    test_df["experience_tier"] = [
        experience_tier(red, blue)
        for red, blue in zip(test_df["red_prior_fights"], test_df["blue_prior_fights"])
    ]
    return test_df


def _estimator_probabilities(
    estimator, imputer, feature_columns: list[str], test_df: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(raw, symmetrized)`` P(red wins) arrays for ``estimator``.

    ``symmetrized`` mirrors the production corner-symmetrization in api.predict:
    for each row ``p_sym = (p(row) + (1 - p(swap_corners(row)))) / 2``. The swap
    negates every ``*_diff`` (all features are diffs now), reusing
    ``api._swap_corners`` so this matches serving exactly. Both orientations pass
    through the same fitted imputer and estimator.
    """
    raw = estimator.predict_proba(imputer.transform(test_df[feature_columns]))[:, 1]
    swapped_records = [
        _swap_corners(record) for record in test_df[feature_columns].to_dict("records")
    ]
    swapped_frame = pd.DataFrame(swapped_records)[feature_columns]
    swapped = estimator.predict_proba(imputer.transform(swapped_frame))[:, 1]
    symmetrized = (raw + (1.0 - swapped)) / 2.0
    return raw, symmetrized


def build_test_predictions(
    dataset_path: Path | str | None = None, bundle_path: Path | str | None = None
) -> tuple[pd.DataFrame, dict[str, np.ndarray], str]:
    """Reconstruct the train.py test slice and score it four ways.

    ``dataset_path`` / ``bundle_path`` default to the training CSV and the served
    bundle. Returns the test dataframe (with the PRODUCTION-EQUIVALENT `prob`/`pred`
    plus segment columns), a dict of the four probability variants {raw,
    symmetrized} x {uncalibrated, calibrated}, and the key of the headline variant
    actually used.
    """
    bundle = load_model_bundle(bundle_path)
    feature_columns = list(bundle["feature_columns"])

    dataset = load_dataset(dataset_path, bundle_dataset_columns(bundle))
    train_df, _calibration_df, test_df = chronological_three_way_split(dataset)
    test_df = test_df.reset_index(drop=True)

    imputer = bundle["imputer"]
    model = bundle["model"]
    # Score with the calibrator when present (mirrors api.predict); the base model
    # drives the uncalibrated variants and the feature importances.
    calibrator = bundle.get("calibrator")

    # Sanity check: the feature columns saved with the model must match what
    # train.py would derive from the same train slice (guards against drift).
    expected_columns = get_available_feature_columns(
        train_df, feature_set_columns(winner_training_config(bundle)["feature_set"])
    )
    if feature_columns != expected_columns:
        print(
            "[warn] Saved feature_columns differ from train.py's "
            f"get_available_feature_columns(train_df).\n  saved:    {feature_columns}\n"
            f"  expected: {expected_columns}\nUsing the saved columns (model was trained on them)."
        )

    raw_uncal, sym_uncal = _estimator_probabilities(model, imputer, feature_columns, test_df)
    variants: dict[str, np.ndarray] = {
        "raw_uncalibrated": raw_uncal,
        "symmetrized_uncalibrated": sym_uncal,
    }
    if calibrator is not None:
        raw_cal, sym_cal = _estimator_probabilities(
            calibrator, imputer, feature_columns, test_df
        )
        variants["raw_calibrated"] = raw_cal
        variants["symmetrized_calibrated"] = sym_cal

    # Headline = symmetrized + calibrated when a calibrator exists, otherwise the
    # best available (symmetrized, uncalibrated). Drives prob/pred and breakdowns.
    headline_key = HEADLINE_VARIANT if HEADLINE_VARIANT in variants else "symmetrized_uncalibrated"
    headline = variants[headline_key]

    test_df = test_df.copy()
    test_df["prob"] = headline
    test_df["pred"] = (headline >= DECISION_THRESHOLD).astype(int)
    test_df["year"] = pd.to_datetime(test_df["event_date"]).dt.year
    test_df["era"] = test_df["year"].apply(era_bucket)

    # Enrich with true fight attributes for segmentation (division + rounds).
    # scheduled_rounds is no longer a model feature, so it comes only from the
    # fights table; without DB access the rounds breakdown collapses to Unknown.
    metadata = fetch_fight_metadata(test_df["fight_id"].tolist())
    if not metadata.empty:
        test_df = test_df.merge(metadata, on="fight_id", how="left")
        test_df["division"] = test_df["weight_class"].fillna("Unknown")
        test_df["rounds_segment"] = test_df["db_scheduled_rounds"]
    else:
        test_df["division"] = "Unknown"
        test_df["rounds_segment"] = pd.NA

    test_df["rounds_segment"] = (
        pd.to_numeric(test_df["rounds_segment"], errors="coerce")
        .round()
        .astype("Int64")
    )

    # UFC experience of each corner at the fight date (phase-3 tiers). It counts
    # every earlier bout, so it reads the whole fights table, not only the test
    # slice; without DB access every row falls back to Unknown.
    test_df = attach_experience(test_df, fetch_fights_for_experience())
    return test_df, variants, headline_key


def segment_breakdown(test_df: pd.DataFrame, column: str) -> list[dict]:
    """Accuracy + Brier per group of `column`, sorted by descending support."""
    rows: list[dict] = []
    grouped = test_df.groupby(column, dropna=False)
    for group_value, group in grouped:
        y_true = group["target"].to_numpy()
        prob = group["prob"].to_numpy()
        pred = group["pred"].to_numpy()
        label = "Unknown" if pd.isna(group_value) else str(group_value)
        rows.append(
            {
                "segment": label,
                "n": int(len(group)),
                "accuracy": float(accuracy_score(y_true, pred)),
                "brier": float(brier_score_loss(y_true, prob)),
                "positive_rate": float(np.mean(y_true)),
            }
        )
    rows.sort(key=lambda item: item["n"], reverse=True)
    return rows


def compute_calibration(test_df: pd.DataFrame) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """Return a per-bin calibration table plus the calibration_curve arrays.

    The table covers all 10 fixed uniform bins (with counts), while the raw
    arrays come from ``sklearn.calibration.calibration_curve`` (non-empty bins
    only), satisfying the requirement to use that helper.
    """
    y_true = test_df["target"].to_numpy()
    prob = test_df["prob"].to_numpy()

    prob_true, prob_pred = calibration_curve(
        y_true, prob, n_bins=N_CALIBRATION_BINS, strategy="uniform"
    )

    edges = np.linspace(0.0, 1.0, N_CALIBRATION_BINS + 1)
    bin_ids = _calibration_bin_ids(prob)
    table: list[dict] = []
    for bin_index in range(N_CALIBRATION_BINS):
        mask = bin_ids == bin_index
        count = int(mask.sum())
        table.append(
            {
                "bin": f"[{edges[bin_index]:.1f}, {edges[bin_index + 1]:.1f})",
                "count": count,
                "mean_predicted": float(prob[mask].mean()) if count else None,
                "observed_fraction": float(y_true[mask].mean()) if count else None,
            }
        )
    return table, prob_true, prob_pred


def _calibration_bin_ids(prob: np.ndarray) -> np.ndarray:
    """Uniform bin (0..N_CALIBRATION_BINS-1) of each probability; 1.0 -> last bin."""
    edges = np.linspace(0.0, 1.0, N_CALIBRATION_BINS + 1)
    return np.clip(np.digitize(prob, edges[1:-1]), 0, N_CALIBRATION_BINS - 1)


def expected_calibration_error(y_true: np.ndarray, prob: np.ndarray) -> float | None:
    """Simple ECE: |mean predicted - observed rate| per uniform bin, weighted by
    the bin's share of rows. Same bins as the calibration curve; None when empty."""
    if len(y_true) == 0:
        return None
    bin_ids = _calibration_bin_ids(prob)
    weighted_gap = 0.0
    for bin_index in np.unique(bin_ids):
        mask = bin_ids == bin_index
        weighted_gap += mask.sum() * abs(prob[mask].mean() - y_true[mask].mean())
    return float(weighted_gap / len(y_true))


def segment_metrics(
    y_true: np.ndarray, prob: np.ndarray
) -> dict[str, float | int | None]:
    """n, accuracy, log loss, Brier, AUC and calibration of one segment.

    Calibration = mean predicted probability vs. observed positive rate, plus the
    ECE. An empty segment gives n=0 and None everywhere else; a single-class
    segment has no AUC (None), but the rest is still computed.
    """
    n = int(len(y_true))
    if n == 0:
        return {
            "n": 0,
            "accuracy": None,
            "log_loss": None,
            "brier": None,
            "auc": None,
            "mean_predicted": None,
            "observed_rate": None,
            "ece": None,
        }
    pred = (prob >= DECISION_THRESHOLD).astype(int)
    both_classes = len(np.unique(y_true)) == 2
    return {
        "n": n,
        "accuracy": float(accuracy_score(y_true, pred)),
        "log_loss": float(log_loss(y_true, prob, labels=[0, 1])),
        "brier": float(brier_score_loss(y_true, prob)),
        "auc": float(roc_auc_score(y_true, prob)) if both_classes else None,
        "mean_predicted": float(np.mean(prob)),
        "observed_rate": float(np.mean(y_true)),
        "ece": expected_calibration_error(y_true, prob),
    }


def experience_breakdown(
    test_df: pd.DataFrame, variants: dict[str, np.ndarray]
) -> list[dict]:
    """``segment_metrics`` per experience tier x variant, tiers in a FIXED order.

    Every tier is listed even when empty (today's dataset drops the bouts where a
    corner has no UFC history, so the debutant tier stays empty or nearly so until
    phase 4), plus the phase-3 rookies row (debutants + rookies). Unknown is
    listed only when some row has no count. Rows are matched to the variant
    arrays by POSITION, like the variant comparison, so ``test_df`` must keep the
    order it was scored in.
    """
    tiers = test_df["experience_tier"].to_numpy()
    y_true = test_df["target"].to_numpy()
    masks = {
        TIER_DEBUTANT: tiers == TIER_DEBUTANT,
        TIER_ROOKIE: tiers == TIER_ROOKIE,
        ROOKIES_PHASE3: (tiers == TIER_DEBUTANT) | (tiers == TIER_ROOKIE),
        TIER_VETERAN: tiers == TIER_VETERAN,
    }
    unknown = tiers == TIER_UNKNOWN
    if unknown.any():
        masks[TIER_UNKNOWN] = unknown
    rows: list[dict] = []
    for tier, mask in masks.items():
        for variant_key in _VARIANT_LABELS:
            if variant_key not in variants:
                continue
            prob = variants[variant_key]
            rows.append(
                {
                    "tier": tier,
                    "variant": variant_key,
                    **segment_metrics(y_true[mask], prob[mask]),
                }
            )
    return rows


def _format_metric(value: float | None, missing: str = "-") -> str:
    return missing if value is None else f"{value:.4f}"


def _variant_metrics(y_true: np.ndarray, prob: np.ndarray) -> dict[str, float]:
    pred = (prob >= DECISION_THRESHOLD).astype(int)
    return {
        "brier": float(brier_score_loss(y_true, prob)),
        "log_loss": float(log_loss(y_true, prob, labels=[0, 1])),
        "accuracy": float(accuracy_score(y_true, pred)),
    }


def build_section(
    test_df: pd.DataFrame, variants: dict[str, np.ndarray], headline_key: str
) -> str:
    """Render the markdown diagnostic section."""
    y_true = test_df["target"].to_numpy()
    prob = test_df["prob"].to_numpy()
    pred = test_df["pred"].to_numpy()

    brier = brier_score_loss(y_true, prob)
    logloss = log_loss(y_true, prob, labels=[0, 1])
    accuracy = accuracy_score(y_true, pred)

    test_dates = pd.to_datetime(test_df["event_date"])
    date_min = test_dates.min().date()
    date_max = test_dates.max().date()

    calibration_table, prob_true, prob_pred = compute_calibration(test_df)
    division_rows = segment_breakdown(test_df, "division")
    rounds_rows = segment_breakdown(test_df, "rounds_segment")
    era_rows = segment_breakdown(test_df, "era")

    headline_label = _VARIANT_LABELS[headline_key]
    lines: list[str] = [
        SECTION_HEADER,
        "",
        "Evaluacion diagnostica del modelo persistido (sin reentrenar). "
        "Reconstruye el mismo test slice cronologico de `train.py` y lo puntua "
        "con `model.joblib` (modelo + imputer + calibrator + feature_columns "
        "guardados), aplicando la simetrizacion de esquinas de produccion.",
        "",
        f"- Test rows: {len(test_df)}",
        f"- Test date range: {date_min} to {date_max}",
        f"- Decision threshold: {DECISION_THRESHOLD}",
        "",
        "### HEADLINE (production-equivalent: " + headline_label + ")",
        f"- Brier score: {brier:.4f}  (lower is better; 0.25 = uninformed 0.5)",
        f"- Log loss: {logloss:.4f}  (lower is better)",
        f"- Accuracy: {accuracy:.4f}",
        "",
        "### Variant comparison {raw, symmetrized} x {uncalibrated, calibrated}",
        "",
        "`symmetrized + calibrated` matches what api.predict serves and is the "
        "headline above; the others are diagnostic references.",
        "",
        "| Variant | Brier | Log loss | Accuracy |",
        "| --- | ---: | ---: | ---: |",
    ]
    for variant_key, variant_label in _VARIANT_LABELS.items():
        if variant_key not in variants:
            continue
        metrics = _variant_metrics(y_true, variants[variant_key])
        marker = " **<-** " if variant_key == headline_key else ""
        lines.append(
            f"| {variant_label}{marker} | {metrics['brier']:.4f} | "
            f"{metrics['log_loss']:.4f} | {metrics['accuracy']:.4f} |"
        )
    if HEADLINE_VARIANT not in variants:
        lines.extend(
            [
                "",
                "Note: the saved bundle has no `calibrator`, so only the "
                "uncalibrated variants are shown. Run `python -m "
                "src.prediction.calibrate` to add one.",
            ]
        )

    lines.extend(
        [
            "",
            "### Calibration curve (10 uniform bins)",
            "",
            "Mean predicted probability vs. observed positive fraction per bin "
            "(headline variant).",
            "",
            "| Bin | Count | Mean predicted | Observed fraction |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for row in calibration_table:
        mean_predicted = "-" if row["mean_predicted"] is None else f"{row['mean_predicted']:.4f}"
        observed = "-" if row["observed_fraction"] is None else f"{row['observed_fraction']:.4f}"
        lines.append(f"| {row['bin']} | {row['count']} | {mean_predicted} | {observed} |")

    paired = ", ".join(
        f"({pred_value:.3f} -> {true_value:.3f})"
        for pred_value, true_value in zip(prob_pred, prob_true, strict=True)
    )
    lines.extend(
        [
            "",
            "calibration_curve (non-empty bins, predicted -> observed): "
            + (paired if paired else "n/a"),
            "",
            "### Breakdown by division (weight class)",
            "",
            "| Division | N | Accuracy | Brier | Positive rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in division_rows:
        lines.append(
            f"| {row['segment']} | {row['n']} | {row['accuracy']:.4f} | "
            f"{row['brier']:.4f} | {row['positive_rate']:.4f} |"
        )

    lines.extend(
        [
            "",
            "### Breakdown by scheduled_rounds (3 vs 5)",
            "",
            "Scheduled rounds taken from the `fights` table. scheduled_rounds is "
            "NO LONGER a model feature (dropped as zero-importance); this is a "
            "segmentation label only.",
            "",
            "| Scheduled rounds | N | Accuracy | Brier | Positive rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rounds_rows:
        lines.append(
            f"| {row['segment']} | {row['n']} | {row['accuracy']:.4f} | "
            f"{row['brier']:.4f} | {row['positive_rate']:.4f} |"
        )

    lines.extend(
        [
            "",
            "### Breakdown by era (year ranges)",
            "",
            "| Era | N | Accuracy | Brier | Positive rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in era_rows:
        lines.append(
            f"| {row['segment']} | {row['n']} | {row['accuracy']:.4f} | "
            f"{row['brier']:.4f} | {row['positive_rate']:.4f} |"
        )

    experience_rows = experience_breakdown(test_df, variants)
    lines.extend(
        [
            "",
            "### Breakdown by UFC experience (phase-3 tiers)",
            "",
            "Prior UFC fights of each corner in `fights` (decided and not "
            "cancelled, like the phase-3 counter), dated STRICTLY before the bout: "
            "same-day bouts (1995-1999 tournaments) do not count each other. The "
            "LEAST experienced corner decides the tier. `novatos fase 3` = "
            "debutantes + novatos = the phase-3 target subset and the api.py "
            "lowConfidence rule. Every variant is shown: phase 4 must leave the "
            "veterans no worse and make the debutants better. ECE = |mean predicted "
            "- observed rate| per uniform bin (10 bins), weighted by rows; a "
            "single-class tier has no AUC (-).",
            "",
            "| Tier | Variant | N | Accuracy | Log loss | Brier | AUC "
            "| Mean predicted | Observed rate | ECE |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in experience_rows:
        marker = " **<-** " if row["variant"] == headline_key else ""
        lines.append(
            f"| {row['tier']} | {_VARIANT_LABELS[row['variant']]}{marker} | "
            f"{row['n']} | {_format_metric(row['accuracy'])} | "
            f"{_format_metric(row['log_loss'])} | {_format_metric(row['brier'])} | "
            f"{_format_metric(row['auc'])} | {_format_metric(row['mean_predicted'])} | "
            f"{_format_metric(row['observed_rate'])} | {_format_metric(row['ece'])} |"
        )
    if any(row["tier"] == TIER_UNKNOWN for row in experience_rows):
        lines.extend(
            [
                "",
                "Unknown = no prior-fight count: no DATABASE_URL, or the bout is "
                "missing from `fights`.",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def write_section_idempotent(section: str) -> None:
    """Insert or replace the diagnostic section in model_metrics.md."""
    if METRICS_PATH.exists():
        existing = METRICS_PATH.read_text(encoding="utf-8")
    else:
        existing = ""

    if SECTION_HEADER in existing:
        start = existing.index(SECTION_HEADER)
        before = existing[:start]
        rest = existing[start + len(SECTION_HEADER):]
        # The section runs until the next level-2 heading (## ...) or EOF.
        next_h2 = re.search(r"\n## ", rest)
        after = rest[next_h2.start():] if next_h2 else ""
        new_text = before.rstrip() + "\n\n" + section.rstrip() + "\n"
        if after:
            new_text += "\n" + after.lstrip("\n")
    else:
        base = existing.rstrip()
        new_text = (base + "\n\n" if base else "") + section.rstrip() + "\n"

    METRICS_PATH.write_text(new_text, encoding="utf-8")


def print_summary(
    test_df: pd.DataFrame, variants: dict[str, np.ndarray], headline_key: str
) -> None:
    y_true = test_df["target"].to_numpy()
    prob = test_df["prob"].to_numpy()
    pred = test_df["pred"].to_numpy()
    print("=== Diagnostic evaluation (no retraining) ===")
    print(f"Test rows: {len(test_df)}")
    print(f"Headline variant (production-equivalent): {_VARIANT_LABELS[headline_key]}")
    print(
        f"Brier: {brier_score_loss(y_true, prob):.4f} | "
        f"LogLoss: {log_loss(y_true, prob, labels=[0, 1]):.4f} | "
        f"Accuracy: {accuracy_score(y_true, pred):.4f}"
    )
    print("Variants {raw, symmetrized} x {uncalibrated, calibrated}:")
    for variant_key, variant_label in _VARIANT_LABELS.items():
        if variant_key not in variants:
            continue
        metrics = _variant_metrics(y_true, variants[variant_key])
        marker = "  <- headline" if variant_key == headline_key else ""
        print(
            f"  {variant_label:<48} brier={metrics['brier']:.4f} "
            f"logloss={metrics['log_loss']:.4f} acc={metrics['accuracy']:.4f}{marker}"
        )
    print()
    print("Division breakdown (accuracy / brier / n):")
    for row in segment_breakdown(test_df, "division"):
        print(f"  {row['segment']:<22} acc={row['accuracy']:.4f} brier={row['brier']:.4f} n={row['n']}")
    print("scheduled_rounds breakdown (accuracy / brier / n):")
    for row in segment_breakdown(test_df, "rounds_segment"):
        print(f"  {row['segment']:<6} acc={row['accuracy']:.4f} brier={row['brier']:.4f} n={row['n']}")
    print("era breakdown (accuracy / brier / n):")
    for row in segment_breakdown(test_df, "era"):
        print(f"  {row['segment']:<12} acc={row['accuracy']:.4f} brier={row['brier']:.4f} n={row['n']}")
    print()
    print("UFC experience breakdown (least experienced corner, phase-3 tiers):")
    experience_rows = experience_breakdown(test_df, variants)
    if any(row["tier"] == TIER_UNKNOWN for row in experience_rows):
        print("  (Unknown = no prior-fight count: no DATABASE_URL, or the bout is "
              "missing from fights)")
    current_tier = None
    for row in experience_rows:
        if row["tier"] != current_tier:
            current_tier = row["tier"]
            print(f"  {row['tier']:<26} n={row['n']}")
        if row["n"] == 0:
            continue
        marker = "  <- headline" if row["variant"] == headline_key else ""
        print(
            f"    {_VARIANT_LABELS[row['variant']]:<48} "
            f"acc={_format_metric(row['accuracy'], 'n/a')} "
            f"logloss={_format_metric(row['log_loss'], 'n/a')} "
            f"brier={_format_metric(row['brier'], 'n/a')} "
            f"auc={_format_metric(row['auc'], 'n/a')} "
            f"pred={_format_metric(row['mean_predicted'], 'n/a')} "
            f"obs={_format_metric(row['observed_rate'], 'n/a')} "
            f"ece={_format_metric(row['ece'], 'n/a')}{marker}"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Defaults are read when this runs (a test may point MODEL_PATH elsewhere).
    parser = argparse.ArgumentParser(
        description="Evaluacion diagnostica del modelo persistido (sin reentrenar)."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET_PATH,
        help="CSV de entrenamiento (por defecto: %(default)s).",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=MODEL_PATH,
        help="Bundle que se evalua (por defecto: %(default)s).",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help=(
            "Solo imprime el resumen: NO escribe la seccion en "
            "src/prediction/model_metrics.md. Para medir (por ejemplo la fase 4) "
            "sin pisar la ficha local del modelo."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    test_df, variants, headline_key = build_test_predictions(
        dataset_path=args.dataset, bundle_path=args.bundle
    )
    if not args.no_write:
        section = build_section(test_df, variants, headline_key)
        write_section_idempotent(section)
    print_summary(test_df, variants, headline_key)
    print()
    if args.no_write:
        print(f"--no-write: {METRICS_PATH} was NOT written.")
    else:
        print(f"Wrote diagnostic section to {METRICS_PATH}")


if __name__ == "__main__":
    main()
