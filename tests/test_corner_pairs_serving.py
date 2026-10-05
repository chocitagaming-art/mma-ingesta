"""Serving a winner bundle that carries per-corner pairs ({base}_red / {base}_blue).

Measured on the phase-4 map: with the old swap (negate *_diff, pass everything else)
a pair bundle served red(A,B)=0.5427 against blue(B,A)=0.5721, the block's signal
almost vanished from the probability (0.54 instead of 0.78) and its attribution came
out with the wrong sign, split in two bars named after a corner. These tests pin the
fix, offline, with tiny synthetic bundles and the REAL api.predict (only the
database reads are stubbed):

* the probability is exactly corner-symmetric on rows built for (A, B) and (B, A);
* the pairs really reach the probability (two fighters who differ ONLY in their
  pre-UFC record get a clear favourite; the old swap served exactly 0.5);
* each pair is ONE factor named after its base, antisymmetric, whose value is the
  raw red-minus-blue difference (None when a side is unknown, never the median);
* featureContributions uses the same names, and everything still adds up to the
  symmetrized margin.
"""

from __future__ import annotations

import math
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from xgboost import DMatrix, XGBClassifier

import src.prediction.api as api
from src.prediction.features import FighterHistorySummary, build_feature_row
from src.prediction.features.types import (
    CORNER_PAIR_BASES,
    FEATURE_COLUMNS,
    FEATURE_SETS,
    PREUFC_BASES,
)

PAIR_COLUMNS = FEATURE_SETS["preufc"]
EMPTY = pd.DataFrame()


class _NoImputation:
    """Native-NaN shape: nothing in front of the booster."""

    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        return frame.to_numpy(dtype=float, na_value=np.nan)


