from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
import xgboost
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import ParameterGrid
from xgboost import XGBClassifier

from src.prediction.features import FEATURE_COLUMNS


DATASET_PATH = Path("training_dataset.csv")
MODEL_PATH = Path("src/prediction/model.joblib")
METRICS_PATH = Path("src/prediction/model_metrics.md")
TEST_SIZE = 0.2
# Out-of-sample calibration holdout carved between train and test (next ~16% of
# the chronology), so calibrate.py can fit on rows the base model never saw.
CALIBRATION_SIZE = 0.16
MIN_TRAIN_ROWS = 40


@dataclass(frozen=True)
class FoldSplit:
    train_idx: np.ndarray
    val_idx: np.ndarray


@dataclass(frozen=True)
class PreparedFeatures:
    x_train: np.ndarray
    x_test: np.ndarray
    imputer: SimpleImputer
    feature_columns: list[str]


def load_dataset() -> pd.DataFrame:
    dataset = pd.read_csv(DATASET_PATH, parse_dates=["event_date"])
    dataset = dataset.sort_values(["event_date", "fight_id", "target"]).reset_index(drop=True)
    missing_columns = [column for column in FEATURE_COLUMNS + ["target"] if column not in dataset.columns]
    if missing_columns:
        raise RuntimeError(f"Dataset missing required columns: {missing_columns}")
    return dataset


def _test_split_index(dataset: pd.DataFrame) -> int:
    """Positional index where the chronological test slice (last TEST_SIZE)
    begins. Shared by the two-way and three-way splits so the test boundary -
    and therefore the reported test metrics - stays identical across both."""
    split_index = max(int(len(dataset) * (1 - TEST_SIZE)), MIN_TRAIN_ROWS)
    return min(split_index, len(dataset) - 1)


