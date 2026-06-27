# Prediction microservice image (FastAPI + XGBoost model). Works on fly.io and Railway.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libgomp1 is the OpenMP runtime XGBoost needs at import time on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-service.txt .
RUN pip install --no-cache-dir -r requirements-service.txt

# Source (includes src/prediction/model.joblib, which is committed to the repo).
COPY src ./src

# Drop privileges: nothing is written at runtime (model.joblib is read-only), so
# a non-root user is enough. Added after install + COPY so the files it needs are
# already in place and world-readable.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Container-level readiness probe hitting the real /health endpoint (which checks
# the model is loaded and the DB answers). 503 -> urlopen raises -> unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)"

# PORT is injected by fly.io / Railway; default to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn src.prediction.service:app --host 0.0.0.0 --port ${PORT:-8080}"]