def _training_frame(seed: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Synthetic winner rows in the "preufc" schema. The label leans hard on the
    pre-UFC win rates of the two corners, a little on age; ~15% of the fighters
    have no pre-UFC record (rates NaN) and ~25% of the history diffs are missing."""
    rng = np.random.default_rng(seed)
    n_rows = 1500
    data: dict[str, np.ndarray] = {
        column: rng.normal(size=n_rows) for column in FEATURE_COLUMNS
    }
    for column in FEATURE_COLUMNS:
        if column not in {"height_cm_diff", "reach_cm_diff", "age_diff"}:
            data[column] = np.where(rng.random(n_rows) < 0.25, np.nan, data[column])
    for side in ("red", "blue"):
        has = rng.random(n_rows) < 0.85
        data[f"ufc_prev_fights_{side}"] = rng.integers(0, 20, size=n_rows).astype(float)
        data[f"espn_has_history_{side}"] = has.astype(float)
        data[f"espn_prev_fights_{side}"] = np.where(
            has, rng.integers(1, 25, size=n_rows), 0
        ).astype(float)
        draws = {
            "espn_win_rate": rng.uniform(0.2, 1.0, n_rows),
            "espn_ko_rate": rng.uniform(0.0, 0.7, n_rows),
            "espn_sub_rate": rng.uniform(0.0, 0.5, n_rows),
            "espn_streak": rng.integers(0, 9, size=n_rows).astype(float),
            "espn_days_since_last": rng.uniform(30.0, 900.0, n_rows),
            "espn_years_pro": rng.uniform(0.5, 12.0, n_rows),
            "espn_title_fights": rng.integers(0, 4, size=n_rows).astype(float),
        }
        for base, values in draws.items():
            data[f"{base}_{side}"] = np.where(has, values, np.nan)
    frame = pd.DataFrame(data)[PAIR_COLUMNS]
    red_rate = np.nan_to_num(frame["espn_win_rate_red"].to_numpy(), nan=0.5)
    blue_rate = np.nan_to_num(frame["espn_win_rate_blue"].to_numpy(), nan=0.5)
    logit = 6.0 * (red_rate - blue_rate) + 0.5 * frame["age_diff"].to_numpy()
    labels = (logit + rng.normal(scale=0.5, size=n_rows) > 0).astype(int)
    return frame, labels


def _pair_bundle(kind: str) -> dict:
    frame, labels = _training_frame(seed=7)
    model = XGBClassifier(n_estimators=60, max_depth=3, random_state=0)
    if kind == "median":
        # Today's production shape: median imputer + sigmoid calibrator.
        imputer = SimpleImputer(strategy="median").fit(frame)
        matrix = imputer.transform(frame)
        model.fit(matrix, labels)
        calibrator = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
        calibrator.fit(matrix, labels)
        return {
            "feature_columns": list(PAIR_COLUMNS),
            "imputer": imputer,
            "model": model,
            "calibrator": calibrator,
        }
    imputer = _NoImputation()
    model.fit(imputer.transform(frame), labels)
    return {"feature_columns": list(PAIR_COLUMNS), "imputer": imputer, "model": model}


@pytest.fixture(scope="module", params=["median", "native"])
def pair_bundle(request) -> dict:
    return _pair_bundle(request.param)


def _summary(**overrides) -> FighterHistorySummary:
    base = dict(
        total_prior_fights=6,
        total_rounds_fought=14,
        sig_strikes_landed_per_fight=42.0,
        sig_strike_accuracy=0.48,
        knockdowns_per_fight=0.3,
        takedowns_landed_per_fight=1.2,
        takedown_accuracy=0.4,
        submission_attempts_per_fight=0.5,
        control_time_seconds_per_fight=110.0,
        win_streak=2,
        wins_last_5=3,
        pct_wins_by_ko=0.4,
        pct_wins_by_submission=0.3,
        pct_wins_by_decision=0.3,
        days_since_last_fight=190,
        ranking_position=None,
        sig_strikes_absorbed_per_fight=33.0,
        sig_strike_defense=0.56,
        takedowns_absorbed_per_fight=1.0,
        takedown_defense=0.65,
        avg_opponent_prior_win_rate=0.5,
        latest_prior_fight_date=date(2026, 3, 1),
    )
    base.update(overrides)
    return FighterHistorySummary(**base)


def _preufc(win_rate: float | None, **overrides) -> dict:
    if win_rate is None:
        block = {base: None for base in PREUFC_BASES}
        block.update(espn_has_history=0.0, espn_prev_fights=0.0)
        return block
    block = {
        "espn_has_history": 1.0,
        "espn_prev_fights": 12.0,
        "espn_win_rate": win_rate,
        "espn_ko_rate": 0.4,
        "espn_sub_rate": 0.2,
        "espn_streak": 3.0,
        "espn_days_since_last": 200.0,
        "espn_years_pro": 5.0,
        "espn_title_fights": 1.0,
    }
    block.update(overrides)
    return block


VETERAN, PROSPECT, DEBUTANT, TWIN_STRONG, TWIN_WEAK = 11, 22, 33, 44, 55
SHARED = _summary()
FIGHTERS = {
    VETERAN: {
        "history": _summary(total_prior_fights=14, sig_strikes_landed_per_fight=51.0),
        "height_cm": 183.0,
        "reach_cm": 190.0,
        "age": 33.0,
        "ufc_prev_fights": 14,
        "preufc": _preufc(0.62, espn_prev_fights=9.0, espn_streak=1.0),
    },
    PROSPECT: {
        "history": _summary(
            total_prior_fights=2, wins_last_5=2, takedown_accuracy=None
        ),
        "height_cm": 178.0,
        "reach_cm": 181.0,
        "age": 26.0,
        "ufc_prev_fights": 2,
        "preufc": _preufc(0.94, espn_prev_fights=16.0, espn_streak=9.0),
    },
    # No UFC history and no pre-UFC record: every history diff and every rate
    # is unknown, which is what makes value=None and NaN routing matter.
    DEBUTANT: {
        "history": None,
        "height_cm": 175.0,
        "reach_cm": None,
        "age": 24.0,
        "ufc_prev_fights": 0,
        "preufc": _preufc(None),
    },
    # Identical in everything the 27-jun model sees; only the pre-UFC record
    # differs. With the old swap every diff is 0, the "swapped" row equals the
    # forward row and the served probability is exactly 0.5.
    TWIN_STRONG: {
        "history": SHARED,
        "height_cm": 180.0,
        "reach_cm": 184.0,
        "age": 29.0,
        "ufc_prev_fights": 6,
        "preufc": _preufc(0.95),
    },
    TWIN_WEAK: {
        "history": SHARED,
        "height_cm": 180.0,
        "reach_cm": 184.0,
        "age": 29.0,
        "ufc_prev_fights": 6,
        "preufc": _preufc(0.30),
    },
}


def _row(red_id: int, blue_id: int) -> dict:
    """The winner row as the phase-4 service will build it: diffs from the shared
    builder, plus each corner's own values in its own column."""
    red, blue = FIGHTERS[red_id], FIGHTERS[blue_id]
    row = build_feature_row(
        red["history"],
        blue["history"],
        red_height_cm=red["height_cm"],
        blue_height_cm=blue["height_cm"],
        red_reach_cm=red["reach_cm"],
        blue_reach_cm=blue["reach_cm"],
        red_age=red["age"],
        blue_age=blue["age"],
    )
    row["ufc_prev_fights_red"] = red["ufc_prev_fights"]
    row["ufc_prev_fights_blue"] = blue["ufc_prev_fights"]
    for base in PREUFC_BASES:
        row[f"{base}_red"] = red["preufc"][base]
        row[f"{base}_blue"] = blue["preufc"][base]
    return row


def _feature_rows(_fights, _rankings, red_id, blue_id, _physical, **_kwargs):
    context = {"lowConfidence": False, "redHistory": None, "blueHistory": None}
    return _row(red_id, blue_id), {}, context, False


def _profiles(_database_url, fighter_ids):
    return {
        fighter_id: api.FighterPredictionProfile(
            id=fighter_id,
            name=f"Fighter {fighter_id}",
            nickname=None,
            headshot_url=None,
            wins=5,
            losses=1,
            draws=0,
            height_cm=None,
            reach_cm=None,
            stance=None,
            latest_weight_class=None,
            aggregate_stats={},
        )
        for fighter_id in fighter_ids
    }


@pytest.fixture
def serve(monkeypatch):
    """serve(bundle, red, blue) -> the real api.predict output."""
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://stub.invalid"),
    )
    monkeypatch.setattr(api, "_load_fighter_physical", lambda _url, ids: {})
    monkeypatch.setattr(api, "_load_fighter_profiles", _profiles)
    # A pair bundle makes api.predict read fight_history_espn; the row is stubbed
    # below, so what it reads is never used.
    monkeypatch.setattr(
        api, "load_preufc_history", lambda _url: api.UNAVAILABLE_PREUFC_HISTORY
    )
    monkeypatch.setattr(api, "_build_feature_row", _feature_rows)

    def _serve(bundle: dict, red: int, blue: int) -> dict:
        return api.predict(
            red,
            blue,
            bundle=bundle,
            fights_df=EMPTY,
            rankings_df=EMPTY,
            history_df=EMPTY,
        )

    return _serve


