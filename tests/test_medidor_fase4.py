"""The phase-4 measuring tools: train / calibrate / evaluate as an instrument.

Why it exists. The pre-registered phase-4 measurement trains several arms (feature
sets) x seeds, calibrates and evaluates each one, and must never write the bundle
production serves. So the three scripts take other paths (--dataset, --bundle),
train.py takes the final-fit seed, the feature set, fixed hyperparameters and the
NaN policy, and --no-test-report keeps every test-period metric out of a run that
only has to produce a model. The bundle records how its winner model was trained.

The other half of the contract: WITHOUT options everything is exactly as before,
and today's bundle (27-jun, no phase-4 keys) reads as legacy columns + median.

Synthetic data and temporary bundles only: no database, no real CSV, no real
metric. src/prediction/model.joblib is only read (and its hash checked).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import shutil
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
import xgboost
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import ParameterGrid
from xgboost import XGBClassifier

from src.prediction import bundle_io, calibrate, evaluate, train
from src.prediction.bundle_io import stale_calibrators
from src.prediction.features.types import (
    CORNER_PAIR_BASES,
    FEATURE_COLUMNS,
    FEATURE_SETS,
    UFC_COUNT_COLUMNS,
    WINNER_FEATURE_COLUMNS,
)
from src.prediction.split import CAL_START, TEST_END, TEST_START

COMMITTED_BUNDLE = (
    Path(__file__).resolve().parents[1] / "src" / "prediction" / "model.joblib"
)
PHASE4_BUNDLE_KEYS = ("feature_set", "nan_policy", "xgb_params", "train_seed")
TRAIN_LOGGER = "src.prediction.train"
RECALIBRATE = "python -m src.prediction.calibrate"

# Small, and with row/column subsampling so the seed matters; a fit takes ms.
FAST_PARAMS = {
    "n_estimators": 20,
    "max_depth": 2,
    "learning_rate": 0.1,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
}


# --- Synthetic data -------------------------------------------------------------------


def _synthetic_dataset(
    seed: int = 3, nan_share: float = 0.0, columns=WINNER_FEATURE_COLUMNS
) -> pd.DataFrame:
    # One fight every 2 days up to the frozen metro's TEST_END: every partition
    # gets rows and the test window clears MIN_TEST_ROWS (~540 rows).
    dates = pd.date_range(end=TEST_END, periods=1_600, freq="2D")
    n_rows = len(dates)
    rng = np.random.default_rng(seed)
    data = pd.DataFrame(rng.normal(size=(n_rows, len(columns))), columns=list(columns))
    for column in UFC_COUNT_COLUMNS:
        if column in data:
            data[column] = rng.integers(0, 12, size=n_rows)
    signal = data["height_cm_diff"] + 0.5 * data["age_diff"]
    data["target"] = (signal + rng.normal(scale=1.0, size=n_rows) > 0).astype(int)
    if nan_share:
        # The UFC count is never NaN (a debutant is an explicit 0).
        holed = [c for c in columns if c not in UFC_COUNT_COLUMNS]
        mask = rng.random(size=(n_rows, len(holed))) < nan_share
        data[holed] = data[holed].mask(mask)
    data["event_date"] = dates
    data["fight_id"] = np.arange(n_rows)
    return data


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_csv(path, index=False)
    return path


@pytest.fixture
def csv_full(tmp_path) -> Path:
    return _write_csv(_synthetic_dataset(), tmp_path / "dataset.csv")


@pytest.fixture
def csv_with_nan(tmp_path) -> Path:
    return _write_csv(_synthetic_dataset(nan_share=0.3), tmp_path / "dataset_nan.csv")


@pytest.fixture(autouse=True)
def _no_side_effects(tmp_path, monkeypatch):
    """No test here may write the repo's model_metrics.md or reach the database."""
    monkeypatch.setattr(train, "METRICS_PATH", tmp_path / "model_metrics.md")
    monkeypatch.setattr(evaluate, "METRICS_PATH", tmp_path / "evaluate_metrics.md")
    monkeypatch.setenv("DATABASE_URL", "")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _train(dataset: Path, bundle: Path, *options: str, params=FAST_PARAMS) -> dict:
    argv = ["--dataset", str(dataset), "--bundle", str(bundle), "--no-test-report"]
    if params is not None:
        argv += ["--params", json.dumps(params)]
    train.main([*argv, *options])
    return joblib.load(bundle)


def _calibrate(dataset: Path, bundle: Path, *options: str) -> dict:
    calibrate.main(["--dataset", str(dataset), "--bundle", str(bundle), *options])
    return joblib.load(bundle)


def _test_rows(dataset: Path, columns) -> pd.DataFrame:
    frame = train.load_dataset(dataset, columns)
    return train.chronological_three_way_split(frame)[2]


def _predict(bundle: dict, frame: pd.DataFrame) -> np.ndarray:
    x = bundle["imputer"].transform(frame[bundle["feature_columns"]])
    return bundle["model"].predict_proba(x)[:, 1]


