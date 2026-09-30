"""Non-finite floats (NaN / Infinity) in the POST /predict payload.

Phase 4 brings per-corner pre-UFC columns, possibly served with XGBoost's NATIVE
missing-value handling: nothing in front of the booster, so a debutant's missing
value reaches it as NaN, and ``_compute_top_features`` echoed that NaN back as
``topFeatures[].value``. The contract agreed with mma-app:

* a non-finite ``topFeatures[].value`` travels as null. The factor stays: its
  contribution is real;
* a non-finite contribution is not a measurement: that factor leaves the ranking
  and the full map, with a warning in the log;
* non-finite METHOD probabilities drop only the (secondary) methodPrediction;
* a non-finite WIN probability is a model defect: an explicit 500, never a null;
* anything else non-finite goes out as null at the service boundary, and is logged.

Why the boundary cannot lean on the serializer: FastAPI serializes the endpoint's
dict through pydantic, whose default happens to write NaN as null, silently, even
for a win probability. Starlette's own JSONResponse (every ``_error`` path, or a
future ``return JSONResponse(result)``) renders with ``allow_nan=False`` and turns
the very same NaN into a 500. And neither of them can serialize a numpy float32.

Offline and deterministic: the REAL FastAPI app and the REAL ``api.predict`` with
tiny synthetic models; only the database reads are stubbed.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sklearn.impute import SimpleImputer
from xgboost import XGBClassifier

import src.prediction.api as api
import src.prediction.service as service
from src.prediction.features import (
    FEATURE_COLUMNS,
    FighterHistorySummary,
    build_feature_row,
)
from src.prediction.features.method_features import (
    METHOD_CLASSES,
    METHOD_FEATURE_COLUMNS,
    build_method_feature_row,
)

DEBUTANT_RED, DEBUTANT_BLUE = 101, 202
VETERAN_RED, VETERAN_BLUE = 303, 404
PHYSICAL = {
    DEBUTANT_RED: {
        "birth_date": date(1998, 5, 1),
        "height_cm": 178.0,
        "reach_cm": 183.0,
    },
    DEBUTANT_BLUE: {
        "birth_date": date(2000, 1, 15),
        "height_cm": 175.0,
        "reach_cm": 180.0,
    },
    VETERAN_RED: {
        "birth_date": date(1990, 3, 3),
        "height_cm": 180.0,
        "reach_cm": 185.0,
    },
    VETERAN_BLUE: {
        "birth_date": date(1992, 7, 7),
        "height_cm": 177.0,
        "reach_cm": 188.0,
    },
}
PHYSICAL_COLUMNS = {"height_cm_diff", "reach_cm_diff", "age_diff"}
TRAINED_AT = "2026-09-30"

# Real column names, no rows: _build_feature_row walks the genuine degraded path
# for two debutants (no bout on record -> today's date, None histories).
EMPTY_FIGHTS = pd.DataFrame(
    columns=[
        "fight_id",
        "event_date",
        "fighter_red_id",
        "fighter_blue_id",
        "winner_id",
        "weight_class",
        "scheduled_rounds",
        "is_title_fight",
    ]
)
EMPTY_HISTORY = pd.DataFrame(columns=["fighter_id", "event_date", "fight_id"])
EMPTY_RANKINGS = pd.DataFrame()


def _strict_json(text: str):
    """Parse a body as STRICT JSON: a NaN / Infinity token fails the test."""

    def reject(token: str):
        raise AssertionError(f"non-standard JSON token on the wire: {token}")

    return json.loads(text, parse_constant=reject)


class _NoImputation:
    """Phase-4 shape: nothing in front of the booster, NaN is routed natively."""

    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        return frame.to_numpy(dtype=float, na_value=np.nan)


class _BrokenEstimator:
    """predict_proba full of NaN, like a corrupt model or calibrator."""

    def __init__(self, n_classes: int) -> None:
        self.classes_ = np.arange(n_classes)

    def predict_proba(self, rows) -> np.ndarray:
        return np.full((len(rows), len(self.classes_)), np.nan)


def _training_matrix(seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic diffs whose history columns go missing in ~40% of the rows (the
    debutants), with a label that leans on one of them."""
    rng = np.random.default_rng(seed)
    n_rows = 400
    x = rng.normal(size=(n_rows, len(FEATURE_COLUMNS)))
    signal = (
        x[:, FEATURE_COLUMNS.index("wins_last_5_diff")]
        + 0.5 * x[:, FEATURE_COLUMNS.index("age_diff")]
    )
    y = (signal + rng.normal(scale=0.3, size=n_rows) > 0).astype(int)
    history = [
        i for i, name in enumerate(FEATURE_COLUMNS) if name not in PHYSICAL_COLUMNS
    ]
    holes = rng.random(size=(n_rows, len(history))) < 0.4
    x[:, history] = np.where(holes, np.nan, x[:, history])
    return x, y