MATCHUPS = [
    (VETERAN, PROSPECT),
    (PROSPECT, DEBUTANT),
    (DEBUTANT, VETERAN),
    (TWIN_STRONG, TWIN_WEAK),
]
MATCHUP_IDS = ["veteran-prospect", "prospect-debutant", "debutant-veteran", "twins"]


def _margin(bundle: dict, row: dict) -> float:
    frame = pd.DataFrame([{c: row.get(c) for c in bundle["feature_columns"]}])
    matrix = bundle["imputer"].transform(frame[bundle["feature_columns"]])
    booster = bundle["model"].get_booster()
    return float(booster.predict(DMatrix(matrix), output_margin=True)[0])


# --- Probability -------------------------------------------------------------------


@pytest.mark.parametrize(("red", "blue"), MATCHUPS, ids=MATCHUP_IDS)
def test_pair_bundle_probability_is_exactly_corner_symmetric(
    serve, pair_bundle, red, blue
):
    """On rows BUILT for (A, B) and for (B, A), not on swap(row): any involution
    satisfies p(r) + p(swap(r)) = 1, the genuine rows are what catch a wrong swap."""
    forward = serve(pair_bundle, red, blue)
    backward = serve(pair_bundle, blue, red)

    assert abs(forward["redProbability"] - (1.0 - backward["redProbability"])) < 1e-12
    assert abs(forward["redProbability"] - backward["blueProbability"]) < 1e-12