def _forbidden(*_args, **_kwargs):
    raise AssertionError("a test-period metric was computed")


# --- 1. Without options, today --------------------------------------------------------


def test_train_default_options_are_todays_paths_seed_columns_and_imputer():
    args = train.parse_args([])

    assert args.dataset == Path("training_dataset.csv")
    assert args.bundle == Path("src/prediction/model.joblib")
    assert args.seed == 42
    assert args.feature_set == "legacy"
    assert args.params is None
    assert args.nan_policy == "median"
    assert args.no_test_report is False
    # The grid search and its folds keep their own fixed seed, whatever --seed says.
    assert train.GRID_SEED == 42


def test_calibrate_and_evaluate_default_options_are_todays():
    cal = calibrate.parse_args([])
    assert cal.dataset == Path("training_dataset.csv")
    assert cal.bundle == Path("src/prediction/model.joblib")
    assert cal.calibration_method == "auto"
    assert cal.no_test_report is False

    ev = evaluate.parse_args([])
    assert ev.dataset == Path("training_dataset.csv")
    assert ev.bundle == Path("src/prediction/model.joblib")
    assert ev.no_write is False


def _todays_grid_choice(train_df: pd.DataFrame, grid: dict) -> dict:
    """train.cross_validate_params as it was before phase 4, written out by hand:
    chronological folds, median imputer per fold, seed 42, best mean AUC (the first
    one wins a tie)."""
    folds = train.build_time_series_folds(train_df)
    best_score, best_params = float("-inf"), None
    for params in ParameterGrid(grid):
        scores = []
        for fold in folds:
            fold_train = train_df.iloc[fold.train_idx]
            fold_val = train_df.iloc[fold.val_idx]
            imputer = SimpleImputer(strategy="median")
            x_train = imputer.fit_transform(fold_train[FEATURE_COLUMNS])
            model = XGBClassifier(
                objective="binary:logistic",
                eval_metric="logloss",
                random_state=42,
                **params,
            )
            model.fit(x_train, fold_train["target"])
            probabilities = model.predict_proba(
                imputer.transform(fold_val[FEATURE_COLUMNS])
            )[:, 1]
            scores.append(roc_auc_score(fold_val["target"], probabilities))
        if np.mean(scores) > best_score:
            best_score, best_params = float(np.mean(scores)), params
    return best_params


def test_without_options_train_builds_the_same_model_as_before(tmp_path, monkeypatch):
    """train.main([]) reads the module paths, runs the grid, imputes with the
    median and fits with seed 42 on the 20 legacy diffs: prediction for
    prediction, the model built the pre-phase-4 way."""
    csv = _write_csv(
        _synthetic_dataset(nan_share=0.1, columns=FEATURE_COLUMNS),
        tmp_path / "training_dataset.csv",
    )
    bundle_path = tmp_path / "model.joblib"
    shutil.copyfile(COMMITTED_BUNDLE, bundle_path)
    monkeypatch.setattr(train, "DATASET_PATH", csv)
    monkeypatch.setattr(train, "MODEL_PATH", bundle_path)
    small_grid = {
        "n_estimators": [10, 20],
        "max_depth": [2],
        "learning_rate": [0.1],
        "subsample": [0.8],
        "colsample_bytree": [0.8, 1.0],
    }
    monkeypatch.setattr(train, "PARAMETER_GRID", small_grid)

    train.main([])

    bundle = joblib.load(bundle_path)
    frame = train.load_dataset(csv)
    train_df, _calibration_df, test_df = train.chronological_three_way_split(frame)
    expected_params = _todays_grid_choice(train_df, small_grid)
    imputer = SimpleImputer(strategy="median").fit(train_df[FEATURE_COLUMNS])
    expected = XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        **expected_params,
    ).fit(imputer.transform(train_df[FEATURE_COLUMNS]), train_df["target"])

    assert bundle["feature_columns"] == FEATURE_COLUMNS
    assert isinstance(bundle["imputer"], SimpleImputer)
    assert bundle["imputer"].strategy == "median"
    np.testing.assert_array_equal(bundle["imputer"].statistics_, imputer.statistics_)
    assert bundle["xgb_params"] == expected_params
    assert bundle["model"].get_params()["random_state"] == 42
    np.testing.assert_array_equal(
        _predict(bundle, test_df),
        expected.predict_proba(imputer.transform(test_df[FEATURE_COLUMNS]))[:, 1],
    )
    assert bundle_io.winner_training_config(bundle) == {
        "feature_set": "legacy",
        "nan_policy": "median",
        "xgb_params": expected_params,
        "train_seed": 42,
    }


def test_todays_bundle_reads_as_legacy_median_and_is_not_modified():
    before = _sha256(COMMITTED_BUNDLE)
    bundle = joblib.load(COMMITTED_BUNDLE)

    for key in PHASE4_BUNDLE_KEYS:
        assert key not in bundle, key
    assert bundle_io.winner_training_config(bundle) == {
        "feature_set": "legacy",
        "nan_policy": "median",
        "xgb_params": None,
        "train_seed": None,
    }
    assert bundle["feature_columns"] == FEATURE_SETS["legacy"]
    assert isinstance(bundle["imputer"], SimpleImputer)
    assert _sha256(COMMITTED_BUNDLE) == before