@pytest.fixture(scope="module")
def native_nan_bundle() -> dict:
    x, y = _training_matrix(seed=3)
    model = XGBClassifier(n_estimators=20, max_depth=3, random_state=0)
    # numpy, like train.py: booster.feature_names is None.
    model.fit(x, y)
    return {
        "feature_columns": list(FEATURE_COLUMNS),
        "imputer": _NoImputation(),
        "model": model,
        "trained_at": TRAINED_AT,
    }


@pytest.fixture(scope="module")
def method_imputer() -> SimpleImputer:
    rng = np.random.default_rng(11)
    frame = pd.DataFrame(
        rng.normal(size=(300, len(METHOD_FEATURE_COLUMNS))),
        columns=METHOD_FEATURE_COLUMNS,
    )
    return SimpleImputer(strategy="median").fit(frame)


@pytest.fixture(scope="module")
def imputed_bundle(method_imputer) -> dict:
    """Today's production shape: median imputers in front of both boosters."""
    x, y = _training_matrix(seed=4)
    frame = pd.DataFrame(x, columns=FEATURE_COLUMNS)
    imputer = SimpleImputer(strategy="median").fit(frame)
    model = XGBClassifier(n_estimators=20, max_depth=3, random_state=0)
    model.fit(imputer.transform(frame), y)

    rng = np.random.default_rng(12)
    method_frame = pd.DataFrame(
        rng.normal(size=(300, len(METHOD_FEATURE_COLUMNS))),
        columns=METHOD_FEATURE_COLUMNS,
    )
    method_model = XGBClassifier(
        objective="multi:softprob", n_estimators=8, max_depth=2, random_state=0
    )
    method_model.fit(
        method_imputer.transform(method_frame),
        rng.integers(0, len(METHOD_CLASSES), size=300),
    )
    return {
        "feature_columns": list(FEATURE_COLUMNS),
        "imputer": imputer,
        "model": model,
        "trained_at": TRAINED_AT,
        "method_model": method_model,
        "method_imputer": method_imputer,
        "method_feature_columns": list(METHOD_FEATURE_COLUMNS),
        "method_classes": list(METHOD_CLASSES),
        "method_trained_at": TRAINED_AT,
    }


def _fake_profiles(_database_url, fighter_ids):
    return {
        fighter_id: api.FighterPredictionProfile(
            id=fighter_id,
            name=f"Fighter {fighter_id}",
            nickname=None,
            headshot_url=None,
            wins=5,
            losses=1,
            draws=0,
            height_cm=PHYSICAL[fighter_id]["height_cm"],
            reach_cm=PHYSICAL[fighter_id]["reach_cm"],
            stance="Orthodox",
            latest_weight_class=None,
            aggregate_stats={
                "sigStrikesLandedPerFight": 40.5,
                "sigStrikeAccuracy": 0.47,
            },
        )
        for fighter_id in fighter_ids
    }


@pytest.fixture
def serve(monkeypatch):
    """serve(bundle) -> TestClient over the real service and the real api.predict.

    Only what would open a socket to Neon is stubbed (get_settings included:
    there is no DATABASE_URL here, on purpose)."""
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://stub.invalid"),
    )
    monkeypatch.setattr(
        api, "_load_fighter_physical", lambda _url, ids: {i: PHYSICAL[i] for i in ids}
    )
    monkeypatch.setattr(api, "_load_fighter_profiles", _fake_profiles)
    monkeypatch.setattr(service, "_existing_fighter_ids", lambda ids: set(ids))
    monkeypatch.setattr(
        service,
        "_get_dataframes",
        lambda: (EMPTY_FIGHTS, EMPTY_RANKINGS, EMPTY_HISTORY),
    )
    for name in service.API_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PREDICTION_ENV", raising=False)

    def _serve(bundle: dict) -> TestClient:
        monkeypatch.setattr(service, "_get_bundle", lambda: bundle)
        # A crash while serializing must show up as the 500 the web would get,
        # not as a Python exception inside the test.
        return TestClient(service.app, raise_server_exceptions=False)

    return _serve