def test_pairs_really_reach_the_served_probability(serve, pair_bundle):
    """Two fighters who differ ONLY in their pre-UFC record. Every diff is 0, so
    the old swap returned the forward row unchanged and served exactly 0.5."""
    strong = serve(pair_bundle, TWIN_STRONG, TWIN_WEAK)["redProbability"]
    weak = serve(pair_bundle, TWIN_WEAK, TWIN_STRONG)["redProbability"]

    assert strong > 0.75
    assert weak < 0.25
    assert abs(strong - (1.0 - weak)) < 1e-12


def test_evaluate_symmetrizes_a_pair_bundle_exactly_like_serving(serve, pair_bundle):
    """evaluate.py imports api._swap_corners: on rows built for (A, B) and (B, A)
    its symmetrized probability is the served one. The slice carries ids and the
    target next to the features, like the real test slice; evaluate selects the
    feature columns before swapping, so the strict swap never sees them."""
    from src.prediction import evaluate

    test_df = pd.DataFrame([_row(VETERAN, PROSPECT), _row(PROSPECT, VETERAN)])
    test_df.insert(0, "fight_id", [901, 902])
    test_df["event_date"] = pd.Timestamp("2026-10-03")
    test_df["target"] = [1, 0]
    estimator = pair_bundle.get("calibrator") or pair_bundle["model"]

    _raw, symmetrized = evaluate._estimator_probabilities(
        estimator, pair_bundle["imputer"], pair_bundle["feature_columns"], test_df
    )

    # An uncalibrated XGBoost hands out float32 and evaluate averages in that
    # dtype; the served path averages in Python floats. The old swap missed by
    # 1e-4 to 1e-2 on this matchup, far above either tolerance.
    tolerance = 1e-12 if symmetrized.dtype == np.float64 else 1e-6
    assert abs(symmetrized[0] - (1.0 - symmetrized[1])) < tolerance
    served = serve(pair_bundle, VETERAN, PROSPECT)["redProbability"]
    assert abs(float(symmetrized[0]) - served) < tolerance


def test_row_is_built_from_the_bundles_columns(pair_bundle):
    """_red_win_probability takes the bundle's own feature_columns: a pair column
    is not in FEATURE_COLUMNS and must not be a KeyError (a 500 for everyone)."""
    row = _row(VETERAN, PROSPECT)
    estimator = pair_bundle.get("calibrator") or pair_bundle["model"]
    columns = pair_bundle["feature_columns"]

    probability, transformed = api._red_win_probability(
        row, pair_bundle["imputer"], estimator, columns
    )

    frame = pd.DataFrame([{c: row[c] for c in columns}])
    expected = pair_bundle["imputer"].transform(frame)
    np.testing.assert_array_equal(transformed, expected)
    assert probability == float(estimator.predict_proba(expected)[0][1])


# --- Factors -----------------------------------------------------------------------


