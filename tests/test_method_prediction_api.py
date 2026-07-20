"""Serving-side tests for the method prediction (_predict_method).

Trains a TINY real XGBoost multiclass model on synthetic data (same approach as
test_attribution.py) so the corner-symmetry and probability guarantees are
exercised against the real estimator stack (imputer + XGBClassifier +
CalibratedClassifierCV), fully offline.
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from src.prediction.api import _predict_method, _swap_corners
from src.prediction.features import FighterHistorySummary, build_feature_row
from src.prediction.features.method_features import (
    METHOD_CLASSES,
    METHOD_FEATURE_COLUMNS,
    build_method_feature_row,
)


def _summary(seed: float) -> FighterHistorySummary:
    return FighterHistorySummary(
        total_prior_fights=int(5 + seed),
        total_rounds_fought=int(12 + seed),
        sig_strikes_landed_per_fight=40.0 + seed,
        sig_strike_accuracy=0.45,
        knockdowns_per_fight=0.2 + seed / 10,
        takedowns_landed_per_fight=1.0 + seed / 5,
        takedown_accuracy=0.4,
        submission_attempts_per_fight=0.5 + seed / 10,
        control_time_seconds_per_fight=100.0 + seed,
        win_streak=2,
        wins_last_5=3,
        pct_wins_by_ko=0.4 + seed / 100,
        pct_wins_by_submission=0.3,
        pct_wins_by_decision=0.3,
        days_since_last_fight=180,
        ranking_position=None,
        sig_strikes_absorbed_per_fight=30.0 + seed,
        sig_strike_defense=0.55,
        takedowns_absorbed_per_fight=1.2,
        takedown_defense=0.6,
        avg_opponent_prior_win_rate=0.5,
        latest_prior_fight_date=date(2025, 1, 1),
    )


def _method_row(red_seed: float = 2.0, blue_seed: float = 1.0) -> dict:
    red, blue = _summary(red_seed), _summary(blue_seed)
    base = build_feature_row(
        red,
        blue,
        red_height_cm=182.0 + red_seed,
        blue_height_cm=180.0 + blue_seed,
        red_reach_cm=185.0,
        blue_reach_cm=183.0,
        red_age=30.0,
        blue_age=29.0,
    )
    return build_method_feature_row(
        base, red, blue, scheduled_rounds=3, weight_class="Lightweight"
    )


@pytest.fixture(scope="module")
def method_bundle() -> dict:
    rng = np.random.default_rng(42)
    n_rows = 240
    x = rng.normal(0.0, 1.0, size=(n_rows, len(METHOD_FEATURE_COLUMNS)))
    frame = pd.DataFrame(x, columns=METHOD_FEATURE_COLUMNS)
    y = rng.integers(0, len(METHOD_CLASSES), size=n_rows)
    imputer = SimpleImputer(strategy="median")
    transformed = imputer.fit_transform(frame)
    model = XGBClassifier(
        objective="multi:softprob",
        eval_metric="mlogloss",
        n_estimators=8,
        max_depth=2,
        random_state=42,
    )
    model.fit(transformed, y)
    calibrator = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
    calibrator.fit(transformed, y)
    return {
        "method_model": model,
        "method_imputer": imputer,
        "method_feature_columns": list(METHOD_FEATURE_COLUMNS),
        "method_classes": list(METHOD_CLASSES),
        "method_calibrator": calibrator,
        "method_trained_at": "2026-07-19",
    }


def test_predict_method_none_for_pre_method_bundle():
    legacy_bundle = {"model": object(), "imputer": object(), "feature_columns": []}
    assert _predict_method(legacy_bundle, _method_row()) is None


def test_predict_method_shape_and_probabilities(method_bundle):
    result = _predict_method(method_bundle, _method_row())
    assert result is not None
    assert set(result["probabilities"]) == set(METHOD_CLASSES)
    values = list(result["probabilities"].values())
    assert all(0.0 <= value <= 1.0 for value in values)
    assert sum(values) == pytest.approx(1.0, abs=1e-9)
    assert result["predicted"] == max(result["probabilities"], key=result["probabilities"].get)
    assert result["trainedAt"] == "2026-07-19"


def test_predict_method_exact_corner_symmetry(method_bundle):
    """probabilities(A, B) must equal probabilities(B, A) exactly: the swapped
    row negates every diff and keeps the symmetric features, and the serving
    average (forward + swapped) / 2 is invariant under that exchange."""
    forward_row = _method_row(red_seed=3.0, blue_seed=1.0)
    swapped_row = _swap_corners(forward_row)

    result_ab = _predict_method(method_bundle, forward_row)
    result_ba = _predict_method(method_bundle, swapped_row)
    assert result_ab is not None and result_ba is not None
    for method_class in METHOD_CLASSES:
        assert result_ab["probabilities"][method_class] == pytest.approx(
            result_ba["probabilities"][method_class], abs=1e-12
        )
    assert result_ab["predicted"] == result_ba["predicted"]


def test_predict_method_without_calibrator_uses_base_model(method_bundle):
    uncalibrated = dict(method_bundle)
    uncalibrated.pop("method_calibrator")
    result = _predict_method(uncalibrated, _method_row())
    assert result is not None
    assert sum(result["probabilities"].values()) == pytest.approx(1.0, abs=1e-6)


def test_predict_method_handles_missing_history_row(method_bundle):
    """A debutant row (all history-derived values None) must still predict:
    the imputer fills medians in both orientations."""
    red = _summary(2.0)
    base = build_feature_row(
        red,
        None,
        red_height_cm=182.0,
        blue_height_cm=None,
        red_reach_cm=None,
        blue_reach_cm=None,
        red_age=30.0,
        blue_age=None,
    )
    row = build_method_feature_row(
        base, red, None, scheduled_rounds=None, weight_class=None
    )
    result = _predict_method(method_bundle, row)
    assert result is not None
    assert sum(result["probabilities"].values()) == pytest.approx(1.0, abs=1e-9)


# --- Ensemble half (XGBoost + shrunken logistic regression) --------------------


@pytest.fixture(scope="module")
def ensemble_bundle(method_bundle) -> dict:
    """The served bundle: same XGBoost plus the calibrated linear half."""
    rng = np.random.default_rng(7)
    n_rows = 240
    x = rng.normal(0.0, 1.0, size=(n_rows, len(METHOD_FEATURE_COLUMNS)))
    y = rng.integers(0, len(METHOD_CLASSES), size=n_rows)
    linear = Pipeline(
        [("scaler", StandardScaler()), ("logreg", LogisticRegression(C=0.001, max_iter=8000))]
    )
    linear.fit(x, y)
    linear_calibrator = CalibratedClassifierCV(FrozenEstimator(linear), method="sigmoid")
    linear_calibrator.fit(x, y)
    return {
        **method_bundle,
        "method_model_linear": linear,
        "method_calibrator_linear": linear_calibrator,
        "method_ensemble_weights": [0.5, 0.5],
    }


def test_ensemble_keeps_exact_corner_symmetry(ensemble_bundle):
    """Averaging models is linear, so it commutes with the forward/swapped
    average: adding the second half must not cost a single bit of symmetry."""
    forward_row = _method_row(red_seed=3.0, blue_seed=1.0)
    result_ab = _predict_method(ensemble_bundle, forward_row)
    result_ba = _predict_method(ensemble_bundle, _swap_corners(forward_row))

    assert result_ab is not None and result_ba is not None
    for method_class in METHOD_CLASSES:
        assert result_ab["probabilities"][method_class] == pytest.approx(
            result_ba["probabilities"][method_class], abs=1e-12
        )
    assert sum(result_ab["probabilities"].values()) == pytest.approx(1.0, abs=1e-9)


def test_ensemble_output_sits_between_its_two_halves(ensemble_bundle, method_bundle):
    """A 50/50 blend must land between the halves, never outside: that is what
    proves both estimators are really being consulted."""
    row = _method_row(red_seed=3.0, blue_seed=1.0)
    blended = _predict_method(ensemble_bundle, row)
    xgb_only = _predict_method(method_bundle, row)
    linear_only = _predict_method(
        {
            **method_bundle,
            "method_calibrator": ensemble_bundle["method_calibrator_linear"],
            "method_model": ensemble_bundle["method_model_linear"],
        },
        row,
    )
    assert blended is not None and xgb_only is not None and linear_only is not None

    moved = False
    for method_class in METHOD_CLASSES:
        low = min(xgb_only["probabilities"][method_class], linear_only["probabilities"][method_class])
        high = max(xgb_only["probabilities"][method_class], linear_only["probabilities"][method_class])
        assert low - 1e-9 <= blended["probabilities"][method_class] <= high + 1e-9
        if abs(blended["probabilities"][method_class] - xgb_only["probabilities"][method_class]) > 1e-6:
            moved = True
    assert moved, "the linear half was ignored: the blend equals the XGBoost alone"


def test_bundle_without_linear_half_degrades_to_xgboost(ensemble_bundle, method_bundle):
    """Rolling back to a pre-ensemble bundle must keep serving, unchanged."""
    without_linear = {
        key: value
        for key, value in ensemble_bundle.items()
        if key not in {"method_model_linear", "method_calibrator_linear"}
    }
    row = _method_row(red_seed=2.5, blue_seed=1.5)
    degraded = _predict_method(without_linear, row)
    reference = _predict_method(method_bundle, row)

    assert degraded is not None and reference is not None
    for method_class in METHOD_CLASSES:
        assert degraded["probabilities"][method_class] == pytest.approx(
            reference["probabilities"][method_class], abs=1e-12
        )