# --- The phase-4 debutant, end to end ------------------------------------------


def test_debutant_nan_value_travels_as_null_and_the_factor_stays(
    serve, native_nan_bundle, caplog
):
    client = serve(native_nan_bundle)
    with caplog.at_level(logging.WARNING):
        response = client.post(
            "/predict", json={"red": DEBUTANT_RED, "blue": DEBUTANT_BLUE}
        )

    assert response.status_code == 200, response.text
    body = _strict_json(response.text)
    assert body["lowConfidence"] is True
    unknown = [feature for feature in body["topFeatures"] if feature["value"] is None]
    assert unknown, "two debutants must rank at least one factor the model saw as NaN"
    for feature in body["topFeatures"]:
        # The value may be unknown; the contribution never is.
        assert math.isfinite(feature["contribution"])
        assert feature["direction"] == (
            "red" if feature["contribution"] >= 0 else "blue"
        )
    # A missing value is the contract, not an anomaly: no log noise per debutant.
    assert [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ] == []


def test_endpoint_hands_the_serializer_strict_json_only(serve, native_nan_bundle):
    """The wire above is clean only because pydantic's default writes NaN as
    null. Starlette's JSONResponse (allow_nan=False) raises on the same payload:
    that is the 500. The endpoint must return finite floats or None, whatever
    serializer comes after it."""
    serve(native_nan_bundle)
    payload = service.predict_endpoint(
        service.PredictRequest(red=DEBUTANT_RED, blue=DEBUTANT_BLUE), x_api_key=None
    )

    assert isinstance(payload, dict), payload
    JSONResponse(content=payload)  # raises ValueError on NaN / Infinity


# --- _compute_top_features -------------------------------------------------------


def _pin_raw_contributions(
    monkeypatch, forward: list[float], swapped: list[float]
) -> None:
    """Pin the TreeSHAP output (float32, like the booster's): the booster is not
    what is under test here, the ranking built on top of it is."""
    calls = iter(
        [np.array(forward, dtype=np.float32), np.array(swapped, dtype=np.float32)]
    )
    monkeypatch.setattr(api, "_raw_contributions", lambda _model, _row: next(calls))


@pytest.mark.parametrize("unknown", [np.nan, np.inf, -np.inf])
def test_non_finite_value_is_none_and_the_factor_keeps_its_rank(monkeypatch, unknown):
    columns = ["a_diff", "b_diff", "c_diff"]
    row = np.array([[unknown, 1.5, -0.5]])
    _pin_raw_contributions(
        monkeypatch, forward=[0.9, 0.2, -0.1], swapped=[-0.9, -0.2, 0.1]
    )

    top, contributions = api._compute_top_features(object(), columns, row, -row)

    assert [item["name"] for item in top] == ["a_diff", "b_diff", "c_diff"]
    assert top[0]["value"] is None
    assert top[0]["contribution"] == pytest.approx(0.9)
    assert top[0]["direction"] == "red"
    assert top[1]["value"] == pytest.approx(1.5)
    assert contributions == pytest.approx(
        {"a_diff": 0.9, "b_diff": 0.2, "c_diff": -0.1}
    )