@pytest.mark.parametrize(("red", "blue"), MATCHUPS, ids=MATCHUP_IDS)
def test_each_pair_is_one_factor_named_after_its_base(serve, pair_bundle, red, blue):
    result = serve(pair_bundle, red, blue)
    contributions = result["featureContributions"]

    assert set(contributions) == set(FEATURE_COLUMNS) | set(CORNER_PAIR_BASES)
    for name in [f["name"] for f in result["topFeatures"]] + list(contributions):
        assert not name.endswith(("_red", "_blue")), name
    # Same keys as topFeatures, or the web's "rest" bar counts a pair twice.
    for factor in result["topFeatures"]:
        assert contributions[factor["name"]] == factor["contribution"]
        assert factor["direction"] == ("red" if factor["contribution"] >= 0 else "blue")


@pytest.mark.parametrize(("red", "blue"), MATCHUPS, ids=MATCHUP_IDS)
def test_pair_factors_are_antisymmetric(serve, pair_bundle, red, blue):
    forward = serve(pair_bundle, red, blue)
    backward = serve(pair_bundle, blue, red)

    assert set(forward["featureContributions"]) == set(backward["featureContributions"])
    for name, contribution in forward["featureContributions"].items():
        assert backward["featureContributions"][name] == pytest.approx(
            -contribution, abs=1e-12
        ), name
    assert [f["name"] for f in forward["topFeatures"]] == [
        f["name"] for f in backward["topFeatures"]
    ]
    for ab, ba in zip(forward["topFeatures"], backward["topFeatures"]):
        if ab["value"] is None:
            assert ba["value"] is None, ab["name"]
        else:
            assert ba["value"] == pytest.approx(-ab["value"], abs=1e-12), ab["name"]


@pytest.mark.parametrize(("red", "blue"), MATCHUPS, ids=MATCHUP_IDS)
def test_factors_still_add_up_to_the_symmetrized_margin(serve, pair_bundle, red, blue):
    """Merging a pair only regroups the same terms: factors + "rest" close the
    balance exactly as before."""
    result = serve(pair_bundle, red, blue)

    margin = (
        _margin(pair_bundle, _row(red, blue)) - _margin(pair_bundle, _row(blue, red))
    ) / 2.0
    assert sum(result["featureContributions"].values()) == pytest.approx(
        margin, abs=1e-5
    )


def test_pair_factor_is_the_sum_of_both_corners(serve, pair_bundle):
    """contribution(base) = symmetrized contribution of {base}_red + of {base}_blue."""
    columns = pair_bundle["feature_columns"]
    booster = pair_bundle["model"].get_booster()

    def contributions(row: dict) -> np.ndarray:
        frame = pd.DataFrame([{c: row.get(c) for c in columns}])
        matrix = pair_bundle["imputer"].transform(frame)
        return booster.predict(DMatrix(matrix), pred_contribs=True)[0][:-1]

    symmetrized = (
        contributions(_row(TWIN_STRONG, TWIN_WEAK))
        - contributions(_row(TWIN_WEAK, TWIN_STRONG))
    ) / 2.0
    result = serve(pair_bundle, TWIN_STRONG, TWIN_WEAK)

    for base in CORNER_PAIR_BASES:
        expected = float(symmetrized[columns.index(f"{base}_red")]) + float(
            symmetrized[columns.index(f"{base}_blue")]
        )
        assert result["featureContributions"][base] == pytest.approx(
            expected, abs=1e-12
        ), base
    # The block carries the decision here, and it points at the strong twin.
    assert result["featureContributions"]["espn_win_rate"] > 0.5
    assert result["topFeatures"][0]["name"] == "espn_win_rate"
    assert result["topFeatures"][0]["direction"] == "red"
    assert result["topFeatures"][0]["value"] == pytest.approx(0.95 - 0.30)


# --- value: the raw red-minus-blue difference ------------------------------------


