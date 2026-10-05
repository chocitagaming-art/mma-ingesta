"""FastAPI microservice wrapping the ML prediction model.

The frontend is deploying to Vercel (serverless), where it can no longer spawn a
Python subprocess for /api/predict. This exposes the exact same JSON that
`src/prediction/api.py` already produces over HTTP so the frontend can call it with
`fetch` instead. The natural-language explanation is NOT produced here — the frontend
adds it with the Anthropic SDK; this service only does the ML prediction.

Contract (see PREDICTION_MICROSERVICE_HANDOFF.md):
    GET  /health[?deep=true]
        200 -> {"status": "ok",
                "db": "up" (deep: Neon answered SELECT 1) | "skipped" (shallow),
                "discardedCalibrators": [...]  only when the bundle load dropped a
                                               calibrator wrapping another model,
                "commit": RENDER_GIT_COMMIT cut to 7 characters, or null,
                "model": {"trainedAt", "featureSet", "nanPolicy", "featureCount"},
                "preUfc": {"needed", "state", "rows", "knownFighters",
                           "loadedAt", "degradedPredictions"}}
               Everything but "db" with deep=true comes from memory: the shallow
               probe never reaches the database nor the loaders (keep-alive every
               10 min, the 18-ago quota outage). preUfc.state is "not_needed" (the
               bundle does not read fight_history_espn), "not_loaded" (it does, no
               /predict has loaded the data yet), "ok" or "unavailable" (the last
               load failed: predictions go out with the block as unknown and are
               counted in degradedPredictions).
        503 -> {"status": "unhealthy"}  the model is not loaded or, with deep=true,
               the DB does not answer.
    POST /predict  body {"red": <id>, "blue": <id>, "fightId": <id> (optional)}
               fightId anchors the prediction to that bout of the two fighters
               (context.anchor "fight"); without it, or when it is not their
               bout, the pending bout ("pending") or today ("today"/"none").
        200 -> PredictionResponse (identical to api.py output, minus explanation*).
               Thin/absent history is still 200 with "lowConfidence": true.
               A non-finite float (NaN/Infinity) goes out as null, and is logged.
        400 -> {"error": "..."}  invalid body / same fighter / unknown id
        401 -> {"error": "Unauthorized"}  when an API key is configured and missing/wrong
        500 -> {"error": "..."}  also when a win probability is not a finite number

Performance: the model bundle is loaded once at startup (fail-fast on a missing or
corrupt model.joblib); the fight/ranking dataframes are cached in-process with a TTL
(PREDICTION_DATA_TTL_SECONDS, default 600s) so repeated predictions don't re-query Neon
every time. Set the TTL to 0 to always reload. When (and only when) the bundle reads
the pre-UFC block, fight_history_espn and the known-history ids are loaded in the
same refresh, with the same TTL; a failure there degrades instead of failing (see
/health above).

Auth: if an API key is configured (PREDICTION_API_KEY or PREDICTION_SERVICE_API_KEY),
requests must send a matching X-API-Key header (compared with hmac.compare_digest). When
PREDICTION_ENV is production/prod the service fails fast at startup unless a key is set;
dev/local runs stay open when no key is configured. In production the interactive docs
(/docs, /redoc) and /openapi.json are disabled; outside production they stay available
as a dev tool.
"""

from __future__ import annotations

import hmac
import logging
import math
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.prediction.api import (
    UNAVAILABLE_PREUFC_HISTORY,
    PreUfcHistory,
    _load_model_bundle,
    load_preufc_history,
    model_trained_at,
    needs_preufc_history,
    predict,
)
from src.prediction.bundle_io import DISCARDED_CALIBRATORS_KEY, winner_training_config
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
WIN_PROBABILITY_KEYS = ("redProbability", "blueProbability")


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
    #
    # minconn=0, NO 1, Y ESTO ES CRITICO. psycopg2 abre `minconn` conexiones en
    # el CONSTRUCTOR del pool y no las cierra mientras viva el proceso. Como el
    # keep-alive mantiene esta instancia de Render despierta 24/7, con minconn=1
    # habia UNA sesion Postgres abierta contra Neon las 24 h del dia, todos los
    # dias. Neon no suspende el computo mientras queden conexiones abiertas, asi
    # que el scale-to-zero no llegaba NUNCA: 0,25 CU x 730 h = ~180 CU-hora al
    # mes contra una cuota de 100. Eso fundio la cuota el 18-ago-2026 y dejo
    # mmastatus.app sin base de datos tres dias.
    #
    # Con minconn=0 el pool nace vacio y solo abre socket cuando entra una
    # peticion real de /predict. El coste es nulo: la primera prediccion tras un
    # rato de calma paga un handshake, que es lo que ya pagaba de todas formas.
    init_pool(
        get_settings().database_url,
        minconn=0,
        maxconn=int(os.getenv("PREDICTION_DB_POOL_MAX", "5")),
    )
    # Load the model eagerly so a missing/corrupt model.joblib crashes startup
    # instead of surfacing as a 500 on the first /predict.
    _get_bundle()
    try:
        yield
    finally:
        close_pool()


