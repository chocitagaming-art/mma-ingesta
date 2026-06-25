"""Diagnostic evaluation of the trained UFC fight-winner model.

This script does NOT retrain. It loads the persisted model bundle
(``src/prediction/model.joblib``) and reconstructs the EXACT same chronological
test slice that ``train.py`` builds (same dataset, same split, same feature
columns and the same fitted imputer that was saved alongside the model).

On that test slice it reports:
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

from src.prediction.train import (
    METRICS_PATH,
    MODEL_PATH,
    chronological_train_test_split,
    get_available_feature_columns,
    load_dataset,
)

SECTION_HEADER = "## Diagnostico (evaluate.py)"
N_CALIBRATION_BINS = 10
DECISION_THRESHOLD = 0.5


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


def build_test_predictions() -> tuple[pd.DataFrame, dict]:
    """Reconstruct the train.py test slice and score it with the saved model.

    Returns the test dataframe enriched with `prob`/`pred`/segment columns and
    the loaded model bundle.
    """
    dataset = load_dataset()
    train_df, test_df = chronological_train_test_split(dataset)
    test_df = test_df.reset_index(drop=True)

    bundle = load_model_bundle()
    feature_columns = list(bundle["feature_columns"])
    imputer = bundle["imputer"]
    model = bundle["model"]

    # Sanity check: the feature columns saved with the model must match what
    # train.py would derive from the same train slice (guards against drift).
    expected_columns = get_available_feature_columns(train_df)
    if feature_columns != expected_columns:
        print(
            "[warn] Saved feature_columns differ from train.py's "
            f"get_available_feature_columns(train_df).\n  saved:    {feature_columns}\n"
            f"  expected: {expected_columns}\nUsing the saved columns (model was trained on them)."
        )

    x_test = imputer.transform(test_df[feature_columns])
    probabilities = model.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= DECISION_THRESHOLD).astype(int)

    test_df = test_df.copy()
    test_df["prob"] = probabilities
    test_df["pred"] = predictions
    test_df["year"] = pd.to_datetime(test_df["event_date"]).dt.year
    test_df["era"] = test_df["year"].apply(era_bucket)

    # Enrich with true fight attributes for segmentation (division + rounds).
    metadata = fetch_fight_metadata(test_df["fight_id"].tolist())
    if not metadata.empty:
        test_df = test_df.merge(metadata, on="fight_id", how="left")
        test_df["division"] = test_df["weight_class"].fillna("Unknown")
        # The CSV `scheduled_rounds` feature is degenerate (all 3); use the true
        # value from the fights table so the 3-vs-5 split is meaningful.
        test_df["rounds_segment"] = test_df["db_scheduled_rounds"].fillna(
            test_df["scheduled_rounds"]
        )
    else:
        test_df["division"] = "Unknown"
        test_df["rounds_segment"] = test_df["scheduled_rounds"]

    test_df["rounds_segment"] = (
        pd.to_numeric(test_df["rounds_segment"], errors="coerce")
        .round()
        .astype("Int64")
    )
    return test_df, bundle


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


def build_section(test_df: pd.DataFrame) -> str:
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

    lines: list[str] = [
        SECTION_HEADER,
        "",
        "Evaluacion diagnostica del modelo persistido (sin reentrenar). "
        "Reconstruye el mismo test slice cronologico de `train.py` y lo puntua "
        "con `model.joblib` (modelo + imputer + feature_columns guardados).",
        "",
        f"- Test rows: {len(test_df)}",
        f"- Test date range: {date_min} to {date_max}",
        f"- Decision threshold: {DECISION_THRESHOLD}",
        "",
        "### Probabilistic metrics (test)",
        f"- Brier score: {brier:.4f}  (lower is better; 0.25 = uninformed 0.5)",
        f"- Log loss: {logloss:.4f}  (lower is better)",
        f"- Accuracy: {accuracy:.4f}",
        "",
        "### Calibration curve (10 uniform bins)",
        "",
        "Mean predicted probability vs. observed positive fraction per bin.",
        "",
        "| Bin | Count | Mean predicted | Observed fraction |",
        "| --- | ---: | ---: | ---: |",
    ]
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
            "Scheduled rounds taken from the `fights` table (the CSV feature is "
            "degenerate, all 3).",
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


def print_summary(test_df: pd.DataFrame, section: str) -> None:
    y_true = test_df["target"].to_numpy()
    prob = test_df["prob"].to_numpy()
    pred = test_df["pred"].to_numpy()
    print("=== Diagnostic evaluation (no retraining) ===")
    print(f"Test rows: {len(test_df)}")
    print(
        f"Brier: {brier_score_loss(y_true, prob):.4f} | "
        f"LogLoss: {log_loss(y_true, prob, labels=[0, 1]):.4f} | "
        f"Accuracy: {accuracy_score(y_true, pred):.4f}"
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
    test_df, _bundle = build_test_predictions()
    section = build_section(test_df)
    write_section_idempotent(section)
    print_summary(test_df, section)


if __name__ == "__main__":
    main()
