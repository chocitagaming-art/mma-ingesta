"""NanPassthrough: the winner model's "imputer" for --nan-policy native.

Why it exists. Phase 4 brings debutants into the winner model, and the pre-UFC block
is NaN for a fighter without any history. The median SimpleImputer would turn that
"unknown" into "a median veteran"; the phase-3 experiment fed XGBoost the NaN as is.
NanPassthrough keeps the imputer's slot in the bundle and its interface (calibrate,
evaluate and api all call ``bundle["imputer"].transform``) while imputing nothing.

It has to behave like SimpleImputer on everything except the filling: same input
checks, same feature names, same column order, and it must survive a joblib round
trip from its home inside src/prediction (Render loads the bundle from there).

Pure: synthetic frames, no database, no model file.
"""

from __future__ import annotations

import warnings

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.impute import SimpleImputer

from src.prediction.preprocessing import (
    DEFAULT_NAN_POLICY,
    NAN_POLICIES,
    NanPassthrough,
    make_imputer,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "age_diff": [1.5, np.nan, -2.0, 0.25],
            "espn_win_rate_red": [0.8, 0.5, np.nan, np.nan],
            "espn_win_rate_blue": [np.nan, 0.75, 0.6, 0.1],
        }
    )


def test_fit_records_feature_names_and_count_like_simple_imputer():
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)
    imputer = SimpleImputer(strategy="median").fit(frame)

    assert passthrough.n_features_in_ == imputer.n_features_in_ == 3
    assert isinstance(passthrough.feature_names_in_, np.ndarray)
    np.testing.assert_array_equal(
        passthrough.feature_names_in_, imputer.feature_names_in_
    )


def test_fit_on_an_array_records_no_feature_names_like_simple_imputer():
    values = _frame().to_numpy()
    passthrough = NanPassthrough().fit(values)

    assert passthrough.n_features_in_ == 3
    assert not hasattr(passthrough, "feature_names_in_")
    assert not hasattr(SimpleImputer().fit(values), "feature_names_in_")


def test_transform_keeps_every_value_and_every_nan_in_place():
    frame = _frame()
    out = NanPassthrough().fit(frame).transform(frame)

    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out.shape == frame.shape
    # assert_array_equal treats NaN in the same position as equal.
    np.testing.assert_array_equal(out, frame.to_numpy(dtype=float))
    assert np.isnan(out).sum() == frame.isna().sum().sum() == 4


def test_none_becomes_nan_as_in_the_served_feature_row():
    """api.py builds a one-row DataFrame from a dict whose missing values are None
    (object dtype). The output must be float with NaN there."""
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)
    row = pd.DataFrame(
        [{"age_diff": None, "espn_win_rate_red": 0.9, "espn_win_rate_blue": None}]
    )
    assert row["age_diff"].dtype == object

    out = passthrough.transform(row)

    assert out.dtype == np.float64
    assert np.isnan(out[0, 0]) and np.isnan(out[0, 2])
    assert out[0, 1] == 0.9


def test_an_all_nan_column_is_kept_not_dropped():
    """SimpleImputer(median) silently DROPS a column without observed values, which
    would shift every later column; NanPassthrough always returns n_features_in_
    columns."""
    frame = _frame().assign(espn_win_rate_red=np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        dropped = SimpleImputer(strategy="median").fit(frame).transform(frame)
    assert dropped.shape[1] == 2

    out = NanPassthrough().fit(frame).transform(frame)

    assert out.shape == (4, 3)
    assert np.isnan(out[:, 1]).all()


def test_column_order_is_the_fitted_one_and_reordering_fails_like_simple_imputer():
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)
    imputer = SimpleImputer(strategy="median").fit(frame)
    reordered = frame[["espn_win_rate_blue", "age_diff", "espn_win_rate_red"]]

    with pytest.raises(ValueError, match="same order"):
        imputer.transform(reordered)
    with pytest.raises(ValueError, match="same order"):
        passthrough.transform(reordered)
    # Selecting the fitted columns by name (what every caller does) restores it.
    np.testing.assert_array_equal(
        passthrough.transform(reordered[list(frame.columns)]),
        frame.to_numpy(dtype=float),
    )


def test_missing_column_or_wrong_width_fails_like_simple_imputer():
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)

    with pytest.raises(ValueError, match="missing"):
        passthrough.transform(frame[["age_diff", "espn_win_rate_red"]])
    # An array also triggers the "no valid feature names" warning, tested below.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with pytest.raises(ValueError, match="features"):
            passthrough.transform(frame.to_numpy()[:, :2])


def test_an_array_after_fitting_on_names_warns_like_simple_imputer():
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)

    with pytest.warns(UserWarning, match="valid feature names"):
        out = passthrough.transform(frame.to_numpy())
    np.testing.assert_array_equal(out, frame.to_numpy(dtype=float))


def test_infinity_is_rejected_like_simple_imputer():
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)
    with pytest.raises(ValueError, match="infinity"):
        passthrough.transform(frame.assign(age_diff=[np.inf, 1.0, 2.0, 3.0]))


def test_transform_before_fit_fails():
    from sklearn.exceptions import NotFittedError

    with pytest.raises(NotFittedError):
        NanPassthrough().transform(_frame())


def test_the_output_is_a_copy_not_a_view_of_the_input():
    frame = _frame()
    out = NanPassthrough().fit(frame).transform(frame)
    out[0, 0] = 999.0
    assert frame.iloc[0, 0] == 1.5


def test_joblib_round_trip_keeps_names_and_output(tmp_path):
    frame = _frame()
    passthrough = NanPassthrough().fit(frame)
    path = tmp_path / "passthrough.joblib"
    joblib.dump(passthrough, path)

    loaded = joblib.load(path)

    # Pickled by reference to its module: Render imports it from src/prediction.
    assert type(loaded).__module__ == "src.prediction.preprocessing"
    np.testing.assert_array_equal(
        loaded.feature_names_in_, passthrough.feature_names_in_
    )
    assert loaded.n_features_in_ == 3
    np.testing.assert_array_equal(loaded.transform(frame), passthrough.transform(frame))


def test_nan_policies_and_their_imputers():
    assert NAN_POLICIES == ("median", "native")
    assert DEFAULT_NAN_POLICY == "median"
    median = make_imputer("median")
    assert isinstance(median, SimpleImputer) and median.strategy == "median"
    assert isinstance(make_imputer("native"), NanPassthrough)
    with pytest.raises(ValueError, match="nan policy"):
        make_imputer("mean")
