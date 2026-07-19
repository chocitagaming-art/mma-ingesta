"""Signed per-prediction attributions (item A) and the lowConfidence threshold (item B).

Both run offline: the attribution test trains a tiny synthetic XGBoost model (no
real model.joblib), and the threshold test exercises the pure helper directly.
"""

from datetime import date

import numpy as np
import pytest
import xgboost as xgb

from src.prediction.api import MIN_CONFIDENT_FIGHTS, _compute_top_features, _is_low_confidence
from src.prediction.features import FighterHistorySummary


def _summary(total_prior_fights: int) -> FighterHistorySummary:
    return FighterHistorySummary(
        total_prior_fights=total_prior_fights,
        total_rounds_fought=total_prior_fights * 2,
        sig_strikes_landed_per_fight=40.0,
        sig_strike_accuracy=0.5,
        knockdowns_per_fight=0.2,
        takedowns_landed_per_fight=1.0,
        takedown_accuracy=0.4,
        submission_attempts_per_fight=0.5,
        control_time_seconds_per_fight=100.0,
        win_streak=1,
        wins_last_5=3,
        pct_wins_by_ko=0.4,
        pct_wins_by_submission=0.2,
        pct_wins_by_decision=0.4,
        days_since_last_fight=150,
        ranking_position=5,
        sig_strikes_absorbed_per_fight=30.0,
        sig_strike_defense=0.6,
        takedowns_absorbed_per_fight=0.7,
        takedown_defense=0.7,
        avg_opponent_prior_win_rate=0.5,
        latest_prior_fight_date=date(2024, 1, 1),
    )


def test_low_confidence_when_either_history_is_none():
    assert _is_low_confidence(None, _summary(10)) is True
    assert _is_low_confidence(_summary(10), None) is True
    assert _is_low_confidence(None, None) is True


def test_low_confidence_when_history_is_thin():
    thin = _summary(MIN_CONFIDENT_FIGHTS - 1)
    deep = _summary(MIN_CONFIDENT_FIGHTS + 5)
    assert _is_low_confidence(thin, deep) is True
    assert _is_low_confidence(deep, thin) is True


def test_not_low_confidence_at_or_above_threshold():
    boundary = _summary(MIN_CONFIDENT_FIGHTS)
    deep = _summary(MIN_CONFIDENT_FIGHTS + 5)
    assert _is_low_confidence(boundary, deep) is False
    assert _is_low_confidence(deep, deep) is False


def _train_tiny_model(feature_columns: list[str]) -> xgb.XGBClassifier:
    rng = np.random.default_rng(0)
    samples = rng.normal(size=(300, len(feature_columns)))
    # Label driven strongly by feature 0 (positive) so its contribution is large.
    logit = 4.0 * samples[:, 0]
    labels = (logit + rng.normal(scale=0.3, size=300) > 0).astype(int)
    model = xgb.XGBClassifier(n_estimators=30, max_depth=3, random_state=0)
    # Fit on a numpy array (no column names), exactly like the production train.py,
    # so booster.feature_names is None and _compute_top_features exercises the same
    # DMatrix path it uses in production.
    model.fit(samples, labels)
    return model


def test_compute_top_features_returns_signed_contributions():
    feature_columns = [f"f{i}_diff" for i in range(6)]
    model = _train_tiny_model(feature_columns)
    transformed_row = np.array([[2.5, 0.1, -0.2, 0.0, 0.3, -0.1]])

    top, contributions = _compute_top_features(model, feature_columns, transformed_row, -transformed_row)

    assert 1 <= len(top) <= 5
    for item in top:
        assert set(item) == {"name", "value", "contribution", "direction"}
        assert item["direction"] == ("red" if item["contribution"] >= 0 else "blue")
    # Ranked by absolute contribution, descending.
    magnitudes = [abs(item["contribution"]) for item in top]
    assert magnitudes == sorted(magnitudes, reverse=True)
    # The dominant, strongly-positive feature should surface and favour red, with
    # its reported value being the (imputed) value the model actually saw.
    by_name = {item["name"]: item for item in top}
    assert "f0_diff" in by_name
    assert by_name["f0_diff"]["direction"] == "red"
    assert by_name["f0_diff"]["value"] == pytest.approx(2.5)
    # The full map covers every feature and agrees with the ranked entries.
    assert set(contributions) == set(feature_columns)
    for item in top:
        assert contributions[item["name"]] == pytest.approx(item["contribution"])


def test_attribution_mirrors_exactly_under_corner_swap():
    # predict(B, A) transforms to exactly the negated row (all features are
    # *_diff), so the symmetrized attribution must be the exact mirror: same
    # ranking by magnitude, every contribution negated, directions flipped.
    feature_columns = [f"f{i}_diff" for i in range(6)]
    model = _train_tiny_model(feature_columns)
    forward = np.array([[2.5, 0.1, -0.2, 0.0, 0.3, -0.1]])
    swapped = -forward

    top_ab, contrib_ab = _compute_top_features(model, feature_columns, forward, swapped)
    top_ba, contrib_ba = _compute_top_features(model, feature_columns, swapped, forward)

    assert [item["name"] for item in top_ab] == [item["name"] for item in top_ba]
    for ab, ba in zip(top_ab, top_ba):
        assert ba["contribution"] == pytest.approx(-ab["contribution"])
        assert ba["value"] == pytest.approx(-ab["value"])
        if ab["contribution"] != 0:
            assert {ab["direction"], ba["direction"]} == {"red", "blue"}
    for name in feature_columns:
        assert contrib_ba[name] == pytest.approx(-contrib_ab[name])


def test_attribution_sums_to_symmetrized_margin():
    # Bias cancels under symmetrization, so the contributions add up EXACTLY to
    # the symmetrized raw margin (log-odds) — the property that lets the UI draw
    # a "rest of factors" bar that closes the balance.
    import xgboost as xgb_module

    feature_columns = [f"f{i}_diff" for i in range(6)]
    model = _train_tiny_model(feature_columns)
    forward = np.array([[2.5, 0.1, -0.2, 0.0, 0.3, -0.1]])
    swapped = -forward

    _, contributions = _compute_top_features(model, feature_columns, forward, swapped)

    booster = model.get_booster()
    margin_fwd = float(booster.predict(xgb_module.DMatrix(forward), output_margin=True)[0])
    margin_swp = float(booster.predict(xgb_module.DMatrix(swapped), output_margin=True)[0])
    assert sum(contributions.values()) == pytest.approx((margin_fwd - margin_swp) / 2.0, abs=1e-5)


def test_compute_top_features_empty_without_booster():
    top, contributions = _compute_top_features(object(), ["f0_diff"], np.array([[1.0]]), np.array([[-1.0]]))
    assert top == []
    assert contributions is None
