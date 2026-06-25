"""Probability calibration for the trained UFC fight-winner model.

This script does NOT retrain the base XGBoost model. It loads the persisted
bundle (``src/prediction/model.joblib`` = model + imputer + feature_columns),
reuses ``load_dataset`` and the chronological split from ``train.py`` to obtain
data, and fits a post-hoc calibrator on a HELD-OUT calibration slice that is
DISTINCT from the test slice that ``evaluate.py`` scores. The calibrated
estimator is added to the bundle under the key ``"calibrator"`` (the original
``model`` / ``imputer`` / ``feature_columns`` are preserved untouched).

Calibration data
----------------
The base model was trained on the whole TRAIN slice, so to avoid contaminating
``evaluate.py``'s metrics we take the most recent portion of TRAIN as the
calibration slice (never the test slice). That slice is split chronologically
into a fit part and a validation part: we fit both an isotonic and a sigmoid
calibrator on the fit part, score Brier on the validation part, keep whichever
is better, and finally refit the winning method on the FULL calibration slice
(more data) before persisting.

Prefit calibration
-------------------
``CalibratedClassifierCV(cv="prefit")`` was removed in scikit-learn 1.6+; the
modern, equivalent way to calibrate an already-fitted estimator without
retraining it is ``CalibratedClassifierCV(FrozenEstimator(model))``. The frozen
wrapper guarantees the base XGBoost is never refit.

Run with: ``python -m src.prediction.calibrate``
"""

from __future__ import annotations

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sklearn.model_selection import StratifiedKFold, cross_val_predict

from src.prediction.train import (
    MODEL_PATH,
    chronological_train_test_split,
    get_available_feature_columns,
    load_dataset,
)

# Fraction of the TRAIN slice (most recent rows) reserved for calibration. This
# is carved out of TRAIN only, never out of the evaluate.py test slice.
CALIBRATION_FRACTION = 0.25
# Number of folds used to compare candidate methods by out-of-fold Brier. A
# cross-validated estimate is far steadier than a single split on a slice this
# small, where isotonic can win by noise yet overfit into extreme 0/1 outputs.
N_SELECTION_FOLDS = 5
CANDIDATE_METHODS = ("isotonic", "sigmoid")


def load_model_bundle() -> dict:
    """Load the persisted bundle (model + imputer + feature_columns)."""
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"Trained model not found at {MODEL_PATH}. Run `python -m src.prediction.train` first."
        )
    bundle = joblib.load(MODEL_PATH)
    for key in ("model", "imputer", "feature_columns"):
        if key not in bundle:
            raise RuntimeError(f"Model bundle is missing required key: {key!r}")
    return bundle


def fit_prefit_calibrator(
    model, method: str, x: np.ndarray, y: np.ndarray
) -> CalibratedClassifierCV:
    """Fit a calibrator on top of the FROZEN base model (no retraining).

    ``FrozenEstimator`` is the scikit-learn 1.6+ replacement for the removed
    ``cv="prefit"`` option: it pins the already-trained model so only the
    calibration map is learned.
    """
    calibrator = CalibratedClassifierCV(FrozenEstimator(model), method=method)
    calibrator.fit(x, y)
    return calibrator