def test_todays_bundle_still_calibrates_and_evaluates_through_the_new_options(
    tmp_path,
):
    csv = _write_csv(
        _synthetic_dataset(nan_share=0.1, columns=FEATURE_COLUMNS), tmp_path / "d.csv"
    )
    before = _sha256(COMMITTED_BUNDLE)
    copy_path = tmp_path / "copy.joblib"
    shutil.copyfile(COMMITTED_BUNDLE, copy_path)
    committed = joblib.load(COMMITTED_BUNDLE)

    test_df, variants, headline = evaluate.build_test_predictions(
        dataset_path=csv, bundle_path=copy_path
    )

    assert headline == "symmetrized_calibrated"
    # The pre-phase-4 computation: median imputer + calibrator of the bundle.
    x_test = committed["imputer"].transform(test_df[FEATURE_COLUMNS])
    np.testing.assert_array_equal(
        variants["raw_calibrated"], committed["calibrator"].predict_proba(x_test)[:, 1]
    )

    recalibrated = _calibrate(csv, copy_path, "--no-test-report")
    assert stale_calibrators(recalibrated) == []
    # calibrate never retrains: same model, same method_* keys.
    for key in ("model", "imputer", "method_model", "method_model_linear"):
        assert joblib.hash(recalibrated[key]) == joblib.hash(committed[key]), key
    assert _sha256(COMMITTED_BUNDLE) == before


# --- 2. Other paths: the committed bundle is never written ----------------------------


def test_train_into_a_temp_bundle_leaves_the_committed_one_untouched(
    tmp_path, csv_full
):
    before = _sha256(COMMITTED_BUNDLE)
    original = joblib.load(COMMITTED_BUNDLE)
    bundle_path = tmp_path / "arm.joblib"
    shutil.copyfile(COMMITTED_BUNDLE, bundle_path)

    trained = _train(csv_full, bundle_path, "--seed", "7")

    assert _sha256(COMMITTED_BUNDLE) == before
    method_keys = [key for key in original if key.startswith("method_")]
    assert len(method_keys) == 12
    for key in method_keys:
        assert joblib.hash(trained[key]) == joblib.hash(original[key]), key
    # The method calibrators still wrap their models; the winner's old one is gone.
    assert stale_calibrators(trained) == []
    assert "calibrator" not in trained
    assert trained["feature_set"] == "legacy"
    assert trained["nan_policy"] == "median"
    assert trained["xgb_params"] == FAST_PARAMS
    assert trained["train_seed"] == 7


# --- 3. NaN policy --------------------------------------------------------------------


def test_native_nan_reaches_xgboost_and_calibrate_and_evaluate_accept_it(
    tmp_path, csv_with_nan, capsys
):
    from src.prediction.preprocessing import NanPassthrough

    bundle_path = tmp_path / "native.joblib"
    trained = _train(csv_with_nan, bundle_path, "--nan-policy", "native")

    assert isinstance(trained["imputer"], NanPassthrough)
    assert trained["nan_policy"] == "native"
    test_df = _test_rows(csv_with_nan, FEATURE_COLUMNS)
    x_test = trained["imputer"].transform(test_df[trained["feature_columns"]])
    assert np.isnan(x_test).any()  # nothing was filled

    calibrated = _calibrate(csv_with_nan, bundle_path, "--no-test-report")
    assert isinstance(calibrated["imputer"], NanPassthrough)
    assert calibrated["calibration_method"] in calibrate.CANDIDATE_METHODS
    assert stale_calibrators(calibrated) == []

    test_df, variants, headline = evaluate.build_test_predictions(
        dataset_path=csv_with_nan, bundle_path=bundle_path
    )
    assert headline == "symmetrized_calibrated"
    for name, probabilities in variants.items():
        assert np.isfinite(probabilities).all(), name

    evaluate.main(
        ["--dataset", str(csv_with_nan), "--bundle", str(bundle_path), "--no-write"]
    )
    assert "Diagnostic evaluation" in capsys.readouterr().out
    assert not evaluate.METRICS_PATH.exists()


def test_the_nan_policy_reaches_the_grid_folds_and_the_final_fit(
    tmp_path, csv_with_nan, monkeypatch
):
    policies: list[str] = []
    real_make_imputer = train.make_imputer
    monkeypatch.setattr(
        train,
        "make_imputer",
        lambda policy: policies.append(policy) or real_make_imputer(policy),
    )
    monkeypatch.setattr(
        train,
        "PARAMETER_GRID",
        {
            "n_estimators": [10],
            "max_depth": [2],
            "learning_rate": [0.1],
            "subsample": [0.8],
            "colsample_bytree": [0.8],
        },
    )

    _train(csv_with_nan, tmp_path / "n.joblib", "--nan-policy", "native", params=None)

    # 3 chronological folds + the final fit, all without imputing.
    assert policies == ["native"] * 4


