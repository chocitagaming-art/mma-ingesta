"""Golden of the served prediction with the 27-jun bundle.

Phase 4 rewires the corner swap and the factor ranking for per-corner columns. The
27-jun bundle has none of them (20 red-minus-blue diffs), so for it the new code
must be INERT: same symmetrized probability, same topFeatures, same
featureContributions and same methodPrediction.

The fixture was recorded with the code of 4dfeb86, BEFORE the phase-4 changes, on
synthetic but realistic rows (veterans, debutants, missing physicals, unranked,
zero takedown attempts, a few float NaN). It stores the rows themselves, so this
test does not depend on the feature builders staying the same, and it runs the
REAL api.predict with only the database reads stubbed.

PERMANENT on purpose: the bundle is a byte copy of the committed 27-jun
src/prediction/model.joblib (sha256 6ccb0e5e...), kept in tests/fixtures. When a new
model.joblib is committed this still runs, and it is the net that proves a rollback
to the 27-jun bundle keeps serving exactly what it served.

Tolerance 1e-6: the golden was recorded on Windows and the CI runs on Linux, and the
TreeSHAP contributions are float32. Still far below anything real: the old corner
swap alone moved the probabilities by more than 1e-4.

Re-recording (only ever with the pre-change code, or the golden proves nothing):
    PYTHONPATH=. .venv/Scripts/python.exe tests/test_predict_golden_27jun.py --record
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import src.prediction.api as api
from src.prediction.features import FighterHistorySummary, build_feature_row
from src.prediction.features.method_features import build_method_feature_row

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = REPO_ROOT / "tests" / "fixtures" / "model_27jun.joblib"
BUNDLE_SHA256 = "6ccb0e5ef99efd5b9389e4aaa45ef3da8f2a1bd6531cec706ac04166a2dd5b4c"
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "predict_golden_27jun.json"
TOLERANCE = 1e-6

RED_ID, BLUE_ID = 1, 2
EMPTY = pd.DataFrame()
WEIGHT_CLASSES = [
    "Lightweight",
    "Welterweight",
    "Women's Strawweight",
    "Heavyweight",
    "Catch Weight",
    None,
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _history(rng: np.random.Generator) -> FighterHistorySummary:
    fights = int(rng.integers(1, 26))
    ko, sub, dec = rng.dirichlet([2.0, 1.0, 2.0])
    losses_ko, losses_sub = rng.dirichlet([1.0, 1.0, 2.0])[:2]
    return FighterHistorySummary(
        total_prior_fights=fights,
        total_rounds_fought=int(fights * rng.uniform(1.5, 2.8)),
        sig_strikes_landed_per_fight=float(rng.uniform(15.0, 90.0)),
        sig_strike_accuracy=float(rng.uniform(0.30, 0.65)),
        knockdowns_per_fight=float(rng.uniform(0.0, 0.8)),
        takedowns_landed_per_fight=float(rng.uniform(0.0, 4.0)),
        takedown_accuracy=float(rng.uniform(0.0, 0.7)),
        submission_attempts_per_fight=float(rng.uniform(0.0, 1.5)),
        control_time_seconds_per_fight=float(rng.uniform(0.0, 400.0)),
        win_streak=int(rng.integers(0, 7)),
        wins_last_5=int(rng.integers(0, min(fights, 5) + 1)),
        pct_wins_by_ko=float(ko),
        pct_wins_by_submission=float(sub),
        pct_wins_by_decision=float(dec),
        days_since_last_fight=int(rng.integers(60, 700)),
        ranking_position=int(rng.integers(1, 16)) if rng.random() < 0.4 else None,
        sig_strikes_absorbed_per_fight=float(rng.uniform(15.0, 80.0)),
        sig_strike_defense=float(rng.uniform(0.40, 0.70)),
        takedowns_absorbed_per_fight=float(rng.uniform(0.0, 3.0)),
        takedown_defense=float(rng.uniform(0.30, 0.95)),
        avg_opponent_prior_win_rate=float(rng.uniform(0.35, 0.75)),
        latest_prior_fight_date=date(2026, 1, 1),
        pct_losses_by_ko=float(losses_ko),
        pct_losses_by_submission=float(losses_sub),
        avg_fight_duration_s=float(rng.uniform(300.0, 900.0)),
        pct_went_the_distance=float(rng.uniform(0.0, 1.0)),
    )


def _physical(rng: np.random.Generator) -> dict[str, float | None]:
    height = float(rng.uniform(160.0, 200.0))
    return {
        "height_cm": height,
        "reach_cm": height + float(rng.uniform(-6.0, 10.0)),
        "age": float(rng.uniform(22.0, 38.0)),
    }


def _record_cases() -> list[dict]:
    """Twenty deterministic matchups covering the shapes serving really sees."""
    rng = np.random.default_rng(20260627)
    cases = []
    for index in range(20):
        red_history: FighterHistorySummary | None = _history(rng)
        blue_history: FighterHistorySummary | None = _history(rng)
        red_phys, blue_phys = _physical(rng), _physical(rng)
        label = "veterans"
        if index in (3, 11):
            blue_history, label = None, "blue debutant"
        elif index == 7:
            red_history, label = None, "red debutant"
        elif index == 15:
            red_history = blue_history = None
            label = "two debutants"
        elif index == 5:
            red_phys["reach_cm"] = None
            blue_phys["age"] = None
            label = "missing reach and age"
        elif index == 9:
            red_history = replace(red_history, takedown_accuracy=None)
            label = "zero takedown attempts"
        elif index == 13:
            blue_history = red_history
            blue_phys = dict(red_phys)
            label = "identical fighters"
        elif index == 17:
            red_history = replace(red_history, ranking_position=3)
            blue_history = replace(blue_history, ranking_position=12)
            label = "both ranked"
        feature_row = build_feature_row(
            red_history,
            blue_history,
            red_height_cm=red_phys["height_cm"],
            blue_height_cm=blue_phys["height_cm"],
            red_reach_cm=red_phys["reach_cm"],
            blue_reach_cm=blue_phys["reach_cm"],
            red_age=red_phys["age"],
            blue_age=blue_phys["age"],
        )
        method_row = build_method_feature_row(
            feature_row,
            red_history,
            blue_history,
            scheduled_rounds=5 if index % 4 == 0 else 3,
            weight_class=WEIGHT_CLASSES[index % len(WEIGHT_CLASSES)],
            is_title_fight=[True, False, None][index % 3],
        )
        if index in (2, 19):
            # A float NaN (not None) in both rows, like a NULL read through pandas.
            for row in (feature_row, method_row):
                row["reach_cm_diff"] = float("nan")
                row["days_since_last_fight_diff"] = float("nan")
            label += " + float NaN"
        cases.append(
            {"label": label, "feature_row": feature_row, "method_row": method_row}
        )
    # Round-trip through JSON so the recorded outputs come from exactly the rows
    # the test will read back.
    return json.loads(json.dumps(cases))


def _load_bundle() -> dict:
    """The 27-jun bundle, loaded exactly like the service loads model.joblib."""
    with patch.object(api, "MODEL_PATH", BUNDLE_PATH):
        return api._load_model_bundle()


def _profiles(_database_url, fighter_ids):
    return {
        fighter_id: api.FighterPredictionProfile(
            id=fighter_id,
            name=f"Fighter {fighter_id}",
            nickname=None,
            headshot_url=None,
            wins=10,
            losses=2,
            draws=0,
            height_cm=None,
            reach_cm=None,
            stance=None,
            latest_weight_class=None,
            aggregate_stats={},
        )
        for fighter_id in fighter_ids
    }


def serve_case(bundle: dict, case: dict) -> dict:
    """The real api.predict on one recorded row; only database reads stubbed."""

    def feature_rows(*_args, **_kwargs):
        context = {"lowConfidence": False, "redHistory": None, "blueHistory": None}
        return dict(case["feature_row"]), dict(case["method_row"]), context, False

    with (
        patch.object(
            api,
            "get_settings",
            lambda: SimpleNamespace(database_url="postgresql://stub.invalid"),
        ),
        patch.object(api, "_load_fighter_physical", lambda _url, ids: {}),
        patch.object(api, "_load_fighter_profiles", _profiles),
        patch.object(api, "_build_feature_row", feature_rows),
    ):
        result = api.predict(
            RED_ID,
            BLUE_ID,
            bundle=bundle,
            fights_df=EMPTY,
            rankings_df=EMPTY,
            history_df=EMPTY,
        )
    return {
        "redProbability": result["redProbability"],
        "blueProbability": result["blueProbability"],
        "topFeatures": result["topFeatures"],
        "featureContributions": result["featureContributions"],
        "methodPrediction": {
            "probabilities": result["methodPrediction"]["probabilities"],
            "predicted": result["methodPrediction"]["predicted"],
        },
    }


def _assert_close(actual, expected, where: str) -> None:
    if expected is None:
        assert actual is None, where
        return
    assert actual is not None, where
    assert math.isfinite(actual), where
    assert abs(actual - expected) <= TOLERANCE, (where, actual, expected)


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_fixture_is_the_27jun_bundle_the_golden_was_recorded_with():
    """Never a skip: if the copy changes, this fails and says so."""
    golden = _golden()
    assert golden["bundle_sha256"] == BUNDLE_SHA256
    assert _sha256(BUNDLE_PATH) == BUNDLE_SHA256
    assert _load_bundle()["trained_at"] == golden["bundle_trained_at"] == "2026-06-27"


def test_golden_covers_the_shapes_serving_sees():
    golden = _golden()
    assert len(golden["cases"]) == 20
    labels = {case["label"] for case in golden["cases"]}
    for shape in ("veterans", "blue debutant", "two debutants", "identical fighters"):
        assert shape in labels
    assert any(
        value is None for case in golden["cases"] for value in case["feature_row"].values()
    )
    assert any(
        isinstance(value, float) and math.isnan(value)
        for case in golden["cases"]
        for value in case["feature_row"].values()
    )


@pytest.mark.parametrize("index", range(20))
def test_27jun_bundle_serves_exactly_the_recorded_prediction(index):
    golden = _golden()
    case = golden["cases"][index]
    expected = case["expected"]

    actual = serve_case(_load_bundle(), case)

    label = case["label"]
    _assert_close(actual["redProbability"], expected["redProbability"], label)
    _assert_close(actual["blueProbability"], expected["blueProbability"], label)
    assert [f["name"] for f in actual["topFeatures"]] == [
        f["name"] for f in expected["topFeatures"]
    ], label
    for got, want in zip(actual["topFeatures"], expected["topFeatures"]):
        assert set(got) == set(want), label
        assert got["direction"] == want["direction"], (label, want["name"])
        _assert_close(got["value"], want["value"], f"{label} {want['name']} value")
        _assert_close(
            got["contribution"], want["contribution"], f"{label} {want['name']}"
        )
    assert list(actual["featureContributions"]) == list(
        expected["featureContributions"]
    ), label
    for name, want in expected["featureContributions"].items():
        _assert_close(actual["featureContributions"][name], want, f"{label} {name}")
    method, want_method = actual["methodPrediction"], expected["methodPrediction"]
    assert method["predicted"] == want_method["predicted"], label
    assert list(method["probabilities"]) == list(want_method["probabilities"]), label
    for method_class, want in want_method["probabilities"].items():
        _assert_close(method["probabilities"][method_class], want, label)


def _record() -> None:
    bundle = _load_bundle()
    cases = _record_cases()
    for case in cases:
        case["expected"] = serve_case(bundle, case)
    golden = {
        "bundle_sha256": _sha256(BUNDLE_PATH),
        "bundle_trained_at": bundle.get("trained_at"),
        "tolerance": TOLERANCE,
        "cases": cases,
    }
    GOLDEN_PATH.write_text(json.dumps(golden, indent=1) + "\n", encoding="utf-8")
    print(f"recorded {len(cases)} cases -> {GOLDEN_PATH}")


if __name__ == "__main__":
    if "--record" not in sys.argv:
        raise SystemExit("usage: tests/test_predict_golden_27jun.py --record")
    _record()