def chronological_train_test_split(dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_index = _test_split_index(dataset)
    train_df = dataset.iloc[:split_index].copy()
    test_df = dataset.iloc[split_index:].copy()
    return train_df, test_df


def chronological_three_way_split(
    dataset: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Positional chronological split into (train, calibration_holdout, test).

    train = first ~64%, calibration-holdout = next ~16%, test = last ~20%. The
    test boundary is the SAME positional index as ``chronological_train_test_split``
    (so test metrics stay comparable), while the calibration-holdout is carved out
    of what used to be the tail of TRAIN. The base model trains on ``train_df``
    only; ``calibrate.py`` fits the calibrator on ``calibration_holdout_df`` (rows
    the base never saw), which removes the in-sample calibration bug. No shuffle."""
    test_start = _test_split_index(dataset)
    cal_start = max(int(len(dataset) * (1 - TEST_SIZE - CALIBRATION_SIZE)), 1)
    cal_start = min(cal_start, test_start - 1)
    train_df = dataset.iloc[:cal_start].copy()
    calibration_holdout_df = dataset.iloc[cal_start:test_start].copy()
    test_df = dataset.iloc[test_start:].copy()
    return train_df, calibration_holdout_df, test_df


def build_time_series_folds(train_df: pd.DataFrame, n_splits: int = 3) -> list[FoldSplit]:
    fold_boundaries = np.linspace(0, len(train_df), n_splits + 2, dtype=int)
    folds: list[FoldSplit] = []
    for fold_index in range(n_splits):
        train_end = fold_boundaries[fold_index + 1]
        val_end = fold_boundaries[fold_index + 2]
        train_idx = np.arange(0, train_end)
        val_idx = np.arange(train_end, val_end)
        if len(train_idx) == 0 or len(val_idx) == 0:
            continue
        folds.append(FoldSplit(train_idx=train_idx, val_idx=val_idx))
    if not folds:
        raise RuntimeError("Unable to create chronological validation folds.")
    return folds


def _has_both_classes(values: pd.Series) -> bool:
    return values.nunique(dropna=True) >= 2


def get_available_feature_columns(dataset: pd.DataFrame) -> list[str]:
    return [column for column in FEATURE_COLUMNS if not dataset[column].isna().all()]


def prepare_features(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_columns: list[str],
) -> PreparedFeatures:
    imputer = SimpleImputer(strategy="median")
    x_train = imputer.fit_transform(train_df[feature_columns])
    x_test = imputer.transform(test_df[feature_columns])
    return PreparedFeatures(
        x_train=x_train,
        x_test=x_test,
        imputer=imputer,
        feature_columns=feature_columns,
    )


def evaluate_predictions(y_true: pd.Series, probabilities: np.ndarray, threshold: float = 0.5) -> dict[str, float]:
    predictions = (probabilities >= threshold).astype(int)
    return {
        "accuracy": accuracy_score(y_true, predictions),
        "precision": precision_score(y_true, predictions, zero_division=0),
        "recall": recall_score(y_true, predictions, zero_division=0),
        "f1": f1_score(y_true, predictions, zero_division=0),
        "roc_auc": roc_auc_score(y_true, probabilities),
    }


def cross_validate_params(
    train_df: pd.DataFrame,
    parameter_grid: list[dict[str, int | float]],
    feature_columns: list[str],
) -> dict[str, int | float]:
    folds = build_time_series_folds(train_df)
    valid_folds = [
        fold
        for fold in folds
        if _has_both_classes(train_df.iloc[fold.train_idx]["target"])
        and _has_both_classes(train_df.iloc[fold.val_idx]["target"])
    ]
    if not valid_folds:
        return parameter_grid[0]
    best_score = float("-inf")
    best_params = parameter_grid[0]
    for params in parameter_grid:
        fold_scores: list[float] = []
        for fold in valid_folds:
            fold_train = train_df.iloc[fold.train_idx]
            fold_val = train_df.iloc[fold.val_idx]
            prepared = prepare_features(fold_train, fold_val, feature_columns)
            model = XGBClassifier(
                objective="binary:logistic",
                eval_metric="logloss",
                random_state=42,
                **params,
            )
            model.fit(prepared.x_train, fold_train["target"])
            probabilities = model.predict_proba(prepared.x_test)[:, 1]
            fold_scores.append(roc_auc_score(fold_val["target"], probabilities))
        if not fold_scores:
            continue
        mean_score = float(np.mean(fold_scores))
        if mean_score > best_score:
            best_score = mean_score
            best_params = params
    return best_params


def majority_class_baseline(
    train_df: pd.DataFrame, test_df: pd.DataFrame
) -> dict[str, float]:
    """Honest majority-class baseline.

    Predicts the TRAIN-majority class for every test row. As a constant predictor
    its accuracy equals the test rate of that class, it cannot rank cases (ROC-AUC
    0.5), and scored as a constant 0.5 probability it has Brier 0.25. This replaces
    the old 'favorite' baseline, which thresholded ranking_position_diff and
    degenerated to 'always predict red' (recall 1.0, ROC-AUC 0.5 - a misleading
    F1). Odds are deliberately NOT used: they are unavailable historically and
    belong to the separate Model-vs-Market visual, not this pure model."""
    majority_class = int(train_df["target"].mode().iloc[0])
    y_test = test_df["target"].to_numpy()
    accuracy = float(np.mean(y_test == majority_class))
    constant_half = np.full(len(test_df), 0.5)
    brier_always_half = float(brier_score_loss(y_test, constant_half))
    return {
        "majority_class": float(majority_class),
        "accuracy": accuracy,
        "roc_auc": 0.5,
        "brier_always_0.5": brier_always_half,
    }


def format_feature_importance(
    model: XGBClassifier,
    feature_columns: list[str],
) -> list[tuple[str, float]]:
    importances = model.feature_importances_
    pairs = sorted(
        zip(feature_columns, importances, strict=True),
        key=lambda item: item[1],
        reverse=True,
    )
    return [(name, float(score)) for name, score in pairs]


def write_metrics_report(
    train_df: pd.DataFrame,
    calibration_df: pd.DataFrame,
    test_df: pd.DataFrame,
    best_params: dict[str, int | float],
    model_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
    confusion: np.ndarray,
    report: str,
    feature_importance: list[tuple[str, float]],
    feature_columns: list[str],
    trained_at: str,
) -> None:
    lines = [
        "# UFC Fight Winner Model Metrics",
        "",
        f"- Trained at: {trained_at}",
        f"- xgboost version: {xgboost.__version__}",
        f"- scikit-learn version: {sklearn.__version__}",
        f"- Training rows: {len(train_df)}",
        f"- Calibration-holdout rows: {len(calibration_df)}",
        f"- Test rows: {len(test_df)}",
        f"- Train date range: {train_df['event_date'].min().date()} to {train_df['event_date'].max().date()}",
        f"- Calibration-holdout date range: {calibration_df['event_date'].min().date()} to {calibration_df['event_date'].max().date()}",
        f"- Test date range: {test_df['event_date'].min().date()} to {test_df['event_date'].max().date()}",
        f"- Best params: {best_params}",
        "",
        "## Headline accuracy",
        "",
        "The PRODUCTION-EQUIVALENT headline (symmetrized + calibrated accuracy) is "
        "reported in the `## Diagnostico (evaluate.py)` section, written after "
        "calibration. The numbers in this section are the raw base-model metrics "
        "(uncalibrated, single corner orientation) and serve as a SECONDARY "
        "reference only.",
        "",
        f"## Features ({len(feature_columns)})",
        "",
        "Pure model: NO odds are used as an input feature (odds feed only the "
        "separate Model-vs-Market visual).",
        "",
    ]
    for feature_name in feature_columns:
        lines.append(f"- {feature_name}")
    lines.extend(["", "## Model Metrics (raw, uncalibrated, single orientation - secondary)"])
    for metric_name, metric_value in model_metrics.items():
        lines.append(f"- {metric_name}: {metric_value:.4f}")
    lines.extend(
        [
            "",
            "## Majority-class baseline",
            "",
            "Predicts the train-majority class for every test row (no odds, no "
            "ranking heuristic). Accuracy = the test rate of that class; as a "
            "constant predictor ROC-AUC is 0.5 and a constant-0.5 probability has "
            "Brier 0.25.",
            f"- majority_class: {int(baseline_metrics['majority_class'])}",
            f"- accuracy (class rate): {baseline_metrics['accuracy']:.4f}",
            f"- roc_auc: {baseline_metrics['roc_auc']:.4f}",
            f"- brier (always 0.5): {baseline_metrics['brier_always_0.5']:.4f}",
            "",
            "## Confusion Matrix",
            "",
            f"`{confusion.tolist()}`",
            "",
            "## Classification Report",
            "",
            "```text",
            report,
            "```",
            "",
            "## Feature Importance",
        ]
    )
    for feature_name, score in feature_importance:
        lines.append(f"- {feature_name}: {score:.6f}")
    METRICS_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    dataset = load_dataset()
    # Base model trains on train_df ONLY; calibration_df is the out-of-sample
    # holdout that calibrate.py fits on; test_df is the unchanged last-20% slice.
    train_df, calibration_df, test_df = chronological_three_way_split(dataset)
    feature_columns = get_available_feature_columns(train_df)
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
    best_params = cross_validate_params(train_df, parameter_grid, feature_columns)
    prepared = prepare_features(train_df, test_df, feature_columns)
    model = XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        **best_params,
    )
    model.fit(prepared.x_train, train_df["target"])
    probabilities = model.predict_proba(prepared.x_test)[:, 1]
    model_metrics = evaluate_predictions(test_df["target"], probabilities)
    baseline_metrics = majority_class_baseline(train_df, test_df)
    predictions = (probabilities >= 0.5).astype(int)
    confusion = confusion_matrix(test_df["target"], predictions)
    report = classification_report(test_df["target"], predictions, digits=4, zero_division=0)
    feature_importance = format_feature_importance(model, feature_columns)

    # ISO date so the UI can show "Modelo entrenado el <fecha>" (#29).
    trained_at = datetime.now(timezone.utc).date().isoformat()
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "imputer": prepared.imputer,
            "feature_columns": feature_columns,
            "trained_at": trained_at,
        },
        MODEL_PATH,
    )
    write_metrics_report(
        train_df=train_df,
        calibration_df=calibration_df,
        test_df=test_df,
        best_params=best_params,
        model_metrics=model_metrics,
        baseline_metrics=baseline_metrics,
        confusion=confusion,
        report=report,
        feature_importance=feature_importance,
        feature_columns=feature_columns,
        trained_at=trained_at,
    )

    print("Train rows:", len(train_df))
    print("Calibration-holdout rows:", len(calibration_df))
    print("Test rows:", len(test_df))
    print("Best params:", best_params)
    print("Model metrics:", {key: round(value, 4) for key, value in model_metrics.items()})
    print("Majority-class baseline:", {key: round(value, 4) for key, value in baseline_metrics.items()})
    print("Confusion matrix:")
    print(confusion)
    print("Classification report:")
    print(report)
    print("Feature importance:")
    for feature_name, score in feature_importance:
        print(f"{feature_name}: {score:.6f}")


if __name__ == "__main__":
    main()