def test_prepare_features_native_keeps_nan_and_median_fills_it():
    frame = _synthetic_dataset(nan_share=0.3, columns=FEATURE_COLUMNS)
    train_df, test_df = frame.iloc[:800], frame.iloc[800:]

    native = train.prepare_features(
        train_df, test_df, FEATURE_COLUMNS, nan_policy="native"
    )
    median = train.prepare_features(train_df, test_df, FEATURE_COLUMNS)

    np.testing.assert_array_equal(native.x_train, train_df[FEATURE_COLUMNS].to_numpy())
    assert np.isnan(native.x_test).any()
    assert not np.isnan(median.x_train).any()
    assert isinstance(median.imputer, SimpleImputer)


# --- 4. Seeds -------------------------------------------------------------------------


def test_with_subsample_below_one_the_seed_changes_the_model(tmp_path, csv_full):
    test_df = _test_rows(csv_full, FEATURE_COLUMNS)

    seed_0 = _train(csv_full, tmp_path / "s0.joblib", "--seed", "0")
    seed_0_again = _train(csv_full, tmp_path / "s0b.joblib", "--seed", "0")
    seed_1 = _train(csv_full, tmp_path / "s1.joblib", "--seed", "1")

    np.testing.assert_array_equal(
        _predict(seed_0, test_df), _predict(seed_0_again, test_df)
    )
    assert not np.allclose(_predict(seed_0, test_df), _predict(seed_1, test_df))
    assert (seed_0["train_seed"], seed_1["train_seed"]) == (0, 1)


def _one_point_grid(subsample: float, colsample: float) -> dict:
    return {
        "n_estimators": [20],
        "max_depth": [2],
        "learning_rate": [0.1],
        "subsample": [subsample],
        "colsample_bytree": [colsample],
    }


def test_full_subsample_and_colsample_make_the_seed_inert_and_it_is_logged(
    tmp_path, csv_full, monkeypatch, caplog
):
    monkeypatch.setattr(train, "PARAMETER_GRID", _one_point_grid(1.0, 1.0))
    test_df = _test_rows(csv_full, FEATURE_COLUMNS)

    with caplog.at_level(logging.WARNING, logger=TRAIN_LOGGER):
        seed_0 = _train(csv_full, tmp_path / "s0.joblib", "--seed", "0", params=None)
    seed_1 = _train(csv_full, tmp_path / "s1.joblib", "--seed", "1", params=None)

    # What the warning claims: every seed gives the same model.
    np.testing.assert_array_equal(_predict(seed_0, test_df), _predict(seed_1, test_df))
    warnings = [r.getMessage() for r in caplog.records if r.name == TRAIN_LOGGER]
    assert any(
        "subsample" in w and "colsample_bytree" in w and "seed" in w for w in warnings
    )


def test_no_inert_seed_warning_when_the_grid_subsamples(
    tmp_path, csv_full, monkeypatch, caplog
):
    monkeypatch.setattr(train, "PARAMETER_GRID", _one_point_grid(0.8, 1.0))
    with caplog.at_level(logging.WARNING, logger=TRAIN_LOGGER):
        _train(csv_full, tmp_path / "s.joblib", params=None)
    assert not [r for r in caplog.records if "subsample" in r.getMessage()]


# --- 5. Feature sets ------------------------------------------------------------------


@pytest.mark.parametrize("feature_set", sorted(FEATURE_SETS))
def test_feature_set_picks_its_columns_and_is_saved(tmp_path, csv_full, feature_set):
    trained = _train(
        csv_full, tmp_path / f"{feature_set}.joblib", "--feature-set", feature_set
    )

    columns = FEATURE_SETS[feature_set]
    assert trained["feature_columns"] == columns
    assert trained["feature_set"] == feature_set
    assert trained["model"].n_features_in_ == len(columns)
    np.testing.assert_array_equal(trained["imputer"].feature_names_in_, columns)


def test_a_feature_set_fails_clearly_when_the_csv_lacks_its_columns(tmp_path):
    legacy_only = _write_csv(
        _synthetic_dataset(columns=FEATURE_COLUMNS), tmp_path / "legacy.csv"
    )
    bundle_path = tmp_path / "never.joblib"

    with pytest.raises(RuntimeError, match="espn_has_history_red"):
        _train(legacy_only, bundle_path, "--feature-set", "preufc")
    assert not bundle_path.exists()


def test_an_unknown_feature_set_is_rejected():
    with pytest.raises(SystemExit):
        train.parse_args(["--feature-set", "everything"])


def test_load_dataset_requires_the_chosen_columns_and_defaults_to_the_legacy_ones(
    tmp_path,
):
    legacy_only = _write_csv(
        _synthetic_dataset(columns=FEATURE_COLUMNS), tmp_path / "legacy.csv"
    )

    assert len(train.load_dataset(legacy_only)) == 1_600
    assert len(train.load_dataset(legacy_only, FEATURE_COLUMNS)) == 1_600
    with pytest.raises(RuntimeError, match="ufc_prev_fights_red"):
        train.load_dataset(legacy_only, FEATURE_SETS["base"])


