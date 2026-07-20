"""Train + calibrate + evaluate the win-METHOD model (3 classes).

Run with ``python -m src.prediction.train_method``. Reads
``method_training_dataset.csv`` (built by ``python -m
src.prediction.features.method_output``), reuses the winner pipeline's
chronological machinery (same three-way split boundaries, same walk-forward
folds) and ADDS the resulting artifacts to the existing ``model.joblib`` bundle
under ``method_*`` keys — the winner model's keys are never touched, so an old
service binary keeps working against the new bundle and vice versa.

Pipeline (all chronological, no shuffle):
1. Hyperparameter search: the winner grid, scored by mean walk-forward
   multiclass log-loss over 3 folds (folds whose train slice lacks a class are
   skipped — XGBoost must see all 3 classes to emit 3-class probabilities).
2. Base model: XGBClassifier(multi:softprob) fit on train (~64%).
3. Calibration: isotonic vs sigmoid CalibratedClassifierCV over the FROZEN base
   model, selected by 5-fold out-of-fold log-loss on the calibration holdout
   (~16%, rows the base never saw), then refit on the full holdout.
4. Evaluation on the untouched last-20% test slice, scored the way PRODUCTION
   will serve: corner-symmetrized (average of forward and swapped predictions —
   every added method feature is swap-invariant and the diffs negate, so this
   is exact) + calibrated. Honest baselines: majority class and train priors.

The metrics section in ``model_metrics.md`` is replaced idempotently.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    log_loss,
    roc_auc_score,
)
from sklearn.model_selection import ParameterGrid, StratifiedKFold, cross_val_predict
from xgboost import XGBClassifier

from src.prediction.api import _swap_corners
from src.prediction.features.method_features import METHOD_CLASSES, METHOD_FEATURE_COLUMNS
from src.prediction.train import (
    METRICS_PATH,
    MODEL_PATH,
    build_time_series_folds,
    chronological_three_way_split,
)

METHOD_DATASET_PATH = Path("method_training_dataset.csv")
SECTION_HEADER = "## Modelo de metodo (train_method.py)"
N_CALIBRATION_FOLDS = 5
CANDIDATE_CALIBRATIONS = ("isotonic", "sigmoid")
CLASS_LABELS = list(range(len(METHOD_CLASSES)))

_VARIANT_LABELS = {
    "raw_uncalibrated": "raw, uncalibrated",
    "symmetrized_uncalibrated": "symmetrized, uncalibrated",
    "raw_calibrated": "raw, calibrated",
    "symmetrized_calibrated": "symmetrized + calibrated (PRODUCTION-EQUIVALENT)",
}
HEADLINE_VARIANT = "symmetrized_calibrated"


def load_method_dataset() -> pd.DataFrame:
    dataset = pd.read_csv(METHOD_DATASET_PATH, parse_dates=["event_date"])
    dataset = dataset.sort_values(["event_date", "fight_id", "target"]).reset_index(drop=True)
    missing = [c for c in METHOD_FEATURE_COLUMNS + ["target"] if c not in dataset.columns]
    if missing:
        raise RuntimeError(f"Method dataset missing required columns: {missing}")
    return dataset


def get_available_method_columns(dataset: pd.DataFrame) -> list[str]:
    """Drop all-NaN columns at train time (same policy as the winner model's
    ranking_position_diff auto-drop)."""
    return [c for c in METHOD_FEATURE_COLUMNS if not dataset[c].isna().all()]


def _fit_model(params: dict, x: np.ndarray, y: pd.Series) -> XGBClassifier:
    model = XGBClassifier(
        objective="multi:softprob",
        eval_metric="mlogloss",
        random_state=42,
        **params,
    )
    model.fit(x, y)
    return model


def cross_validate_method_params(
    train_df: pd.DataFrame,
    parameter_grid: list[dict[str, int | float]],
    feature_columns: list[str],
) -> dict[str, int | float]:
    folds = build_time_series_folds(train_df)
    valid_folds = [
        fold
        for fold in folds
        if train_df.iloc[fold.train_idx]["target"].nunique() == len(METHOD_CLASSES)
        and train_df.iloc[fold.val_idx]["target"].nunique() >= 2
    ]
    if not valid_folds:
        return parameter_grid[0]
    best_score = float("inf")
    best_params = parameter_grid[0]
    for params in parameter_grid:
        fold_scores: list[float] = []
        for fold in valid_folds:
            fold_train = train_df.iloc[fold.train_idx]
            fold_val = train_df.iloc[fold.val_idx]
            imputer = SimpleImputer(strategy="median")
            x_train = imputer.fit_transform(fold_train[feature_columns])
            x_val = imputer.transform(fold_val[feature_columns])
            model = _fit_model(params, x_train, fold_train["target"])
            probabilities = model.predict_proba(x_val)
            fold_scores.append(
                log_loss(fold_val["target"], probabilities, labels=CLASS_LABELS)
            )
        mean_score = float(np.mean(fold_scores))
        if mean_score < best_score:
            best_score = mean_score
            best_params = params
    return best_params


def _probability_variants(
    estimator, imputer, feature_columns: list[str], frame: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """(raw, symmetrized) class-probability matrices for ``frame``.

    Production symmetrization: the method classes are invariant under a corner
    swap, so ``p_sym = (p(row) + p(swap_corners(row))) / 2`` per class. Reuses
    ``api._swap_corners`` (negates ``*_diff``, passes the symmetric features
    through) so this matches serving exactly."""
    raw = estimator.predict_proba(imputer.transform(frame[feature_columns]))
    swapped_records = [_swap_corners(r) for r in frame[feature_columns].to_dict("records")]
    swapped_frame = pd.DataFrame(swapped_records)[feature_columns]
    swapped = estimator.predict_proba(imputer.transform(swapped_frame))
    return raw, (raw + swapped) / 2.0


def _variant_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    predictions = probabilities.argmax(axis=1)
    return {
        "accuracy": float(accuracy_score(y_true, predictions)),
        "log_loss": float(log_loss(y_true, probabilities, labels=CLASS_LABELS)),
        "macro_auc_ovr": float(
            roc_auc_score(y_true, probabilities, multi_class="ovr", average="macro")
        ),
    }


def method_baselines(train_df: pd.DataFrame, test_df: pd.DataFrame) -> dict[str, float]:
    """Honest constant baselines the model must clearly beat.

    * majority-class accuracy: predict the train-majority method for every row.
    * priors log-loss: predict the train class frequencies for every row (the
      best constant probabilistic prediction).
    """
    y_test = test_df["target"].to_numpy()
    majority_class = int(train_df["target"].mode().iloc[0])
    priors = (
        train_df["target"].value_counts(normalize=True).reindex(CLASS_LABELS, fill_value=0.0)
    ).to_numpy()
    prior_matrix = np.tile(priors, (len(test_df), 1))
    return {
        "majority_class": float(majority_class),
        "majority_accuracy": float(np.mean(y_test == majority_class)),
        "priors_log_loss": float(log_loss(y_test, prior_matrix, labels=CLASS_LABELS)),
        "uniform_log_loss": float(np.log(len(METHOD_CLASSES))),
    }


def select_calibration(
    model: XGBClassifier, x_cal: np.ndarray, y_cal: np.ndarray
) -> tuple[CalibratedClassifierCV, str, list[tuple[str, float]]]:
    """Pick isotonic vs sigmoid by out-of-fold log-loss, refit on the full holdout."""
    folds = StratifiedKFold(n_splits=N_CALIBRATION_FOLDS, shuffle=True, random_state=42)
    selection: list[tuple[str, float]] = []
    for candidate in CANDIDATE_CALIBRATIONS:
        estimator = CalibratedClassifierCV(FrozenEstimator(model), method=candidate)
        oof = cross_val_predict(estimator, x_cal, y_cal, cv=folds, method="predict_proba")
        selection.append((candidate, float(log_loss(y_cal, oof, labels=CLASS_LABELS))))
    selection.sort(key=lambda item: item[1])
    best_method = selection[0][0]
    calibrator = CalibratedClassifierCV(FrozenEstimator(model), method=best_method)
    calibrator.fit(x_cal, y_cal)
    return calibrator, best_method, selection


def build_metrics_section(
    *,
    train_df: pd.DataFrame,
    calibration_df: pd.DataFrame,
    test_df: pd.DataFrame,
    best_params: dict,
    calibration_method: str,
    variants: dict[str, np.ndarray],
    baselines: dict[str, float],
    confusion: np.ndarray,
    report: str,
    feature_importance: list[tuple[str, float]],
    feature_columns: list[str],
    trained_at: str,
) -> str:
    y_true = test_df["target"].to_numpy()
    headline = _variant_metrics(y_true, variants[HEADLINE_VARIANT])
    lines = [
        SECTION_HEADER,
        "",
        "Modelo de METODO de victoria (decision / ko / submission), XGBoost "
        "multi:softprob + calibracion, servido simetrizado por esquinas. Clases "
        f"(orden del target y de method_classes): {METHOD_CLASSES}.",
        "",
        f"- Trained at: {trained_at}",
        f"- Training rows: {len(train_df)} | Calibration rows: {len(calibration_df)} | Test rows: {len(test_df)}",
        f"- Test date range: {test_df['event_date'].min().date()} to {test_df['event_date'].max().date()}",
        f"- Best params: {best_params}",
        f"- Calibration method: {calibration_method}",
        "",
        "### HEADLINE (production-equivalent: symmetrized + calibrated)",
        f"- Accuracy: {headline['accuracy']:.4f}",
        f"- Log loss: {headline['log_loss']:.4f}",
        f"- Macro AUC (OvR): {headline['macro_auc_ovr']:.4f}",
        "",
        "### Baselines (constant predictors)",
        f"- majority class ({METHOD_CLASSES[int(baselines['majority_class'])]}) accuracy: {baselines['majority_accuracy']:.4f}",
        f"- train-priors log loss: {baselines['priors_log_loss']:.4f}",
        f"- uniform log loss: {baselines['uniform_log_loss']:.4f}",
        "",
        "### Variants {raw, symmetrized} x {uncalibrated, calibrated}",
        "",
        "| Variant | Accuracy | Log loss | Macro AUC |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key, label in _VARIANT_LABELS.items():
        if key not in variants:
            continue
        metrics = _variant_metrics(y_true, variants[key])
        marker = " **<-** " if key == HEADLINE_VARIANT else ""
        lines.append(
            f"| {label}{marker} | {metrics['accuracy']:.4f} | "
            f"{metrics['log_loss']:.4f} | {metrics['macro_auc_ovr']:.4f} |"
        )
    lines.extend(
        [
            "",
            "### Confusion matrix (rows=true, cols=pred; order " + str(METHOD_CLASSES) + ")",
            "",
            f"`{confusion.tolist()}`",
            "",
            "### Classification report",
            "",
            "```text",
            report,
            "```",
            "",
            f"### Feature importance ({len(feature_columns)} features)",
        ]
    )
    for name, score in feature_importance:
        lines.append(f"- {name}: {score:.6f}")
    return "\n".join(lines).rstrip() + "\n"


def write_section_idempotent(section: str) -> None:
    """Insert or replace the method section in model_metrics.md (same approach
    as evaluate.write_section_idempotent, scoped to this section's header)."""
    existing = METRICS_PATH.read_text(encoding="utf-8") if METRICS_PATH.exists() else ""
    if SECTION_HEADER in existing:
        start = existing.index(SECTION_HEADER)
        before = existing[:start]
        rest = existing[start + len(SECTION_HEADER):]
        next_h2 = re.search(r"\n## ", rest)
        after = rest[next_h2.start():] if next_h2 else ""
        new_text = before.rstrip() + "\n\n" + section.rstrip() + "\n"
        if after:
            new_text += "\n" + after.lstrip("\n")
    else:
        base = existing.rstrip()
        new_text = (base + "\n\n" if base else "") + section.rstrip() + "\n"
    METRICS_PATH.write_text(new_text, encoding="utf-8")


def main() -> None:
    dataset = load_method_dataset()
    train_df, calibration_df, test_df = chronological_three_way_split(dataset)
    feature_columns = get_available_method_columns(train_df)

    parameter_grid = list(
        ParameterGrid(
            {
                "n_estimators": [50, 100, 200],
                "max_depth": [2, 3, 4],
                "learning_rate": [0.03, 0.05, 0.1],
                "subsample": [0.8, 1.0],
                "colsample_bytree": [0.8, 1.0],
            }
        )
    )
    best_params = cross_validate_method_params(train_df, parameter_grid, feature_columns)

    imputer = SimpleImputer(strategy="median")
    x_train = imputer.fit_transform(train_df[feature_columns])
    model = _fit_model(best_params, x_train, train_df["target"])
    if list(model.classes_) != CLASS_LABELS:
        raise RuntimeError(f"Unexpected class order from XGBoost: {model.classes_}")

    x_cal = imputer.transform(calibration_df[feature_columns])
    y_cal = calibration_df["target"].to_numpy()
    calibrator, calibration_method, selection = select_calibration(model, x_cal, y_cal)

    raw_uncal, sym_uncal = _probability_variants(model, imputer, feature_columns, test_df)
    raw_cal, sym_cal = _probability_variants(calibrator, imputer, feature_columns, test_df)
    variants = {
        "raw_uncalibrated": raw_uncal,
        "symmetrized_uncalibrated": sym_uncal,
        "raw_calibrated": raw_cal,
        "symmetrized_calibrated": sym_cal,
    }
    y_test = test_df["target"].to_numpy()
    headline_pred = sym_cal.argmax(axis=1)
    confusion = confusion_matrix(y_test, headline_pred, labels=CLASS_LABELS)
    report = classification_report(
        y_test,
        headline_pred,
        labels=CLASS_LABELS,
        target_names=METHOD_CLASSES,
        digits=4,
        zero_division=0,
    )
    baselines = method_baselines(train_df, test_df)
    importance_pairs = sorted(
        zip(feature_columns, model.feature_importances_, strict=True),
        key=lambda item: item[1],
        reverse=True,
    )
    feature_importance = [(name, float(score)) for name, score in importance_pairs]

    trained_at = datetime.now(timezone.utc).date().isoformat()
    headline_metrics = _variant_metrics(y_test, sym_cal)

    bundle = joblib.load(MODEL_PATH)
    if not isinstance(bundle, dict) or "model" not in bundle:
        raise RuntimeError("Existing winner bundle not found or malformed; aborting.")
    bundle["method_model"] = model
    bundle["method_imputer"] = imputer
    bundle["method_feature_columns"] = feature_columns
    bundle["method_classes"] = list(METHOD_CLASSES)
    bundle["method_calibrator"] = calibrator
    bundle["method_calibration_method"] = calibration_method
    bundle["method_trained_at"] = trained_at
    bundle["method_test_metrics"] = headline_metrics
    joblib.dump(bundle, MODEL_PATH)

    section = build_metrics_section(
        train_df=train_df,
        calibration_df=calibration_df,
        test_df=test_df,
        best_params=best_params,
        calibration_method=calibration_method,
        variants=variants,
        baselines=baselines,
        confusion=confusion,
        report=report,
        feature_importance=feature_importance,
        feature_columns=feature_columns,
        trained_at=trained_at,
    )
    write_section_idempotent(section)

    print("Train rows:", len(train_df), "| Calibration rows:", len(calibration_df), "| Test rows:", len(test_df))
    print("Best params:", best_params)
    print("Calibration selection (OOF log-loss):", selection)
    print("Headline (symmetrized + calibrated):", {k: round(v, 4) for k, v in headline_metrics.items()})
    print("Baselines:", {k: round(v, 4) for k, v in baselines.items()})
    print("Confusion matrix (rows=true, cols=pred):")
    print(confusion)
    print(report)
    print("Feature importance:")
    for name, score in feature_importance:
        print(f"  {name}: {score:.6f}")
    print(f"Bundle updated at {MODEL_PATH} (method_* keys) | metrics section written to {METRICS_PATH}")


if __name__ == "__main__":
    main()
