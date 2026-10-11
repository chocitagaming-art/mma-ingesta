"""Preprocessing steps the winner model's bundle carries as its ``imputer``.

They live inside src/prediction on purpose: the bundle is unpickled by joblib,
which imports each class from the module it was defined in, and Render only ships
this package. A class defined anywhere else (a script, a notebook, docs/) would make
the served bundle unloadable.

Two NaN policies for the winner model (train.py --nan-policy):

- ``median`` (default, the 27-jun bundle): SimpleImputer(strategy="median"), so
  XGBoost never sees a NaN.
- ``native``: NanPassthrough below. XGBoost receives the NaN and routes it natively,
  as in the phase-3 experiment: a debutant without pre-UFC history stays "unknown"
  instead of becoming the training median.

The METHOD model is not concerned: train_method.py keeps its own SimpleImputer, so
its LogisticRegression never sees a NaN.
"""

from __future__ import annotations

from sklearn.base import BaseEstimator, OneToOneFeatureMixin, TransformerMixin
from sklearn.impute import SimpleImputer
from sklearn.utils.validation import FLOAT_DTYPES, check_is_fitted, validate_data

NAN_POLICIES = ("median", "native")
DEFAULT_NAN_POLICY = "median"


class NanPassthrough(OneToOneFeatureMixin, TransformerMixin, BaseEstimator):
    """A stand-in for the median SimpleImputer that imputes nothing.

    It keeps the imputer's slot in the bundle and its interface, so calibrate.py,
    evaluate.py and api.py keep calling ``bundle["imputer"].transform`` unchanged:

    - ``fit`` validates X exactly like SimpleImputer and records ``n_features_in_``
      and, for a DataFrame with string column names, ``feature_names_in_``;
    - ``transform`` returns a float ndarray (a copy) with the same columns in the
      same order and every NaN left as is; a None in an object column becomes NaN.
      SimpleImputer's checks apply unchanged: a DataFrame must bring the fitted
      columns in the fitted order, a wrong width fails, an infinite value fails.

    One deliberate difference: SimpleImputer(strategy="median") silently DROPS a
    column with no observed value, which shifts every later column. NanPassthrough
    always returns ``n_features_in_`` columns; an all-NaN one stays all-NaN.
    """

    def fit(self, X, y=None):
        validate_data(
            self, X, reset=True, dtype=FLOAT_DTYPES, ensure_all_finite="allow-nan"
        )
        return self

    def transform(self, X):
        check_is_fitted(self, "n_features_in_")
        return validate_data(
            self,
            X,
            reset=False,
            dtype=FLOAT_DTYPES,
            ensure_all_finite="allow-nan",
            copy=True,
        )

    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        tags.input_tags.allow_nan = True
        return tags


def make_imputer(nan_policy: str) -> SimpleImputer | NanPassthrough:
    """A fresh, unfitted imputer for the winner model under ``nan_policy``."""
    if nan_policy == "median":
        return SimpleImputer(strategy="median")
    if nan_policy == "native":
        return NanPassthrough()
    raise ValueError(
        f"Unknown nan policy {nan_policy!r}; expected one of {NAN_POLICIES}"
    )