# --- 6. Available columns: whole corner pairs -----------------------------------------


def test_available_columns_drop_an_all_nan_column_and_whole_corner_pairs(caplog):
    columns = FEATURE_SETS["preufc"]
    frame = _synthetic_dataset()
    frame["ranking_position_diff"] = np.nan  # a lone diff: dropped alone, as always
    frame["espn_win_rate_red"] = np.nan  # one side of a pair: the pair goes
    frame["espn_streak_red"] = np.nan  # both sides empty
    frame["espn_streak_blue"] = np.nan

    with caplog.at_level(logging.WARNING, logger=TRAIN_LOGGER):
        available = train.get_available_feature_columns(frame, columns)

    dropped = {
        "ranking_position_diff",
        "espn_win_rate_red",
        "espn_win_rate_blue",
        "espn_streak_red",
        "espn_streak_blue",
    }
    assert available == [column for column in columns if column not in dropped]
    # Both the empty columns and the partner dropped with its pair are named.
    for column in ("ranking_position_diff", "espn_win_rate_red", "espn_win_rate_blue"):
        assert column in caplog.text


def test_available_columns_never_leave_half_a_pair():
    columns = FEATURE_SETS["preufc"]
    rng = np.random.default_rng(11)
    for _ in range(20):
        frame = _synthetic_dataset()
        for column in rng.choice(columns, size=6, replace=False):
            frame[column] = np.nan
        available = set(train.get_available_feature_columns(frame, columns))
        for base in CORNER_PAIR_BASES:
            assert (f"{base}_red" in available) == (f"{base}_blue" in available), base


def test_available_columns_default_to_the_legacy_diffs_and_stay_quiet(caplog):
    frame = _synthetic_dataset(columns=FEATURE_COLUMNS)
    with caplog.at_level(logging.WARNING, logger=TRAIN_LOGGER):
        assert train.get_available_feature_columns(frame) == FEATURE_COLUMNS
    assert [r for r in caplog.records if r.name == TRAIN_LOGGER] == []


# --- 7. --params skips the grid -------------------------------------------------------


def test_params_skip_the_grid_and_are_the_ones_used(tmp_path, csv_full, monkeypatch):
    def no_grid(*_args, **_kwargs):
        raise AssertionError("the grid search ran despite --params")

    monkeypatch.setattr(train, "cross_validate_params", no_grid)
    params = {
        "n_estimators": 15,
        "max_depth": 3,
        "learning_rate": 0.05,
        "subsample": 0.9,
        "colsample_bytree": 0.7,
    }

    trained = _train(csv_full, tmp_path / "p.joblib", params=params)

    assert trained["xgb_params"] == params
    model_params = trained["model"].get_params()
    for key, value in params.items():
        assert model_params[key] == value, key


def test_without_params_the_grid_choice_is_saved(tmp_path, csv_full, monkeypatch):
    chosen = dict(FAST_PARAMS, n_estimators=10)
    calls: list[dict] = []
    monkeypatch.setattr(
        train,
        "cross_validate_params",
        lambda *_args, **kwargs: calls.append(kwargs) or chosen,
    )

    trained = _train(csv_full, tmp_path / "g.joblib", params=None)

    assert len(calls) == 1
    assert trained["xgb_params"] == chosen


@pytest.mark.parametrize(
    "bad",
    ["not json", "[1, 2]", '{"random_state": 3}', '{"objective": "reg:squarederror"}'],
)
def test_bad_params_are_rejected(bad):
    with pytest.raises(SystemExit):
        train.parse_args(["--params", bad])


# --- 8. --no-test-report --------------------------------------------------------------


def test_no_test_report_computes_prints_and_writes_no_test_metric(
    tmp_path, csv_full, monkeypatch, capsys
):
    for name in (
        "evaluate_predictions",
        "majority_class_baseline",
        "confusion_matrix",
        "classification_report",
        "write_metrics_report",
    ):
        monkeypatch.setattr(train, name, _forbidden)

    _train(csv_full, tmp_path / "q.joblib")  # _train passes --no-test-report

    out = capsys.readouterr().out
    # Metric dict keys as train.py prints them (feature names such as
    # sig_strike_accuracy_diff do contain the bare word).
    for marker in (
        "Model metrics",
        "Majority-class baseline",
        "Confusion matrix",
        "Classification report",
        "'roc_auc'",
        "'accuracy'",
        "precision",
    ):
        assert marker not in out, marker
    assert not train.METRICS_PATH.exists()
    assert "Feature importance:" in out  # train-only, still reported
    assert out.rindex(RECALIBRATE) > out.rindex("Feature importance:")


def test_without_no_test_report_train_still_reports_the_test_metrics(
    tmp_path, csv_full, capsys
):
    train.main(
        [
            "--dataset",
            str(csv_full),
            "--bundle",
            str(tmp_path / "r.joblib"),
            "--params",
            json.dumps(FAST_PARAMS),
        ]
    )
    out = capsys.readouterr().out
    assert "Model metrics" in out
    assert train.METRICS_PATH.exists()