@pytest.mark.parametrize("broken", [np.nan, np.inf])
def test_non_finite_contribution_leaves_the_ranking_and_the_map(
    monkeypatch, caplog, broken
):
    """A NaN cannot be drawn as a bar, breaks the |contribution| sort for every
    other factor, and the web's schema would reject the whole prediction over a
    null contribution. So that factor goes, loudly; the rest stay ranked."""
    columns = ["a_diff", "b_diff", "c_diff", "d_diff"]
    row = np.array([[1.0, 2.0, 3.0, 4.0]])
    _pin_raw_contributions(
        monkeypatch, forward=[0.1, broken, -0.7, 0.3], swapped=[-0.1, 0.0, 0.7, -0.3]
    )

    with caplog.at_level(logging.WARNING, logger="prediction.api"):
        top, contributions = api._compute_top_features(object(), columns, row, -row)

    assert [item["name"] for item in top] == ["c_diff", "d_diff", "a_diff"]
    assert set(contributions) == {"a_diff", "c_diff", "d_diff"}
    assert "b_diff" in caplog.text


# --- _predict_method -------------------------------------------------------------


def _method_row(value: float | None = 0.5) -> dict:
    return {column: value for column in METHOD_FEATURE_COLUMNS}


def test_non_finite_method_probabilities_drop_only_the_method_block(
    method_imputer, caplog
):
    bundle = {
        "method_model": _BrokenEstimator(len(METHOD_CLASSES)),
        "method_imputer": method_imputer,
        "method_feature_columns": list(METHOD_FEATURE_COLUMNS),
        "method_classes": list(METHOD_CLASSES),
    }

    with caplog.at_level(logging.WARNING, logger="prediction.api"):
        assert api._predict_method(bundle, _method_row()) is None
    assert "methodPrediction dropped" in caplog.text


def test_nan_in_the_method_row_is_imputed_not_propagated(imputed_bundle):
    """The method path already survives a NaN INPUT: its imputer fills it in both
    orientations. Only a broken model can put a NaN in its OUTPUT (test above)."""
    row = _method_row(0.5)
    row["sig_strikes_landed_per_fight_diff"] = float("nan")

    result = api._predict_method(imputed_bundle, row)

    assert result is not None
    assert all(math.isfinite(value) for value in result["probabilities"].values())
    # Uncalibrated XGBoost probabilities are float32.
    assert sum(result["probabilities"].values()) == pytest.approx(1.0, abs=1e-6)


# --- Win probability: a defect, never a null -------------------------------------


def test_non_finite_win_probability_is_an_explicit_500_not_a_null(serve, caplog):
    broken = _BrokenEstimator(2)
    # Broken in both slots, so the test holds whichever one predict() reads.
    bundle = {
        "feature_columns": list(FEATURE_COLUMNS),
        "imputer": _NoImputation(),
        "model": broken,
        "calibrator": broken,
        "trained_at": TRAINED_AT,
    }

    with caplog.at_level(logging.ERROR, logger="prediction.service"):
        response = serve(bundle).post(
            "/predict", json={"red": DEBUTANT_RED, "blue": DEBUTANT_BLUE}
        )

    assert response.status_code == 500, response.text
    assert response.json() == {"error": "Internal prediction error"}
    assert "redProbability" in caplog.text


# --- The service boundary: everything else goes out as null, and is logged -------


@pytest.fixture
def boundary(monkeypatch):
    """boundary(payload) -> TestClient whose predict() returns that payload."""
    monkeypatch.setattr(service, "_existing_fighter_ids", lambda ids: set(ids))
    monkeypatch.setattr(service, "_get_bundle", lambda: {"trained_at": TRAINED_AT})
    monkeypatch.setattr(service, "_get_dataframes", lambda: (None, None, None))
    for name in service.API_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PREDICTION_ENV", raising=False)

    def _boundary(payload: dict) -> TestClient:
        monkeypatch.setattr(service, "predict", lambda red, blue, **_kwargs: payload)
        return TestClient(service.app, raise_server_exceptions=False)

    return _boundary


def _payload(**overrides) -> dict:
    payload = {
        "redProbability": 0.6,
        "blueProbability": 0.4,
        "topFeatures": [
            {"name": "age_diff", "value": -2.5, "contribution": 0.2, "direction": "red"}
        ],
        "featureContributions": {"age_diff": 0.2},
        "featureValues": {"age_diff": -2.5, "reach_cm_diff": None},
        "methodPrediction": None,
        "context": {"lowConfidence": True, "redHistory": None, "blueHistory": None},
        "lowConfidence": True,
        "fighters": {"red": {"id": 1}, "blue": {"id": 2}},
    }
    payload.update(overrides)
    return payload


