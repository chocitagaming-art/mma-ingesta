"""FastAPI microservice wrapping the ML prediction model.

The frontend is deploying to Vercel (serverless), where it can no longer spawn a
Python subprocess for /api/predict. This exposes the exact same JSON that
`src/prediction/api.py` already produces over HTTP so the frontend can call it with
`fetch` instead. The natural-language explanation is NOT produced here — the frontend
adds it with the Anthropic SDK; this service only does the ML prediction.

Contract (see PREDICTION_MICROSERVICE_HANDOFF.md):
    GET  /health  -> 200 {"status": "ok"}
    POST /predict  body {"red": <id>, "blue": <id>}
        200 -> PredictionResponse (identical to api.py output, minus explanation*)
        400 -> {"error": "..."}  invalid body / same fighter / unknown id
        422 -> {"error": "Insufficient fighter history"}
        500 -> {"error": "..."}

Performance: the model bundle is loaded once at startup; the fight/ranking dataframes
are cached in-process with a TTL (PREDICTION_DATA_TTL_SECONDS, default 600s) so repeated
predictions don't re-query Neon every time. Set the TTL to 0 to always reload.

Optional auth: if PREDICTION_API_KEY is set, requests must send a matching X-API-Key header.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.prediction.api import InsufficientHistoryError, _load_model_bundle, predict
from src.prediction.features import load_base_dataframe, load_rankings_dataframe
from src.scrapers.config import get_settings
from src.scrapers.db import connect, cursor


LOGGER = logging.getLogger("prediction.service")
logging.basicConfig(level=logging.INFO)

DATA_TTL_SECONDS = float(os.getenv("PREDICTION_DATA_TTL_SECONDS", "600"))
API_KEY = os.getenv("PREDICTION_API_KEY") or None
API_KEY_HEADER = "X-API-Key"

app = FastAPI(title="MMA Prediction Service", version="1.0.0")

# In-process caches: the model never changes at runtime; dataframes refresh on a TTL.
_cache: dict[str, Any] = {"bundle": None, "fights_df": None, "rankings_df": None, "loaded_at": 0.0}


class PredictRequest(BaseModel):
    red: int
    blue: int


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _get_bundle() -> dict[str, Any]:
    if _cache["bundle"] is None:
        LOGGER.info("Loading model bundle")
        _cache["bundle"] = _load_model_bundle()
    return _cache["bundle"]


def _get_dataframes():
    now = time.monotonic()
    stale = _cache["fights_df"] is None or (now - _cache["loaded_at"]) > DATA_TTL_SECONDS
    if stale:
        LOGGER.info("Loading fight/ranking dataframes from the database")
        database_url = get_settings().database_url
        _cache["fights_df"] = load_base_dataframe(database_url)
        _cache["rankings_df"] = load_rankings_dataframe(database_url)
        _cache["loaded_at"] = now
    return _cache["fights_df"], _cache["rankings_df"]


def _existing_fighter_ids(ids: list[int]) -> set[int]:
    database_url = get_settings().database_url
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute("SELECT id FROM fighters WHERE id = ANY(%s)", (ids,))
            return {int(row["id"]) for row in db_cursor.fetchall()}


@app.exception_handler(RequestValidationError)
async def _on_validation_error(_request, _exc: RequestValidationError) -> JSONResponse:
    # Keep 422 reserved for "insufficient history"; malformed bodies are 400.
    return _error(400, 'Invalid request body; expected JSON {"red": <int>, "blue": <int>}')


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/predict")
def predict_endpoint(
    body: PredictRequest,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> Any:
    if API_KEY and x_api_key != API_KEY:
        return _error(401, "Unauthorized")
    if body.red == body.blue:
        return _error(400, "Red and blue fighters must be different")

    existing = _existing_fighter_ids([body.red, body.blue])
    missing = [fighter_id for fighter_id in (body.red, body.blue) if fighter_id not in existing]
    if missing:
        return _error(400, f"Fighter id(s) not found: {missing}")

    try:
        bundle = _get_bundle()
        fights_df, rankings_df = _get_dataframes()
        return predict(
            body.red,
            body.blue,
            bundle=bundle,
            fights_df=fights_df,
            rankings_df=rankings_df,
        )
    except InsufficientHistoryError:
        return _error(422, "Insufficient fighter history")
    except Exception:  # noqa: BLE001 - surface as a clean 500 for the frontend
        LOGGER.exception("Prediction failed for red=%s blue=%s", body.red, body.blue)
        return _error(500, "Internal prediction error")