def test_calibrate_no_test_report_skips_the_test_diagnostics(
    tmp_path, csv_full, monkeypatch, capsys
):
    bundle_path = tmp_path / "c.joblib"
    _train(csv_full, bundle_path)
    capsys.readouterr()
    # Only the test-slice diagnostics use these two.
    monkeypatch.setattr(calibrate, "log_loss", _forbidden)
    monkeypatch.setattr(calibrate, "accuracy_score", _forbidden)

    calibrated = _calibrate(csv_full, bundle_path, "--no-test-report")

    assert stale_calibrators(calibrated) == []
    assert "Test-slice" not in capsys.readouterr().out


def test_calibrate_without_the_flag_still_prints_the_test_diagnostics(
    tmp_path, csv_full, capsys
):
    bundle_path = tmp_path / "c.joblib"
    _train(csv_full, bundle_path)
    _calibrate(csv_full, bundle_path)
    assert "Test-slice diagnostics" in capsys.readouterr().out


# --- 9. Calibration method ------------------------------------------------------------


@pytest.mark.parametrize("method", ["sigmoid", "isotonic"])
def test_the_calibration_method_can_be_fixed(tmp_path, csv_full, method):
    bundle_path = tmp_path / "m.joblib"
    _train(csv_full, bundle_path)

    calibrated = _calibrate(
        csv_full, bundle_path, "--calibration-method", method, "--no-test-report"
    )

    assert calibrated["calibration_method"] == method
    assert calibrated["calibrator"].method == method
    assert stale_calibrators(calibrated) == []


def test_auto_calibration_compares_both_methods_like_today(tmp_path, csv_full, capsys):
    bundle_path = tmp_path / "a.joblib"
    _train(csv_full, bundle_path)
    capsys.readouterr()

    calibrated = _calibrate(csv_full, bundle_path, "--no-test-report")

    out = capsys.readouterr().out
    for method in ("isotonic", "sigmoid"):
        assert f"{method:<9} brier=" in out
    assert calibrated["calibration_method"] in ("isotonic", "sigmoid")


def test_an_unknown_calibration_method_is_rejected():
    with pytest.raises(SystemExit):
        calibrate.parse_args(["--calibration-method", "beta"])


# --- 10. calibrate and evaluate use the bundle's columns ------------------------------


def test_calibrate_and_evaluate_use_the_bundles_columns(tmp_path, capsys):
    full = _synthetic_dataset(nan_share=0.2)
    csv = _write_csv(full, tmp_path / "full.csv")
    bundle_path = tmp_path / "diff.joblib"
    _train(csv, bundle_path, "--feature-set", "preufc_diff", "--nan-policy", "native")
    _calibrate(csv, bundle_path, "--no-test-report")
    assert "differ" not in capsys.readouterr().out  # no drift warning

    # A CSV with only the columns of the bundle's set is enough...
    keep = FEATURE_SETS["preufc_diff"] + ["target", "event_date", "fight_id"]
    trimmed = _write_csv(full[keep], tmp_path / "trimmed.csv")
    test_df, variants, _headline = evaluate.build_test_predictions(
        dataset_path=trimmed, bundle_path=bundle_path
    )
    assert len(test_df) > 0
    assert all(np.isfinite(p).all() for p in variants.values())
    assert "differ" not in capsys.readouterr().out

    # ...and one without a column the bundle uses fails clearly, in both scripts.
    lacking = _write_csv(full.drop(columns=["espn_win_rate_diff"]), tmp_path / "l.csv")
    with pytest.raises(RuntimeError, match="espn_win_rate_diff"):
        evaluate.build_test_predictions(dataset_path=lacking, bundle_path=bundle_path)
    with pytest.raises(RuntimeError, match="espn_win_rate_diff"):
        _calibrate(lacking, bundle_path)


def test_a_bundle_with_an_unknown_feature_set_fails_clearly(tmp_path, csv_full):
    bundle_path = tmp_path / "u.joblib"
    trained = _train(csv_full, bundle_path)
    joblib.dump({**trained, "feature_set": "from_the_future"}, bundle_path)

    with pytest.raises(RuntimeError, match="from_the_future"):
        evaluate.build_test_predictions(dataset_path=csv_full, bundle_path=bundle_path)


# --- 11. --params is checked against XGBoost, never silently ignored ------------------


@pytest.mark.parametrize(
    ("bad", "named"),
    [
        # XGBoost's alias of random_state: it would override --seed and the bundle
        # would record a train_seed that was not used. Refused as reserved, with
        # the way out, not merely as an unknown key.
        ('{"seed": 5}', "the seed goes in --seed"),
        # A typo only gets an XGBoost warning: the arm trains with the default
        # (max_depth 6) and the bundle keeps the typo as if it had been used.
        ('{"n_estimators": 20, "max_dpeth": 2}', "max_dpeth"),
        ('{"subsample": null}', "subsample"),
    ],
)
def test_params_reject_seed_unknown_keys_and_nulls(bad, named, capsys):
    with pytest.raises(SystemExit):
        train.parse_args(["--params", bad])
    assert named in capsys.readouterr().err


