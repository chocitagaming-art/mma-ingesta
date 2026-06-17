from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    accuracy_score,
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


def chronological_train_test_split(dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_index = max(int(len(dataset) * (1 - TEST_SIZE)), MIN_TRAIN_ROWS)
    split_index = min(split_index, len(dataset) - 1)
    train_df = dataset.iloc[:split_index].copy()
    test_df = dataset.iloc[split_index:].copy()
    return train_df, test_df


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
    best_score = float("-inf")
    best_params = parameter_grid[0]
    for params in parameter_grid:
        fold_scores: list[float] = []
        for fold in folds:
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
        mean_score = float(np.mean(fold_scores))
        if mean_score > best_score:
            best_score = mean_score
            best_params = params
    return best_params


def favorite_baseline(test_df: pd.DataFrame) -> np.ndarray:
    if "ranking_position_diff" in test_df.columns:
        baseline = np.where(
            test_df["ranking_position_diff"].fillna(0) <= 0,
            1.0,
            0.0,
        )
        return baseline.astype(float)
    return np.full(len(test_df), 0.5)


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
    test_df: pd.DataFrame,
    best_params: dict[str, int | float],
    model_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
    confusion: np.ndarray,
    report: str,
    feature_importance: list[tuple[str, float]],
) -> None:
    lines = [
        "# UFC Fight Winner Model Metrics",
        "",
        f"- Training rows: {len(train_df)}",
        f"- Test rows: {len(test_df)}",
        f"- Train date range: {train_df['event_date'].min().date()} to {train_df['event_date'].max().date()}",
        f"- Test date range: {test_df['event_date'].min().date()} to {test_df['event_date'].max().date()}",
        f"- Best params: {best_params}",
        "",
        "## Model Metrics",
    ]
    for metric_name, metric_value in model_metrics.items():
        lines.append(f"- {metric_name}: {metric_value:.4f}")
    lines.extend(["", "## Baseline Metrics"])
    for metric_name, metric_value in baseline_metrics.items():
        lines.append(f"- {metric_name}: {metric_value:.4f}")
    lines.extend(
        [
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
    train_df, test_df = chronological_train_test_split(dataset)
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
    baseline_probabilities = favorite_baseline(test_df)
    baseline_metrics = evaluate_predictions(test_df["target"], baseline_probabilities)
    predictions = (probabilities >= 0.5).astype(int)
    confusion = confusion_matrix(test_df["target"], predictions)
    report = classification_report(test_df["target"], predictions, digits=4, zero_division=0)
    feature_importance = format_feature_importance(model, feature_columns)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "imputer": prepared.imputer,
            "feature_columns": feature_columns,
        },
        MODEL_PATH,
    )
    write_metrics_report(
        train_df=train_df,
        test_df=test_df,
        best_params=best_params,
        model_metrics=model_metrics,
        baseline_metrics=baseline_metrics,
        confusion=confusion,
        report=report,
        feature_importance=feature_importance,
    )

    print("Train rows:", len(train_df))
    print("Test rows:", len(test_df))
    print("Best params:", best_params)
    print("Model metrics:", {key: round(value, 4) for key, value in model_metrics.items()})
    print("Baseline metrics:", {key: round(value, 4) for key, value in baseline_metrics.items()})
    print("Confusion matrix:")
    print(confusion)
    print("Classification report:")
    print(report)
    print("Feature importance:")
    for feature_name, score in feature_importance:
        print(f"{feature_name}: {score:.6f}")


if __name__ == "__main__":
    main()