"""Endpoint tests for the prediction microservice (FastAPI TestClient).

Everything that would touch Neon or load the real model is stubbed at the module
level, so these run fully offline and deterministic: no real database, no real
model.joblib. We deliberately build the TestClient WITHOUT the `with` block so the
lifespan (which opens the pool and eager-loads the model) never fires.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

import src.prediction.service as service


def _fake_predict(low_confidence: bool = False):
    def _predict(red, blue, **_kwargs):
        return {
            "redProbability": 0.6,
            "blueProbability": 0.4,
            "topFeatures": [],
            "featureContributions": {},
            "featureValues": {},
            "methodPrediction": {
                "probabilities": {"decision": 0.5, "ko": 0.3, "submission": 0.2},
                "predicted": "decision",
                "trainedAt": "2026-07-19",
            },
            "context": {"lowConfidence": low_confidence},
            "lowConfidence": low_confidence,
            "fighters": {"red": {"id": red}, "blue": {"id": blue}},
        }

    return _predict


@pytest.fixture
def client(monkeypatch):
    # Both fighters exist; bundle + dataframes are stubs (predict is mocked too).
    monkeypatch.setattr(service, "_existing_fighter_ids", lambda ids: set(ids))
    monkeypatch.setattr(service, "_get_bundle", lambda: {"trained_at": "2026-06-25"})
    monkeypatch.setattr(service, "_get_dataframes", lambda: (None, None, None))
    monkeypatch.setattr(service, "predict", _fake_predict())
    # Auth open by default; no prod flag.
    for name in service.API_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PREDICTION_ENV", raising=False)
    return TestClient(service.app)


def test_predict_happy_path_is_200(client):
    response = client.post("/predict", json={"red": 1, "blue": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["redProbability"] == 0.6
    assert body["lowConfidence"] is False
    assert body["modelTrainedAt"] == "2026-06-25"


def test_predict_passes_method_prediction_through(client):
    # The service forwards api.predict()'s payload untouched, so the method
    # block must arrive exactly as produced (and stay optional for old bundles).
    response = client.post("/predict", json={"red": 1, "blue": 2})
    assert response.status_code == 200
    method = response.json()["methodPrediction"]
    assert method["predicted"] == "decision"
    assert set(method["probabilities"]) == {"decision", "ko", "submission"}


def test_predict_same_fighter_is_400(client):
    response = client.post("/predict", json={"red": 5, "blue": 5})
    assert response.status_code == 400
    assert "different" in response.json()["error"].lower()


def test_predict_malformed_body_is_400(client):
    response = client.post("/predict", json={"red": "not-an-int"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_predict_unknown_fighter_is_400(client, monkeypatch):
    monkeypatch.setattr(service, "_existing_fighter_ids", lambda ids: {1})
    response = client.post("/predict", json={"red": 1, "blue": 999})
    assert response.status_code == 400
    assert "999" in response.json()["error"]


def test_predict_thin_or_missing_history_is_200_low_confidence(client, monkeypatch):
    monkeypatch.setattr(service, "predict", _fake_predict(low_confidence=True))
    response = client.post("/predict", json={"red": 1, "blue": 2})
    assert response.status_code == 200
    assert response.json()["lowConfidence"] is True


def test_predict_enforces_api_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("PREDICTION_API_KEY", "s3cret")
    assert client.post("/predict", json={"red": 1, "blue": 2}).status_code == 401
    assert (
        client.post(
            "/predict", json={"red": 1, "blue": 2}, headers={"X-API-Key": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/predict", json={"red": 1, "blue": 2}, headers={"X-API-Key": "s3cret"}
        ).status_code
        == 200
    )


def test_predict_accepts_frontend_env_key_name(client, monkeypatch):
    # The frontend ships PREDICTION_SERVICE_API_KEY; it must be honoured too.
    monkeypatch.setenv("PREDICTION_SERVICE_API_KEY", "frontkey")
    assert (
        client.post(
            "/predict", json={"red": 1, "blue": 2}, headers={"X-API-Key": "frontkey"}
        ).status_code
        == 200
    )
    assert client.post("/predict", json={"red": 1, "blue": 2}).status_code == 401


def test_health_ok_when_model_and_db_ready(client, monkeypatch):
    monkeypatch.setattr(service, "_get_bundle", lambda: {"ready": True})
    monkeypatch.setattr(service, "_db_ping", lambda: None)
    response = client.get("/health?deep=true")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "up"}


def test_shallow_health_never_touches_the_database(client, monkeypatch):
    """El ping barato NO puede abrir conexion a Neon.

    Es la guarda de regresion del apagon del 18-ago-2026: keepalive-prediction.yml
    sondea /health cada 10 minutos las 24 h, y mientras ese sondeo tocaba la base,
    el computo de Neon no se suspendia nunca y se comia la cuota del mes entera.
    Si alguien vuelve a meter el _db_ping en el camino superficial, este test cae.
    """
    monkeypatch.setattr(service, "_get_bundle", lambda: {"ready": True})

    def boom():  # pragma: no cover - debe ser inalcanzable
        raise AssertionError("/health superficial no debe tocar la base de datos")

    monkeypatch.setattr(service, "_db_ping", boom)

    response = client.get("/health")
    assert response.status_code == 200
    # "skipped" y no "up": el 200 superficial no afirma nada sobre Neon.
    assert response.json() == {"status": "ok", "db": "skipped"}


@pytest.fixture
def client_for_env(monkeypatch):
    """Build a TestClient with PREDICTION_ENV set (or cleared with None) BEFORE the
    app exists: docs/openapi exposure is decided when the FastAPI app is created,
    so the module must be reloaded for the env var to take effect."""

    def _build(env):
        if env is None:
            monkeypatch.delenv("PREDICTION_ENV", raising=False)
        else:
            monkeypatch.setenv("PREDICTION_ENV", env)
        importlib.reload(service)
        return TestClient(service.app)

    yield _build
    # Rebuild the module-level app without the patched env so later tests see the
    # normal dev app (docs enabled) again.
    monkeypatch.delenv("PREDICTION_ENV", raising=False)
    importlib.reload(service)


def test_docs_and_openapi_closed_in_production(client_for_env):
    prod_client = client_for_env("production")
    assert prod_client.get("/docs").status_code == 404
    assert prod_client.get("/redoc").status_code == 404
    assert prod_client.get("/openapi.json").status_code == 404


def test_docs_and_openapi_open_outside_production(client_for_env):
    dev_client = client_for_env(None)
    assert dev_client.get("/docs").status_code == 200
    assert dev_client.get("/openapi.json").status_code == 200


def test_health_unhealthy_when_db_unreachable(client, monkeypatch):
    monkeypatch.setattr(service, "_get_bundle", lambda: {"ready": True})

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "_db_ping", boom)
    response = client.get("/health?deep=true")
    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy"}