def test_params_accept_any_key_xgboost_knows():
    params = {"gamma": 0.1, "min_child_weight": 2, "reg_lambda": 1.5, "max_depth": 3}
    assert set(params) <= set(XGBClassifier().get_params())

    assert train.parse_args(["--params", json.dumps(params)]).params == params


# --- 12. The served bundle only takes a phase-4 set on purpose ------------------------


@pytest.fixture
def served_bundle(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "served" / "model.joblib"
    path.parent.mkdir()
    shutil.copyfile(COMMITTED_BUNDLE, path)
    monkeypatch.setattr(train, "MODEL_PATH", path)
    return path


def _train_argv(dataset: Path, *options: str) -> list[str]:
    return [
        "--dataset", str(dataset), "--no-test-report",
        "--params", json.dumps(FAST_PARAMS), *options,
    ]


@pytest.mark.parametrize("feature_set", ["base", "preufc", "preufc_diff"])
def test_train_refuses_a_phase4_set_into_the_served_bundle(
    served_bundle, csv_full, feature_set
):
    before = _sha256(served_bundle)
    other_spelling = served_bundle.parent / ".." / "served" / "model.joblib"

    for bundle_option in ([], ["--bundle", str(other_spelling)]):
        with pytest.raises(RuntimeError, match="--write-production-bundle"):
            train.main(
                _train_argv(csv_full, "--feature-set", feature_set, *bundle_option)
            )

    assert _sha256(served_bundle) == before


def test_the_explicit_option_lets_a_phase4_set_into_the_served_bundle(
    served_bundle, csv_full
):
    train.main(
        _train_argv(csv_full, "--feature-set", "preufc", "--write-production-bundle")
    )
    assert joblib.load(served_bundle)["feature_set"] == "preufc"


def test_legacy_into_the_served_bundle_needs_no_option(served_bundle, csv_full):
    assert train.parse_args([]).write_production_bundle is False
    train.main(_train_argv(csv_full))
    assert joblib.load(served_bundle)["feature_set"] == "legacy"


# --- 13. What the review's surviving mutants changed, now pinned ----------------------


def test_the_grid_folds_use_grid_seed_whatever_seed_says(
    tmp_path, csv_full, monkeypatch
):
    seeds: list[int] = []
    real_build_model = train.build_model
    monkeypatch.setattr(
        train,
        "build_model",
        lambda params, seed: seeds.append(seed) or real_build_model(params, seed),
    )
    monkeypatch.setattr(
        train,
        "PARAMETER_GRID",
        {
            "n_estimators": [10, 20],
            "max_depth": [2],
            "learning_rate": [0.1],
            "subsample": [0.8],
            "colsample_bytree": [0.8],
        },
    )

    _train(csv_full, tmp_path / "g.joblib", "--seed", "7", params=None)

    # 2 grid points x 3 chronological folds with GRID_SEED, then the final fit.
    assert seeds == [train.GRID_SEED] * 6 + [7]


def test_evaluate_main_scores_the_bundle_it_is_given(
    tmp_path, csv_full, monkeypatch, capsys
):
    bundle_path = tmp_path / "arm.joblib"
    trained = _train(
        csv_full, bundle_path, "--feature-set", "base", "--nan-policy", "native"
    )
    loaded: list = []
    real_load = evaluate.load_model_bundle
    monkeypatch.setattr(
        evaluate,
        "load_model_bundle",
        lambda path=None: loaded.append(path) or real_load(path),
    )
    capsys.readouterr()

    evaluate.main(
        ["--dataset", str(csv_full), "--bundle", str(bundle_path), "--no-write"]
    )

    assert [Path(path) for path in loaded] == [bundle_path]
    # The served bundle has a calibrator; this arm has none, so its headline is
    # the uncalibrated variant.
    out = capsys.readouterr().out
    assert "Headline variant (production-equivalent): symmetrized, uncalibrated" in out
    test_df, variants, _headline = evaluate.build_test_predictions(
        dataset_path=csv_full, bundle_path=bundle_path
    )
    np.testing.assert_array_equal(
        variants["raw_uncalibrated"], _predict(trained, test_df)
    )


@pytest.mark.parametrize(
    "key", ["subsample", "colsample_bytree", "colsample_bylevel", "colsample_bynode"]
)
def test_any_subsampling_key_below_one_makes_the_seed_matter(key):
    no_subsampling = {
        "subsample": 1.0,
        "colsample_bytree": 1.0,
        "colsample_bylevel": 1.0,
        "colsample_bynode": 1.0,
    }
    assert train.seed_is_inert(no_subsampling)
    assert not train.seed_is_inert({**no_subsampling, key: 0.8})


def test_no_inert_seed_warning_when_only_colsample_subsamples(
    tmp_path, csv_full, monkeypatch, caplog
):
    monkeypatch.setattr(train, "PARAMETER_GRID", _one_point_grid(1.0, 0.8))
    with caplog.at_level(logging.WARNING, logger=TRAIN_LOGGER):
        _train(csv_full, tmp_path / "c.joblib", params=None)
    assert not [r for r in caplog.records if "subsample" in r.getMessage()]


def test_available_columns_are_decided_on_the_train_partition_only(tmp_path):
    frame = _synthetic_dataset(columns=FEATURE_COLUMNS)
    in_train = pd.to_datetime(frame["event_date"]) < pd.Timestamp(CAL_START)
    frame.loc[in_train, "ranking_position_diff"] = np.nan  # values only later
    assert frame["ranking_position_diff"].notna().any()
    csv = _write_csv(frame, tmp_path / "late_column.csv")

    trained = _train(csv, tmp_path / "late.joblib")

    assert "ranking_position_diff" not in trained["feature_columns"]
    assert len(trained["feature_columns"]) == len(FEATURE_COLUMNS) - 1


TEST_ROW_MARK = 1.0e6


def _csv_with_marked_test_rows(tmp_path) -> Path:
    """Every row of the frozen test window carries an impossible height_cm_diff."""
    frame = _synthetic_dataset(columns=FEATURE_COLUMNS)
    in_test = pd.to_datetime(frame["event_date"]) >= pd.Timestamp(TEST_START)
    frame.loc[in_test, "height_cm_diff"] = TEST_ROW_MARK
    return _write_csv(frame, tmp_path / "marked.csv")


@pytest.fixture
def test_rows_scored(monkeypatch) -> list[bool]:
    """One entry per prediction of ANY XGBoost booster (fold, final, frozen inside
    a calibrator), whatever the route to it: predict_proba, predict or the Booster
    itself. Every one of them ends in Booster.inplace_predict (an array) or
    Booster.predict (a DMatrix), so the spy sits there. True when the rows it
    scored include a test-window row."""
    seen: list[bool] = []
    column = FEATURE_COLUMNS.index("height_cm_diff")
    real_inplace_predict = xgboost.Booster.inplace_predict
    real_predict = xgboost.Booster.predict

    def has_a_test_row(rows) -> bool:
        rows = np.asarray(rows, dtype=float)
        return bool(np.any(rows[:, column] == TEST_ROW_MARK))

    @functools.wraps(real_inplace_predict)
    def inplace_predict_spy(self, data, *args, **kwargs):
        seen.append(has_a_test_row(data))
        return real_inplace_predict(self, data, *args, **kwargs)

    @functools.wraps(real_predict)
    def predict_spy(self, data, *args, **kwargs):
        # A DMatrix keeps its values: get_data() hands them back as CSR.
        seen.append(has_a_test_row(data.get_data().toarray()))
        return real_predict(self, data, *args, **kwargs)

    monkeypatch.setattr(xgboost.Booster, "inplace_predict", inplace_predict_spy)
    monkeypatch.setattr(xgboost.Booster, "predict", predict_spy)
    return seen


def test_the_spy_sees_every_route_to_the_booster(test_rows_scored):
    """predict_proba, predict and the Booster directly, on an array and on a
    DMatrix: each one is seen, with or without a test-window row."""
    rng = np.random.default_rng(0)
    rows = rng.normal(size=(60, len(FEATURE_COLUMNS)))
    labels = (rows[:, 1] > 0).astype(int)
    model = XGBClassifier(n_estimators=5, max_depth=2).fit(rows, labels)
    marked = rows.copy()
    marked[3, FEATURE_COLUMNS.index("height_cm_diff")] = TEST_ROW_MARK
    booster = model.get_booster()

    for scored in (rows, marked):
        test_rows_scored.clear()
        model.predict_proba(scored)
        model.predict(scored)
        booster.inplace_predict(scored)
        booster.predict(xgboost.DMatrix(scored))
        assert len(test_rows_scored) >= 4
        assert all(seen is (scored is marked) for seen in test_rows_scored)


def test_no_test_report_never_scores_a_test_row(
    tmp_path, monkeypatch, test_rows_scored
):
    csv = _csv_with_marked_test_rows(tmp_path)
    monkeypatch.setattr(train, "PARAMETER_GRID", _one_point_grid(0.8, 0.8))
    bundle_path = tmp_path / "t.joblib"

    _train(csv, bundle_path, params=None)  # grid folds + final fit
    _calibrate(csv, bundle_path, "--no-test-report")

    assert test_rows_scored  # models did predict (folds, calibration)...
    assert not any(test_rows_scored)  # ...never on a test row


def test_without_the_flag_the_spy_does_see_the_test_rows(tmp_path, test_rows_scored):
    csv = _csv_with_marked_test_rows(tmp_path)
    bundle_path = tmp_path / "t.joblib"

    train.main(
        ["--dataset", str(csv), "--bundle", str(bundle_path),
         "--params", json.dumps(FAST_PARAMS)]
    )
    assert any(test_rows_scored)
    test_rows_scored.clear()
    _calibrate(csv, bundle_path)
    assert any(test_rows_scored)
