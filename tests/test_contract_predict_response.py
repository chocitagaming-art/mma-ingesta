"""Shared contract for the POST /predict response.

The two repos ship their own CI, so a rename inside ``api.predict`` passes green
here and silently breaks the web: mma-app would read a key that no longer exists
and paint a hole or "NaN%" with no error anywhere. That gap is what this file
closes, from the producer's side.

``tests/contracts/predict_response.json`` is the canonical payload, captured from
the live service. mma-app keeps a byte-identical copy under
``src/lib/__fixtures__/predict-response.json`` and validates it against its own
parser, so both ends are pinned to the same document.

If a test here fails you are changing the contract. That is allowed — just do it
deliberately: update the fixture, copy it to mma-app, and check that its parser
still accepts it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

CONTRACT_PATH = Path(__file__).parent / "contracts" / "predict_response.json"
API_PATH = Path(__file__).parent.parent / "src" / "prediction" / "api.py"
SERVICE_PATH = Path(__file__).parent.parent / "src" / "prediction" / "service.py"
# Sibling repo. Absent on CI runners, which only check out one repo.
APP_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent / "mma-app" / "src" / "lib" / "__fixtures__" / "predict-response.json"
)


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _predict_response_keys() -> set[str]:
    """Literal keys of the dict ``api.predict`` returns, read from the AST.

    Parsed rather than called because ``predict`` needs the database and the
    model bundle. The point is to catch a rename at the source, so reading the
    source is exactly right."""
    tree = ast.parse(API_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "predict"
    )
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            return {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("api.predict no longer returns a dict literal")


def _service_added_keys() -> set[str]:
    """Keys the FastAPI layer bolts on after calling ``predict``.

    The wire response is not just ``predict``'s dict: service.py enriches it
    (``result["modelTrainedAt"] = ...``), so the contract has to cover both or
    the comparison below would flag a legitimate field as missing."""
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "result"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                keys.add(target.slice.value)
    return keys


def test_contract_matches_what_the_service_returns(contract):
    """Every key that reaches the wire is in the contract, and vice versa."""
    emitted = _predict_response_keys() | _service_added_keys()
    assert emitted == set(contract), (
        "the service and tests/contracts/predict_response.json disagree. If the "
        "change is intended, update the fixture AND its copy in mma-app."
    )


def test_contract_carries_the_fields_the_web_reads(contract):
    """Spot-check the payload the UI cannot render without."""
    assert isinstance(contract["redProbability"], float)
    assert isinstance(contract["blueProbability"], float)
    assert contract["redProbability"] + contract["blueProbability"] == pytest.approx(1.0, abs=1e-9)
    assert isinstance(contract["lowConfidence"], bool)

    assert contract["topFeatures"], "topFeatures empty: the factor bars would vanish"
    for feature in contract["topFeatures"]:
        assert set(feature) == {"name", "value", "contribution", "direction"}
        assert feature["direction"] in {"red", "blue"}

    method = contract["methodPrediction"]
    assert set(method["probabilities"]) == {"decision", "ko", "submission"}
    assert sum(method["probabilities"].values()) == pytest.approx(1.0, abs=1e-3)
    assert method["predicted"] in method["probabilities"]

    for corner in ("red", "blue"):
        assert {"id", "name"} <= set(contract["fighters"][corner])
    assert {"matchupDate", "weightClass"} <= set(contract["context"])


@pytest.mark.skipif(not APP_CONTRACT_PATH.exists(), reason="mma-app no está junto a este repo")
def test_both_repos_hold_the_same_contract():
    """The two copies must not drift apart.

    Only runs when both repos sit side by side (a dev machine, or the megatest).
    CI checks out one repo at a time, so there it skips.
    """
    assert CONTRACT_PATH.read_text(encoding="utf-8") == APP_CONTRACT_PATH.read_text(
        encoding="utf-8"
    ), (
        "the contract fixtures have diverged: copy "
        f"{CONTRACT_PATH} over {APP_CONTRACT_PATH}"
    )