def _docs_kwargs() -> dict[str, Any]:
    """Interactive docs are a dev tool: in production the service is API-key-only,
    so /docs, /redoc and /openapi.json are not exposed (same env source of truth
    as the startup fail-fast: PREDICTION_ENV)."""
    if _is_production():
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title="MMA Prediction Service", version="1.0.0", lifespan=_lifespan, **_docs_kwargs()
)

# In-process caches: the model never changes at runtime; dataframes refresh on a TTL.
# preufc_history (fight_history_espn, indexed) refreshes with them, and only for a
# bundle that reads the pre-UFC block; None otherwise.
_cache: dict[str, Any] = {
    "bundle": None,
    "fights_df": None,
    "rankings_df": None,
    "history_df": None,
    "preufc_history": None,
    "loaded_at": 0.0,
}
# Guard the lazy bundle load and the TTL refresh so a burst of concurrent
# requests does a single load instead of a thundering herd of redundant ones.
_bundle_lock = threading.Lock()
_data_lock = threading.Lock()

# What /health reports about the pre-UFC block, kept in memory so the probe never
# has to ask the database. "state" is not_loaded / ok / unavailable (not_needed is
# derived from the bundle at /health time); rows, known_fighters and loaded_at
# describe the data being served (None when there is none);
# degraded_predictions counts the 200s served with the block as unknown because
# the load failed, since the process started.
_PREUFC_STATE_AT_START: dict[str, Any] = {
    "state": "not_loaded",
    "rows": None,
    "known_fighters": None,
    "loaded_at": None,
    "degraded_predictions": 0,
}
_preufc_state: dict[str, Any] = dict(_PREUFC_STATE_AT_START)
_preufc_counter_lock = threading.Lock()


class PredictRequest(BaseModel):
    """Body of POST /predict.

    ``fightId`` is optional: the fight page sends its own fight's id so the
    prediction is anchored to that bout even once it is decided (its own result
    never enters the history); /enfrentamiento sends none. An id that is not
    these two fighters' bout is ignored (see api._get_latest_matchup_context).

    Unknown fields are ignored (pydantic v2's default ``extra='ignore'``, kept on
    purpose): the web may deploy a new field before this service knows it."""

    red: int
    blue: int
    fightId: int | None = None


def _error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": message})


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(value)
    except TypeError:  # None, a string...: not a number at all
        return False


def _json_safe(value: Any, path: str, replaced: list[str]) -> Any:
    """Copy of ``value`` with every non-finite float (NaN, +/-Infinity) as None.

    JSON has no NaN. FastAPI currently hands the endpoint's dict to pydantic,
    whose default quietly writes NaN as null, but Starlette's JSONResponse (every
    ``_error`` path, or a future ``return JSONResponse(...)``) renders with
    ``allow_nan=False``, where a NaN is a 500, and a numpy float32 is a 500 on
    either path. So the payload leaves this service with plain finite floats or
    None only, whatever serializes it; the dotted path of each replacement is
    collected in ``replaced`` for the log."""
    if isinstance(value, dict):
        return {
            key: _json_safe(item, f"{path}.{key}" if path else str(key), replaced)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item, f"{path}[{index}]", replaced)
            for index, item in enumerate(value)
        ]
    if isinstance(value, (float, np.floating)):
        number = float(value)
        if math.isfinite(number):
            return number
        replaced.append(path)
        return None
    return value


def _get_bundle() -> dict[str, Any]:
    if _cache["bundle"] is None:
        with _bundle_lock:
            if _cache["bundle"] is None:
                LOGGER.info("Loading model bundle")
                _cache["bundle"] = _load_model_bundle()
    return _cache["bundle"]