def _pin_raw_contributions(monkeypatch, forward: list[float], swapped: list[float]):
    calls = iter(
        [np.array(forward, dtype=np.float32), np.array(swapped, dtype=np.float32)]
    )
    monkeypatch.setattr(api, "_raw_contributions", lambda _model, _row: next(calls))


def test_pair_value_is_raw_red_minus_blue_never_the_imputed_number(monkeypatch):
    columns = ["age_diff", "espn_win_rate_red", "espn_win_rate_blue"]
    # What the booster saw (imputed medians), NOT what the factor must report.
    transformed = np.array([[2.0, 0.55, 0.55]])
    _pin_raw_contributions(
        monkeypatch, forward=[0.10, 0.50, -0.10], swapped=[-0.10, -0.30, 0.40]
    )

    top, contributions = api._compute_top_features(
        object(),
        columns,
        transformed,
        transformed,
        raw_row={"age_diff": 2.0, "espn_win_rate_red": 0.8, "espn_win_rate_blue": 0.3},
    )

    assert [f["name"] for f in top] == ["espn_win_rate", "age_diff"]
    pair = top[0]
    assert pair["contribution"] == pytest.approx(0.40 + (-0.25))
    assert pair["value"] == pytest.approx(0.5)
    assert pair["direction"] == "red"
    # A diff keeps today's value: what the booster saw.
    assert top[1]["value"] == pytest.approx(2.0)
    assert contributions == pytest.approx({"age_diff": 0.10, "espn_win_rate": 0.15})


@pytest.mark.parametrize(
    "raw_row",
    [
        {"espn_win_rate_red": 0.8, "espn_win_rate_blue": None},
        {"espn_win_rate_red": None, "espn_win_rate_blue": 0.3},
        {"espn_win_rate_red": float("nan"), "espn_win_rate_blue": 0.3},
        {"espn_win_rate_red": None, "espn_win_rate_blue": None},
        None,
    ],
    ids=["blue-missing", "red-missing", "red-nan", "both-missing", "no-raw-row"],
)
def test_pair_value_none_when_one_side_missing(monkeypatch, raw_row):
    """The web paints N/D for None. The imputed median is a made-up number for
    someone without a record: it must never be reported as the difference."""
    columns = ["espn_win_rate_red", "espn_win_rate_blue"]
    transformed = np.array([[0.8, 0.55]])
    _pin_raw_contributions(monkeypatch, forward=[0.3, 0.2], swapped=[-0.1, -0.2])

    top, contributions = api._compute_top_features(
        object(), columns, transformed, transformed[:, ::-1], raw_row=raw_row
    )

    assert len(top) == 1
    assert top[0]["name"] == "espn_win_rate"
    assert top[0]["value"] is None
    assert top[0]["contribution"] == pytest.approx(0.2 + 0.2)
    assert math.isfinite(contributions["espn_win_rate"])


def test_half_a_pair_in_a_bundle_stays_its_own_factor(monkeypatch):
    """A bundle should never hold half a pair (whole pairs are dropped together),
    but the ranking is informative and must not 500 over it. Its value is None:
    the one corner the booster saw may be an imputed median, and a single
    fighter's number is not a red-minus-blue difference anyway."""
    columns = ["age_diff", "espn_win_rate_red"]
    transformed = np.array([[1.0, 0.7]])
    _pin_raw_contributions(monkeypatch, forward=[0.2, 0.1], swapped=[-0.2, -0.1])

    top, contributions = api._compute_top_features(
        object(),
        columns,
        transformed,
        -transformed,
        raw_row={"age_diff": 1.0, "espn_win_rate_red": 0.7},
    )

    assert [f["name"] for f in top] == ["age_diff", "espn_win_rate_red"]
    assert top[0]["value"] == pytest.approx(1.0)
    assert top[1]["value"] is None
    assert top[1]["contribution"] == pytest.approx(0.1)
    assert set(contributions) == {"age_diff", "espn_win_rate_red"}