def test_a_numpy_float32_nan_is_not_a_500(boundary):
    """XGBoost hands out float32. pydantic cannot serialize one at all, so a
    single float32 NaN that escapes a float() cast was a text/plain 500."""
    client = boundary(
        _payload(featureValues={"age_diff": np.float32("nan"), "reach_cm_diff": None})
    )

    response = client.post("/predict", json={"red": 1, "blue": 2})

    assert response.status_code == 200, response.text
    assert _strict_json(response.text)["featureValues"] == {
        "age_diff": None,
        "reach_cm_diff": None,
    }


def _non_finite_payload() -> dict:
    """A non-finite float in every block (Python and numpy ones), plus a finite
    numpy float32, the type XGBoost hands out."""
    return _payload(
        topFeatures=[
            {
                "name": "age_diff",
                "value": math.nan,
                "contribution": 0.2,
                "direction": "red",
            }
        ],
        featureContributions={"age_diff": 0.2, "pre_ufc_diff": math.inf},
        featureValues={"age_diff": math.nan, "reach_cm_diff": None},
        methodPrediction={
            "probabilities": {"decision": 0.5, "ko": math.nan, "submission": 0.2},
            "predicted": "decision",
            "trainedAt": TRAINED_AT,
        },
        context={
            "lowConfidence": True,
            "redHistory": {
                "avg_opponent_prior_win_rate": -math.inf,
                "win_streak": 2,
            },
            "blueHistory": None,
        },
        fighters={
            "red": {"id": 1, "reach_cm": np.float64("nan")},
            "blue": {"id": 2, "reach_cm": np.float32(180.5)},
        },
    )


def test_non_finite_floats_anywhere_go_out_as_null_and_are_logged(boundary, caplog):
    client = boundary(_non_finite_payload())

    with caplog.at_level(logging.WARNING, logger="prediction.service"):
        response = client.post("/predict", json={"red": 1, "blue": 2})

    assert response.status_code == 200, response.text
    body = _strict_json(response.text)
    assert body["topFeatures"][0]["value"] is None
    assert body["featureContributions"] == {"age_diff": 0.2, "pre_ufc_diff": None}
    assert body["featureValues"] == {"age_diff": None, "reach_cm_diff": None}
    # A null here makes the web drop only the method block (its second-chance
    # parse); _predict_method already drops it at the source, this is the net.
    assert body["methodPrediction"]["probabilities"] == {
        "decision": 0.5,
        "ko": None,
        "submission": 0.2,
    }
    assert body["context"]["redHistory"] == {
        "avg_opponent_prior_win_rate": None,
        "win_streak": 2,
    }
    assert body["fighters"]["red"]["reach_cm"] is None
    assert body["redProbability"] == 0.6
    # Not silent: every field sent as null is named in the log.
    for path in (
        "topFeatures[0].value",
        "featureContributions.pre_ufc_diff",
        "featureValues.age_diff",
        "methodPrediction.probabilities.ko",
        "context.redHistory.avg_opponent_prior_win_rate",
        "fighters.red.reach_cm",
    ):
        assert path in caplog.text


def test_non_finite_floats_are_none_before_any_serializer_runs(boundary):
    """The wire above cannot tell who wrote each null: pydantic writes a Python
    NaN or Infinity as null on its own. So read what the endpoint returns, before
    any serializer: None for every non-finite float, plain floats elsewhere, and
    nothing that Starlette's strict JSONResponse would reject."""
    boundary(_non_finite_payload())

    payload = service.predict_endpoint(
        service.PredictRequest(red=1, blue=2), x_api_key=None
    )

    assert isinstance(payload, dict), payload
    JSONResponse(content=payload)  # raises on NaN / Infinity, and on a numpy float
    assert payload["topFeatures"][0]["value"] is None
    assert payload["featureContributions"]["pre_ufc_diff"] is None
    assert payload["featureValues"]["age_diff"] is None
    assert payload["methodPrediction"]["probabilities"]["ko"] is None
    assert payload["context"]["redHistory"]["avg_opponent_prior_win_rate"] is None
    assert payload["fighters"]["red"]["reach_cm"] is None
    reach = payload["fighters"]["blue"]["reach_cm"]
    assert type(reach) is float and reach == 180.5