def _bundle_reads_preufc(bundle: Any) -> bool:
    """Whether the served winner bundle reads fight_history_espn. Tolerates the
    partial bundles of the tests (no feature_columns: a pre-phase-4 bundle)."""
    if not isinstance(bundle, dict):
        return False
    return needs_preufc_history(bundle.get("feature_columns") or ())


def _load_preufc_history_or_degrade(database_url: str) -> PreUfcHistory:
    """fight_history_espn for the next TTL window, or UNAVAILABLE_PREUFC_HISTORY.

    A failure here does not fail the refresh: the fights are fine, and the block
    has a meaning for "don't know" (every corner None, the unknown-history rule).
    Predictions keep going out with 200 and are counted as degraded; /health and
    the keep-alive say "unavailable" until a later refresh loads it."""
    try:
        history = load_preufc_history(database_url)
    except Exception:  # noqa: BLE001 - any failure degrades, none is fatal
        LOGGER.exception(
            "Pre-UFC history (fight_history_espn) failed to load: every corner's "
            "block is served as unknown (None) until the next refresh"
        )
        _preufc_state.update(
            state="unavailable", rows=None, known_fighters=None, loaded_at=None
        )
        return UNAVAILABLE_PREUFC_HISTORY
    LOGGER.info(
        "Loaded fight_history_espn: %d rows, %d fighters with known history",
        history.rows,
        len(history.known_fighter_ids),
    )
    _preufc_state.update(
        state="ok",
        rows=history.rows,
        known_fighters=len(history.known_fighter_ids),
        loaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return history


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
            # It is also the population training counts ufc_prev_fights over
            # (build_training_dataset starts from the same load_base_dataframe).
            history_df = build_fighter_history_dataframe(fights_df)
            # Phase 4: fight_history_espn rides on the same refresh and TTL, and
            # only for a bundle that reads it (none with the 27-jun bundle).
            preufc_history = (
                _load_preufc_history_or_degrade(database_url)
                if _bundle_reads_preufc(_get_bundle())
                else None
            )
            _cache["fights_df"] = fights_df
            _cache["rankings_df"] = rankings_df
            _cache["history_df"] = history_df
            _cache["preufc_history"] = preufc_history
            _cache["loaded_at"] = time.monotonic()
    return _cache["fights_df"], _cache["rankings_df"], _cache["history_df"]


def _preufc_history_for(bundle: Any) -> PreUfcHistory | None:
    """What /predict hands to api.predict as ``preufc_history``.

    None when the bundle does not read the block (api.predict never looks at it
    then). Otherwise the copy loaded with the dataframes, or
    UNAVAILABLE_PREUFC_HISTORY when there is none: api.predict must never fall
    back to reading the database per request. Read right after
    _get_dataframes(); a refresh in between only makes this copy one TTL newer
    than the fights, both genuine snapshots."""
    if not _bundle_reads_preufc(bundle):
        return None
    history = _cache["preufc_history"]
    return history if history is not None else UNAVAILABLE_PREUFC_HISTORY


def _count_degraded_prediction() -> None:
    with _preufc_counter_lock:
        _preufc_state["degraded_predictions"] += 1


def _commit() -> str | None:
    """The deployed commit, as Render exposes it (RENDER_GIT_COMMIT), cut to 7."""
    value = (os.getenv("RENDER_GIT_COMMIT") or "").strip()
    return value[:7] or None


def _model_health(bundle: Any) -> dict[str, Any]:
    """The served winner model, from the bundle in memory (no file, no DB)."""
    bundle = bundle if isinstance(bundle, dict) else {}
    config = winner_training_config(bundle)
    feature_columns = bundle.get("feature_columns")
    trained_at = bundle.get("trained_at")
    return {
        "trainedAt": str(trained_at) if trained_at else None,
        "featureSet": config["feature_set"],
        "nanPolicy": config["nan_policy"],
        "featureCount": len(feature_columns) if feature_columns is not None else None,
    }


def _preufc_health(bundle: Any) -> dict[str, Any]:
    state = dict(_preufc_state)
    needed = _bundle_reads_preufc(bundle)
    return {
        "needed": needed,
        "state": state["state"] if needed else "not_needed",
        "rows": state["rows"] if needed else None,
        "knownFighters": state["known_fighters"] if needed else None,
        "loadedAt": state["loaded_at"] if needed else None,
        "degradedPredictions": state["degraded_predictions"],
    }


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
    return _error(
        400,
        'Invalid request body; expected JSON {"red": <int>, "blue": <int>, '
        '"fightId": <int, optional>}',
    )


@app.get("/health")
def health(deep: bool = False) -> JSONResponse:
    """Readiness probe con DOS niveles, y la distincion importa.

    GET /health            -> el modelo esta cargado. NO toca la base de datos.
    GET /health?deep=true  -> ademas, Neon contesta a un SELECT 1.

    Antes solo existia el nivel profundo, y `keepalive-prediction.yml` lo sondeaba
    cada 10 minutos las 24 h para que Render no durmiera la instancia. Cada sonda
    despertaba el computo de Neon, que seguia encendido otros 5 minutos hasta el
    autosuspend: el keep-alive, el solo, mantenia la base viva el mes entero.
    Ahora el ping barato mantiene Render despierto sin costar ni una conexion, y
    el chequeo profundo va una vez por hora.

    Si al cargar el bundle se descarto un calibrador porque envolvia OTRO modelo
    (ver api._load_model_bundle), sigue siendo 200 -- el servicio predice, con el
    modelo correcto y sin calibrar -- pero lo dice en `discardedCalibrators`. El
    campo solo aparece entonces.

    Phase 4 adds `commit`, `model` and `preUfc` (module docstring), all read from
    memory: RENDER_GIT_COMMIT, the bundle already loaded and the state the last
    data refresh left behind. Nothing here may call a loader or the database, so
    right after a deploy preUfc.state is "not_loaded" until the first /predict.
    """
    try:
        bundle = _get_bundle()
        if bundle is None:
            raise RuntimeError("model bundle is not loaded")
        if deep:
            _db_ping()
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        LOGGER.exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "unhealthy"})
    # `db` declara explicitamente que se ha comprobado, para que un 200 superficial
    # no se lea nunca como "Neon va bien".
    content: dict[str, Any] = {"status": "ok", "db": "up" if deep else "skipped"}
    discarded = isinstance(bundle, dict) and bundle.get(DISCARDED_CALIBRATORS_KEY)
    if discarded:
        content["discardedCalibrators"] = list(discarded)
    content["commit"] = _commit()
    content["model"] = _model_health(bundle)
    content["preUfc"] = _preufc_health(bundle)
    return JSONResponse(status_code=200, content=content)


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
        preufc_history = _preufc_history_for(bundle)
        result = predict(
            body.red,
            body.blue,
            bundle=bundle,
            fights_df=fights_df,
            rankings_df=rankings_df,
            history_df=history_df,
            fight_id=body.fightId,
            preufc_history=preufc_history,
        )
        # Expose the model's training date so the UI can show it (#29).
        result["modelTrainedAt"] = model_trained_at(bundle)
        # A win probability that is not a finite number is a model defect, not a
        # data gap, so it never goes out as null: the web's schema would reject
        # the null anyway (blaming the payload's shape) and the cause would be
        # lost. Explicit 500 instead: the web answers 503 and degrades, and does
        # not retry it (only 502/503/504 are retried), which is right for a
        # failure that repeats with the same two fighters.
        broken = {
            key: result.get(key)
            for key in WIN_PROBABILITY_KEYS
            if not _is_finite_number(result.get(key))
        }
        if broken:
            LOGGER.error(
                "Non-finite win probability for red=%s blue=%s fightId=%s: %s",
                body.red,
                body.blue,
                body.fightId,
                broken,
            )
            return _error(500, "Internal prediction error")
        # Anything else non-finite goes out as null, named in the log.
        replaced: list[str] = []
        payload = _json_safe(result, "", replaced)
        if replaced:
            LOGGER.warning(
                "Non-finite floats sent as null for red=%s blue=%s fightId=%s: %s",
                body.red,
                body.blue,
                body.fightId,
                ", ".join(replaced),
            )
        # Served, but with the pre-UFC block as unknown because its load failed:
        # counted for /health (the ERROR went to the log once, at load time).
        if preufc_history is UNAVAILABLE_PREUFC_HISTORY:
            _count_degraded_prediction()
        return payload
    except Exception:  # noqa: BLE001 - surface as a clean 500 for the frontend
        LOGGER.exception(
            "Prediction failed for red=%s blue=%s fightId=%s", body.red, body.blue, body.fightId
        )
        return _error(500, "Internal prediction error")
