"""Train the UFC fight-winner model (XGBoost) and save it into the bundle.

Run with: ``python -m src.prediction.train`` (then ``python -m
src.prediction.calibrate``). Without options it does what it always did: the
training CSV and the bundle at their usual paths, the 20 legacy diffs, the median
imputer, the 108-point grid and seed 42.

The options turn it into the instrument of the pre-registered phase-4 measurement,
which trains several arms x seeds without ever writing the served bundle:

    --dataset PATH / --bundle PATH   other CSV / other bundle (other keys of an
                                     existing bundle, method_* included, are kept)
    --seed N                         random_state of the FINAL fit only; the grid
                                     and its folds always use GRID_SEED
    --feature-set NAME               a named column set of features/types.py
    --params JSON                    fixed XGBoost hyperparameters, no grid search
    --nan-policy {median,native}     median imputer, or NaN straight to XGBoost
    --no-test-report                 no test-period metric computed, printed or
                                     written (model_metrics.md is left alone)

The bundle records feature_set, nan_policy, xgb_params and train_seed, so a run
can be reproduced with ``--params`` and ``--seed`` (bundle_io.winner_training_config).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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

from src.prediction.bundle_io import save_bundle_preserving, winner_training_config
from src.prediction.features import FEATURE_COLUMNS
from src.prediction.features.types import (
    CORNER_PAIR_BASES,
    DEFAULT_FEATURE_SET,
    FEATURE_SETS,
    pair_columns,
)
from src.prediction.preprocessing import (
    DEFAULT_NAN_POLICY,
    NAN_POLICIES,
    NanPassthrough,
    make_imputer,
)

# The train / calibration / test split is the frozen "metro" in split.py (fixed
# dates, defined there and nowhere else). Re-exported here because calibrate.py,
# evaluate.py, train_method.py and archived experiment scripts import it from
# train.
from src.prediction.split import (  # noqa: F401  (re-exported)
    _test_split_index,
    chronological_three_way_split,
    chronological_train_test_split,
)

logger = logging.getLogger(__name__)

DATASET_PATH = Path("training_dataset.csv")
MODEL_PATH = Path("src/prediction/model.joblib")
METRICS_PATH = Path("src/prediction/model_metrics.md")

# Hyperparameter grid (108 points), scored by mean AUC over 3 chronological folds.
PARAMETER_GRID: dict[str, list[int | float]] = {
    "n_estimators": [50, 100, 200],
    "max_depth": [2, 3, 4],
    "learning_rate": [0.03, 0.05, 0.1],
    "subsample": [0.8, 1.0],
    "colsample_bytree": [0.8, 1.0],
}
# The grid search and its folds always use this seed, so every arm of a measurement
# picks its hyperparameters the same way; --seed only moves the final fit.
GRID_SEED = 42
DEFAULT_SEED = 42
# train.py sets these itself: --params cannot override them (--seed is the way).
_RESERVED_XGB_KEYS = ("objective", "eval_metric", "random_state")
# XGBoost draws random numbers only to subsample rows or columns. With all of these
# at 1.0 every seed gives the same model.
_SUBSAMPLING_KEYS = (
    "subsample",
    "colsample_bytree",
    "colsample_bylevel",
    "colsample_bynode",
)

# Un modelo recien entrenado se guarda SIN calibrador (bundle_io.py explica por
# que). El servicio seguiria funcionando, con probabilidades sin calibrar, y nadie
# lo notaria: por eso se avisa alto y con el comando exacto.
UNCALIBRATED_WARNING = "\n".join(
    [
        "",
        "=" * 78,
        "AVISO: el modelo nuevo se ha guardado SIN calibrador.",
        "Un calibrador lleva DENTRO el modelo con el que se calibro. El que hubiera",
        "en el bundle era del modelo ANTERIOR: conservarlo habria dejado produccion",
        "sirviendo ese modelo viejo, en silencio. Por eso no se conserva.",
        "Desplegado asi, el servicio daria probabilidades SIN calibrar, y el test",
        "del bundle commiteado (tests/test_calibrador_desparejado.py) esta en ROJO",
        "hasta que recalibres.",
        "",
        "Recalibra ahora:  python -m src.prediction.calibrate",
        "=" * 78,
    ]
)


@dataclass(frozen=True)
class FoldSplit:
    train_idx: np.ndarray
    val_idx: np.ndarray


@dataclass(frozen=True)
class PreparedFeatures:
    x_train: np.ndarray
    x_test: np.ndarray
    imputer: SimpleImputer | NanPassthrough
    feature_columns: list[str]


def load_dataset(
    path: Path | str | None = None, columns: list[str] | None = None
) -> pd.DataFrame:
    """The training CSV (default DATASET_PATH) in chronological order.

    Fails when it lacks the target or any of ``columns``, the columns of the
    chosen feature set (default: the 20 legacy diffs)."""
    path = DATASET_PATH if path is None else Path(path)
    required = list(FEATURE_COLUMNS if columns is None else columns)
    dataset = pd.read_csv(path, parse_dates=["event_date"])
    dataset = dataset.sort_values(["event_date", "fight_id", "target"]).reset_index(drop=True)
    missing_columns = [column for column in required + ["target"] if column not in dataset.columns]
    if missing_columns:
        raise RuntimeError(
            f"Dataset missing required columns: {missing_columns} (file: {path}). "
            "Every column of the chosen feature set must be in the CSV."
        )
    return dataset


def feature_set_columns(name: str) -> list[str]:
    """The columns of a named feature set (features/types.py FEATURE_SETS)."""
    if name not in FEATURE_SETS:
        raise RuntimeError(
            f"Unknown feature set {name!r}; this code knows {sorted(FEATURE_SETS)}."
        )
    return list(FEATURE_SETS[name])


def bundle_dataset_columns(bundle: dict) -> list[str]:
    """Columns a CSV must carry to calibrate or evaluate ``bundle``: those of the
    feature set it was trained with (legacy for a pre-phase-4 bundle, so today's
    requirement) plus, defensively, its own feature_columns."""
    set_columns = feature_set_columns(winner_training_config(bundle)["feature_set"])
    return list(dict.fromkeys([*set_columns, *bundle["feature_columns"]]))


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


def get_available_feature_columns(
    dataset: pd.DataFrame, columns: list[str] | None = None
) -> list[str]:
    """The columns of the chosen set (default: the 20 legacy diffs) that carry at
    least one value in ``dataset``, in the set's order.

    An all-NaN column is dropped, as it always was (ranking_position_diff in an
    early CSV). A per-corner pair ({base}_red / {base}_blue) goes as a whole: when
    either side is all-NaN both are dropped, because a corner swap EXCHANGES the
    two and half a pair cannot be swapped. Every drop is logged."""
    candidates = list(FEATURE_COLUMNS if columns is None else columns)
    empty = [column for column in candidates if dataset[column].isna().all()]
    dropped = set(empty)
    partners: list[str] = []
    for base in CORNER_PAIR_BASES:
        pair = pair_columns([base])
        if not set(pair) <= set(candidates) or not dropped & set(pair):
            continue
        partners.extend(column for column in pair if column not in dropped)
        dropped.update(pair)
    if empty:
        logger.warning("All-NaN feature columns dropped: %s", empty)
    if partners:
        logger.warning(
            "Dropped with the all-NaN other half of their corner pair: %s", partners
        )
    return [column for column in candidates if column not in dropped]


def prepare_features(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_columns: list[str],
    nan_policy: str = DEFAULT_NAN_POLICY,
) -> PreparedFeatures:
    # median: SimpleImputer fitted on train only. native: NanPassthrough, the NaN
    # reach XGBoost untouched (preprocessing.py).
    imputer = make_imputer(nan_policy)
    x_train = imputer.fit_transform(train_df[feature_columns])
    x_test = imputer.transform(test_df[feature_columns])
    return PreparedFeatures(
        x_train=x_train,
        x_test=x_test,
        imputer=imputer,
        feature_columns=feature_columns,
    )


def build_model(params: dict[str, int | float], seed: int) -> XGBClassifier:
    return XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=seed,
        **params,
    )


def seed_is_inert(params: dict[str, int | float]) -> bool:
    """True when XGBoost draws no random number with ``params`` (no row or column
    subsampling), so every --seed gives the same model."""
    return all(float(params.get(key, 1.0)) >= 1.0 for key in _SUBSAMPLING_KEYS)


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
    nan_policy: str = DEFAULT_NAN_POLICY,
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
            prepared = prepare_features(
                fold_train, fold_val, feature_columns, nan_policy=nan_policy
            )
            model = build_model(params, GRID_SEED)
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


@dataclass(frozen=True)
class EvaluationReport:
    """Test-period figures of the raw base model (what model_metrics.md shows)."""

    model_metrics: dict[str, float]
    baseline_metrics: dict[str, float]
    confusion: np.ndarray
    report: str


def evaluate_on_test(
    model: XGBClassifier,
    x_test: np.ndarray,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> EvaluationReport:
    probabilities = model.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)
    return EvaluationReport(
        model_metrics=evaluate_predictions(test_df["target"], probabilities),
        baseline_metrics=majority_class_baseline(train_df, test_df),
        confusion=confusion_matrix(test_df["target"], predictions),
        report=classification_report(
            test_df["target"], predictions, digits=4, zero_division=0
        ),
    )


def _xgb_params_arg(text: str) -> dict[str, int | float]:
    """argparse type of --params: a JSON object of XGBoost hyperparameters."""
    try:
        params = json.loads(text)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"not valid JSON ({error})") from error
    if not isinstance(params, dict):
        raise argparse.ArgumentTypeError(
            'must be a JSON object, e.g. \'{"max_depth": 3, "subsample": 0.8}\''
        )
    reserved = sorted(set(params) & set(_RESERVED_XGB_KEYS))
    if reserved:
        raise argparse.ArgumentTypeError(
            f"cannot set {reserved}: train.py fixes them (the seed goes in --seed)"
        )
    return params


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Defaults are read when this runs, so a test that points DATASET_PATH or
    # MODEL_PATH elsewhere moves the defaults too.
    parser = argparse.ArgumentParser(
        description=(
            "Entrena el modelo de ganador (XGBoost) y lo guarda en el bundle. "
            "Sin opciones, exactamente como siempre."
        )
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
        help=(
            "Bundle donde se guarda el modelo (por defecto: %(default)s, el que "
            "sirve produccion). Si ya existe, sus otras claves se conservan, "
            "method_* incluidas."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=(
            "Semilla del ajuste FINAL (por defecto: %(default)s). La rejilla y sus "
            f"pliegues usan siempre la semilla {GRID_SEED}."
        ),
    )
    parser.add_argument(
        "--feature-set",
        choices=sorted(FEATURE_SETS),
        default=DEFAULT_FEATURE_SET,
        help=(
            "Conjunto de variables de features/types.py FEATURE_SETS "
            "(por defecto: %(default)s, las 20 diferencias de siempre)."
        ),
    )
    parser.add_argument(
        "--params",
        type=_xgb_params_arg,
        default=None,
        help=(
            "Hiperparametros de XGBoost en JSON: se salta la rejilla y se usan "
            "tal cual (los usados quedan en el bundle como xgb_params)."
        ),
    )
    parser.add_argument(
        "--nan-policy",
        choices=NAN_POLICIES,
        default=DEFAULT_NAN_POLICY,
        help=(
            "median (por defecto): imputa con la mediana de train. native: los NaN "
            "llegan a XGBoost sin imputar (rejilla y ajuste final)."
        ),
    )
    parser.add_argument(
        "--no-test-report",
        action="store_true",
        help=(
            "No calcula, no imprime y no escribe NINGUNA metrica del periodo de "
            "test (model_metrics.md no se toca). Para entrenar los brazos de una "
            "medicion pre-registrada sin mirar el test."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    candidate_columns = feature_set_columns(args.feature_set)
    dataset = load_dataset(args.dataset, candidate_columns)
    # Base model trains on train_df ONLY; calibration_df is the out-of-sample
    # holdout that calibrate.py fits on; test_df is the frozen test window
    # (split.py). Fights after the metro's TEST_END are in none of the three.
    train_df, calibration_df, test_df = chronological_three_way_split(dataset)
    feature_columns = get_available_feature_columns(train_df, candidate_columns)
    if args.params is None:
        parameter_grid = list(ParameterGrid(PARAMETER_GRID))
        best_params = cross_validate_params(
            train_df, parameter_grid, feature_columns, nan_policy=args.nan_policy
        )
        params_source = "The grid"
    else:
        best_params = dict(args.params)
        params_source = "--params"
    if seed_is_inert(best_params):
        logger.warning(
            "%s gives subsample=%s and colsample_bytree=%s: without row or column "
            "subsampling XGBoost draws no random number, so every --seed gives the "
            "SAME model (a multi-seed measurement would repeat one model).",
            params_source,
            best_params.get("subsample", 1.0),
            best_params.get("colsample_bytree", 1.0),
        )
    prepared = prepare_features(
        train_df, test_df, feature_columns, nan_policy=args.nan_policy
    )
    model = build_model(best_params, args.seed)
    model.fit(prepared.x_train, train_df["target"])
    # --no-test-report: the test slice is never scored, not even in memory.
    evaluation = (
        None
        if args.no_test_report
        else evaluate_on_test(model, prepared.x_test, train_df, test_df)
    )
    feature_importance = format_feature_importance(model, feature_columns)

    # ISO date so the UI can show "Modelo entrenado el <fecha>" (#29).
    trained_at = datetime.now(timezone.utc).date().isoformat()
    # NO usar joblib.dump directo: este bundle tambien lleva el modelo de metodo
    # (12 claves method_*), y un dump plano lo borraria. El calibrador del modelo
    # de ganador, en cambio, NO sobrevive: se calibro sobre el modelo anterior y
    # lo lleva dentro, asi que save_bundle_preserving lo quita (ver bundle_io.py)
    # y el aviso del final pide recalibrar.
    bundle = save_bundle_preserving(
        args.bundle,
        {
            "model": model,
            "imputer": prepared.imputer,
            "feature_columns": feature_columns,
            "trained_at": trained_at,
            # How it was trained (bundle_io.winner_training_config): enough to
            # rebuild the same model with --feature-set, --nan-policy, --params
            # and --seed.
            "feature_set": args.feature_set,
            "nan_policy": args.nan_policy,
            "xgb_params": dict(best_params),
            "train_seed": args.seed,
        },
    )
    if evaluation is not None:
        write_metrics_report(
            train_df=train_df,
            calibration_df=calibration_df,
            test_df=test_df,
            best_params=best_params,
            model_metrics=evaluation.model_metrics,
            baseline_metrics=evaluation.baseline_metrics,
            confusion=evaluation.confusion,
            report=evaluation.report,
            feature_importance=feature_importance,
            feature_columns=feature_columns,
            trained_at=trained_at,
        )

    print("Train rows:", len(train_df))
    print("Calibration-holdout rows:", len(calibration_df))
    print("Test rows:", len(test_df))
    print("Best params:", best_params)
    print(
        f"Feature set: {args.feature_set} ({len(feature_columns)} columns) | "
        f"NaN policy: {args.nan_policy} | final-fit seed: {args.seed} | "
        f"bundle: {args.bundle}"
    )
    if evaluation is None:
        print("--no-test-report: no test-period metric computed, printed or written.")
    else:
        model_metrics = evaluation.model_metrics
        baseline_metrics = evaluation.baseline_metrics
        print("Model metrics:", {key: round(value, 4) for key, value in model_metrics.items()})
        print("Majority-class baseline:", {key: round(value, 4) for key, value in baseline_metrics.items()})
        print("Confusion matrix:")
        print(evaluation.confusion)
        print("Classification report:")
        print(evaluation.report)
    print("Feature importance:")
    for feature_name, score in feature_importance:
        print(f"{feature_name}: {score:.6f}")
    # Lo ultimo que se imprime, para que no se pierda entre las metricas.
    if bundle.get("calibrator") is None:
        print(UNCALIBRATED_WARNING)
        if args.bundle != MODEL_PATH or args.dataset != DATASET_PATH:
            command = (
                "python -m src.prediction.calibrate "
                f"--dataset {args.dataset} --bundle {args.bundle}"
            )
            if args.no_test_report:
                command += " --no-test-report"
            print(f"Para ESTE bundle:  {command}")


if __name__ == "__main__":
    main()
