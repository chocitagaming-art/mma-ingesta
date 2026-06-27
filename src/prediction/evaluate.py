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

Results are written idempotently into ``src/prediction/model_metrics.md`` under
the ``## Diagnostico (evaluate.py)`` section (the section is replaced, never
duplicated, so re-running the script does not accumulate sections). A summary is
also printed to stdout.

Run with: ``python -m src.prediction.evaluate``
"""

from __future__ import annotations

import os
import re

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss

from src.prediction.api import _swap_corners
from src.prediction.train import (
    METRICS_PATH,
    MODEL_PATH,
    chronological_three_way_split,
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


def load_model_bundle() -> dict:
    """Load the persisted model bundle (model + imputer + feature_columns)."""
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Trained model not found at {MODEL_PATH}. Run `python -m src.prediction.train` first."
        )
    bundle = joblib.load(MODEL_PATH)
    for key in ("model", "imputer", "feature_columns"):
        if key not in bundle:
            raise RuntimeError(f"Model bundle is missing required key: {key!r}")
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


def build_test_predictions() -> tuple[pd.DataFrame, dict[str, np.ndarray], str]:
    """Reconstruct the train.py test slice and score it four ways.

    Returns the test dataframe (with the PRODUCTION-EQUIVALENT `prob`/`pred` plus
    segment columns), a dict of the four probability variants {raw, symmetrized} x
    {uncalibrated, calibrated}, and the key of the headline variant actually used.
    """
    dataset = load_dataset()
    train_df, _calibration_df, test_df = chronological_three_way_split(dataset)
    test_df = test_df.reset_index(drop=True)

    bundle = load_model_bundle()
    feature_columns = list(bundle["feature_columns"])
    imputer = bundle["imputer"]
    model = bundle["model"]
    # Score with the calibrator when present (mirrors api.predict); the base model
    # drives the uncalibrated variants and the feature importances.
    calibrator = bundle.get("calibrator")

    # Sanity check: the feature columns saved with the model must match what
    # train.py would derive from the same train slice (guards against drift).
    expected_columns = get_available_feature_columns(train_df)
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
    bin_ids = np.clip(np.digitize(prob, edges[1:-1]), 0, N_CALIBRATION_BINS - 1)
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
    print(f"Wrote diagnostic section to {METRICS_PATH}")


def main() -> None:
    test_df, variants, headline_key = build_test_predictions()
    section = build_section(test_df, variants, headline_key)
    write_section_idempotent(section)
    print_summary(test_df, variants, headline_key)


if __name__ == "__main__":
    main()
