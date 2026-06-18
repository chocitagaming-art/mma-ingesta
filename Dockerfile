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

EXPOSE 8080
# PORT is injected by fly.io / Railway; default to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn src.prediction.service:app --host 0.0.0.0 --port ${PORT:-8080}"]
