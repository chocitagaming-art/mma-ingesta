"""FastAPI microservice wrapping the ML prediction model.

The frontend is deploying to Vercel (serverless), where it can no longer spawn a
Python subprocess for /api/predict. This exposes the exact same JSON that
`src/prediction/api.py` already produces over HTTP so the frontend can call it with
`fetch` instead. The natural-language explanation is NOT produced here — the frontend
adds it with the Anthropic SDK; this service only does the ML prediction.

Contract (see PREDICTION_MICROSERVICE_HANDOFF.md):
    GET  /health  -> 200 {"status": "ok"} when the model is loaded and the DB
                     answers; 503 {"status": "unhealthy"} otherwise.
    POST /predict  body {"red": <id>, "blue": <id>}
        200 -> PredictionResponse (identical to api.py output, minus explanation*).
               Thin/absent history is still 200 with "lowConfidence": true.
        400 -> {"error": "..."}  invalid body / same fighter / unknown id
        401 -> {"error": "Unauthorized"}  when an API key is configured and missing/wrong
        500 -> {"error": "..."}

Performance: the model bundle is loaded once at startup (fail-fast on a missing or
corrupt model.joblib); the fight/ranking dataframes are cached in-process with a TTL
(PREDICTION_DATA_TTL_SECONDS, default 600s) so repeated predictions don't re-query Neon
every time. Set the TTL to 0 to always reload.

Auth: if an API key is configured (PREDICTION_API_KEY or PREDICTION_SERVICE_API_KEY),
requests must send a matching X-API-Key header (compared with hmac.compare_digest). When
PREDICTION_ENV is production/prod the service fails fast at startup unless a key is set;
dev/local runs stay open when no key is configured.
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.prediction.api import (
    _load_model_bundle,
    model_trained_at,
    predict,
)
from src.prediction.features import (
    build_fighter_history_dataframe,
    load_base_dataframe,
    load_rankings_dataframe,
)
from src.scrapers.config import get_settings
from src.scrapers.db import close_pool, connect, cursor, init_pool


LOGGER = logging.getLogger("prediction.service")
logging.basicConfig(level=logging.INFO)

DATA_TTL_SECONDS = float(os.getenv("PREDICTION_DATA_TTL_SECONDS", "600"))
API_KEY_HEADER = "X-API-Key"
# The frontend ships PREDICTION_SERVICE_API_KEY; older configs use PREDICTION_API_KEY.
API_KEY_ENV_NAMES = ("PREDICTION_API_KEY", "PREDICTION_SERVICE_API_KEY")


def _resolve_api_key() -> str | None:
    """Configured API key from either accepted env name (None -> auth disabled)."""
    for name in API_KEY_ENV_NAMES:
        value = os.getenv(name)
        if value:
            return value
    return None


def _is_production() -> bool:
    return os.getenv("PREDICTION_ENV", "").strip().lower() in {"production", "prod"}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Fail fast in production if auth is misconfigured: an open prediction endpoint
    # in prod is a bug, not a default. Dev/local stay open when no key is set.
    if _is_production() and _resolve_api_key() is None:
        raise RuntimeError(
            "PREDICTION_ENV is production but no API key is set "
            "(PREDICTION_API_KEY or PREDICTION_SERVICE_API_KEY)."
        )
    # Open the shared Neon pool once so bursts of /predict reuse a handful of
    # sockets instead of opening 3 connections per request (and exhausting the
    # free-tier connection slots). Pool size is overridable via env.
    init_pool(
        get_settings().database_url,
        minconn=1,
        maxconn=int(os.getenv("PREDICTION_DB_POOL_MAX", "5")),
    )
    # Load the model eagerly so a missing/corrupt model.joblib crashes startup
    # instead of surfacing as a 500 on the first /predict.
    _get_bundle()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="MMA Prediction Service", version="1.0.0", lifespan=_lifespan
)

# In-process caches: the model never changes at runtime; dataframes refresh on a TTL.
_cache: dict[str, Any] = {
    "bundle": None,
    "fights_df": None,
    "rankings_df": None,
    "history_df": None,
    "loaded_at": 0.0,
}
# Guard the lazy bundle load and the TTL refresh so a burst of concurrent
# requests does a single load instead of a thundering herd of redundant ones.
_bundle_lock = threading.Lock()
_data_lock = threading.Lock()


class PredictRequest(BaseModel):
    red: int
    blue: int


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _get_bundle() -> dict[str, Any]:
    if _cache["bundle"] is None:
        with _bundle_lock:
            if _cache["bundle"] is None:
                LOGGER.info("Loading model bundle")
                _cache["bundle"] = _load_model_bundle()
    return _cache["bundle"]


def _get_dataframes():
    now = time.monotonic()
    fresh = _cache["fights_df"] is not None and (now - _cache["loaded_at"]) <= DATA_TTL_SECONDS
    if fresh:
        return _cache["fights_df"], _cache["rankings_df"], _cache["history_df"]
    with _data_lock:
        # Double-check: another thread may have refreshed while we waited.
        now = time.monotonic()
        stale = _cache["fights_df"] is None or (now - _cache["loaded_at"]) > DATA_TTL_SECONDS
        if stale:
            LOGGER.info("Loading fight/ranking dataframes from the database")
            database_url = get_settings().database_url
            fights_df = load_base_dataframe(database_url)
            rankings_df = load_rankings_dataframe(database_url)
            # Derive the per-fighter history once per refresh and reuse it across
            # every prediction until the TTL expires (it is O(all fights) to build).
            history_df = build_fighter_history_dataframe(fights_df)
            _cache["fights_df"] = fights_df
            _cache["rankings_df"] = rankings_df
            _cache["history_df"] = history_df
            _cache["loaded_at"] = time.monotonic()
    return _cache["fights_df"], _cache["rankings_df"], _cache["history_df"]


def _db_ping() -> None:
    """Cheap round trip proving the pool can reach the database."""
    database_url = get_settings().database_url
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute("SELECT 1")
            db_cursor.fetchone()


def _existing_fighter_ids(ids: list[int]) -> set[int]:
    database_url = get_settings().database_url
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute("SELECT id FROM fighters WHERE id = ANY(%s)", (ids,))
            return {int(row["id"]) for row in db_cursor.fetchall()}


@app.exception_handler(RequestValidationError)
async def _on_validation_error(_request, _exc: RequestValidationError) -> JSONResponse:
    # Malformed bodies are a client error: remap FastAPI's default 422 to 400 so
    # the only 4xx the frontend sees from a bad body is a plain 400.
    return _error(400, 'Invalid request body; expected JSON {"red": <int>, "blue": <int>}')


@app.get("/health")
def health() -> JSONResponse:
    # Real readiness probe: the service is healthy only if the model is loaded
    # AND the database answers a trivial query through the pool.
    try:
        if _get_bundle() is None:
            raise RuntimeError("model bundle is not loaded")
        _db_ping()
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        LOGGER.exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "unhealthy"})
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.post("/predict")
def predict_endpoint(
    body: PredictRequest,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> Any:
    api_key = _resolve_api_key()
    if api_key is not None and not hmac.compare_digest(
        (x_api_key or "").encode("utf-8"), api_key.encode("utf-8")
    ):
        return _error(401, "Unauthorized")
    if body.red == body.blue:
        return _error(400, "Red and blue fighters must be different")

    try:
        # Inside the try so a DB failure (dropped Neon connection, pool wait
        # timeout) surfaces as a clean 500 instead of an off-contract default error.
        existing = _existing_fighter_ids([body.red, body.blue])
        missing = [fighter_id for fighter_id in (body.red, body.blue) if fighter_id not in existing]
        if missing:
            return _error(400, f"Fighter id(s) not found: {missing}")

        bundle = _get_bundle()
        fights_df, rankings_df, history_df = _get_dataframes()
        result = predict(
            body.red,
            body.blue,
            bundle=bundle,
            fights_df=fights_df,
            rankings_df=rankings_df,
            history_df=history_df,
        )
        # Expose the model's training date so the UI can show it (#29).
        result["modelTrainedAt"] = model_trained_at(bundle)
        return result
    except Exception:  # noqa: BLE001 - surface as a clean 500 for the frontend
        LOGGER.exception("Prediction failed for red=%s blue=%s", body.red, body.blue)
        return _error(500, "Internal prediction error")