def main() -> None:
    dataset = load_dataset()
    train_df, test_df = chronological_train_test_split(dataset)

    bundle = load_model_bundle()
    model = bundle["model"]
    imputer = bundle["imputer"]
    feature_columns = list(bundle["feature_columns"])

    # Guard against feature drift between the saved bundle and the current data.
    expected_columns = get_available_feature_columns(train_df)
    if feature_columns != expected_columns:
        print(
            "[warn] Saved feature_columns differ from train.py's "
            "get_available_feature_columns(train_df); using the saved columns "
            "(the model was trained on them)."
        )

    # Held-out calibration slice = most recent rows of TRAIN (kept fully apart
    # from the evaluate.py test slice so those metrics stay honest).
    cal_start = max(int(len(train_df) * (1 - CALIBRATION_FRACTION)), 1)
    calibration_df = train_df.iloc[cal_start:].reset_index(drop=True)
    if len(calibration_df) < 50 or calibration_df["target"].nunique() < 2:
        raise RuntimeError(
            "Calibration slice is too small or single-class; cannot calibrate."
        )

    x_cal = imputer.transform(calibration_df[feature_columns])
    y_cal = calibration_df["target"].to_numpy()

    # Baseline (uncalibrated) out-of-fold Brier on the calibration slice. The
    # base model already saw these rows during training, so this in-sample number
    # is only a loose reference point for the calibrated estimates below.
    uncal_cal_brier = brier_score_loss(y_cal, model.predict_proba(x_cal)[:, 1])

    # Compare candidate methods by out-of-fold Brier (5-fold CV on the calibration
    # slice). cross_val_predict clones the estimator per fold; FrozenEstimator
    # keeps the base XGBoost fitted across clones, so only the calibration map is
    # learned each fold and the base model is never retrained.
    folds = StratifiedKFold(n_splits=N_SELECTION_FOLDS, shuffle=True, random_state=42)
    selection: list[tuple[str, float]] = []
    for method in CANDIDATE_METHODS:
        estimator = CalibratedClassifierCV(FrozenEstimator(model), method=method)
        oof_prob = cross_val_predict(
            estimator, x_cal, y_cal, cv=folds, method="predict_proba"
        )[:, 1]
        selection.append((method, float(brier_score_loss(y_cal, oof_prob))))

    selection.sort(key=lambda item: item[1])
    best_method, best_cv_brier = selection[0]

    # Fit the winning method on the FULL calibration slice for the final
    # calibrator (uses every calibration row).
    calibrator = fit_prefit_calibrator(model, best_method, x_cal, y_cal)

    # Diagnostics on the evaluate.py test slice (NOT used for fitting/selection),
    # purely to report how calibration shifts the test-set probabilities.
    x_test = imputer.transform(test_df[feature_columns])
    y_test = test_df["target"].to_numpy()
    base_test_prob = model.predict_proba(x_test)[:, 1]
    cal_test_prob = calibrator.predict_proba(x_test)[:, 1]

    base_brier = brier_score_loss(y_test, base_test_prob)
    cal_brier = brier_score_loss(y_test, cal_test_prob)
    base_logloss = log_loss(y_test, base_test_prob, labels=[0, 1])
    cal_logloss = log_loss(y_test, cal_test_prob, labels=[0, 1])
    base_acc = accuracy_score(y_test, (base_test_prob >= 0.5).astype(int))
    cal_acc = accuracy_score(y_test, (cal_test_prob >= 0.5).astype(int))

    # Persist: add the calibrator, keep model/imputer/feature_columns intact.
    out_bundle = dict(bundle)
    out_bundle["calibrator"] = calibrator
    out_bundle["calibration_method"] = best_method
    joblib.dump(out_bundle, MODEL_PATH)

    print("=== Probability calibration (base XGBoost frozen, not retrained) ===")
    print(f"Train rows: {len(train_df)} | Test rows (evaluate.py): {len(test_df)}")
    print(
        f"Calibration slice (most recent TRAIN): {len(calibration_df)} rows, "
        f"{N_SELECTION_FOLDS}-fold CV for method selection"
    )
    print(f"Calibration-slice Brier (uncalibrated base, in-sample): {uncal_cal_brier:.4f}")
    print("Method selection (out-of-fold CV Brier, lower is better):")
    for method, score in selection:
        marker = "  <- chosen" if method == best_method else ""
        print(f"  {method:<9} brier={score:.4f}{marker}")
    print(f"Chosen method: {best_method}")
    print()
    print("Test-slice diagnostics (for reference only; not used to fit/select):")
    print(f"  Brier    base={base_brier:.4f} -> calibrated={cal_brier:.4f}")
    print(f"  LogLoss  base={base_logloss:.4f} -> calibrated={cal_logloss:.4f}")
    print(f"  Accuracy base={base_acc:.4f} -> calibrated={cal_acc:.4f}")
    print()
    print(f"Saved calibrator into bundle at {MODEL_PATH} (key 'calibrator').")


if __name__ == "__main__":
    main()