# --- Regression: finite rows go out exactly as computed --------------------------


def _summary(seed: float) -> FighterHistorySummary:
    return FighterHistorySummary(
        total_prior_fights=int(8 + seed),
        total_rounds_fought=int(20 + seed),
        sig_strikes_landed_per_fight=40.0 + seed,
        sig_strike_accuracy=0.45,
        knockdowns_per_fight=0.2 + seed / 10,
        takedowns_landed_per_fight=1.0 + seed / 5,
        takedown_accuracy=0.4,
        submission_attempts_per_fight=0.5,
        control_time_seconds_per_fight=100.0 + seed,
        win_streak=2,
        wins_last_5=int(2 + seed) % 6,
        pct_wins_by_ko=0.4,
        pct_wins_by_submission=0.3,
        pct_wins_by_decision=0.3,
        days_since_last_fight=180,
        ranking_position=None,
        sig_strikes_absorbed_per_fight=30.0 + seed,
        sig_strike_defense=0.55,
        takedowns_absorbed_per_fight=1.2,
        takedown_defense=0.6,
        avg_opponent_prior_win_rate=0.5,
        latest_prior_fight_date=date(2026, 3, 1),
    )


VETERAN_HISTORIES = {VETERAN_RED: _summary(3.0), VETERAN_BLUE: _summary(1.0)}


def _veteran_feature_rows(
    _fights, _rankings, red_id, blue_id, physical, history_df=None
):
    """What _build_feature_row returns for two fighters with a full history."""
    red, blue = VETERAN_HISTORIES[red_id], VETERAN_HISTORIES[blue_id]
    matchup = date(2026, 10, 3)
    feature_row = build_feature_row(
        red,
        blue,
        red_height_cm=physical[red_id]["height_cm"],
        blue_height_cm=physical[blue_id]["height_cm"],
        red_reach_cm=physical[red_id]["reach_cm"],
        blue_reach_cm=physical[blue_id]["reach_cm"],
        red_age=api.compute_age(physical[red_id]["birth_date"], matchup),
        blue_age=api.compute_age(physical[blue_id]["birth_date"], matchup),
    )
    method_row = build_method_feature_row(
        feature_row,
        red,
        blue,
        scheduled_rounds=5,
        weight_class="Lightweight",
        is_title_fight=True,
    )
    context = {
        "matchupDate": matchup.isoformat(),
        "weightClass": "Lightweight",
        "scheduledRounds": 5,
        "redHistory": asdict(red),
        "blueHistory": asdict(blue),
        "lowConfidence": False,
    }
    return feature_row, method_row, context, False


@pytest.mark.parametrize(
    ("red", "blue"),
    [
        (DEBUTANT_RED, DEBUTANT_BLUE),
        (VETERAN_RED, VETERAN_BLUE),
        (VETERAN_BLUE, VETERAN_RED),
    ],
    ids=["debutants", "veterans", "veterans-swapped"],
)
def test_finite_prediction_goes_out_exactly_as_computed(
    serve, imputed_bundle, monkeypatch, caplog, red, blue
):
    """Regression: with today's imputed bundle nothing is non-finite, so the new
    guards must change nothing: same keys, same numbers, no warning."""
    if red in VETERAN_HISTORIES:
        monkeypatch.setattr(api, "_build_feature_row", _veteran_feature_rows)
    client = serve(imputed_bundle)

    with caplog.at_level(logging.WARNING):
        response = client.post("/predict", json={"red": red, "blue": blue})

    assert response.status_code == 200, response.text
    expected = api.predict(
        red,
        blue,
        bundle=imputed_bundle,
        fights_df=EMPTY_FIGHTS,
        rankings_df=EMPTY_RANKINGS,
        history_df=EMPTY_HISTORY,
    )
    expected["modelTrainedAt"] = TRAINED_AT
    assert _strict_json(response.text) == json.loads(json.dumps(expected, default=str))
    assert expected["methodPrediction"] is not None
    assert len(expected["topFeatures"]) == 5
    assert all(
        isinstance(feature["value"], float) for feature in expected["topFeatures"]
    )
    assert [
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    ] == []
