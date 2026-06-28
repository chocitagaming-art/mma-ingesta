<div align="center">

<img src="docs/banner.png" alt="MMA STATUS" width="640">

# mma-ingesta

The data and machine learning half of [MMA STATUS](https://mma-app-ruby.vercel.app). It scrapes UFC and ESPN, keeps a PostgreSQL database current, trains the fight prediction model, and serves predictions over a small FastAPI service.

![Python](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python)
![XGBoost](https://img.shields.io/badge/XGBoost-model-ff6600?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-service-009688?style=flat-square&logo=fastapi)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-4169e1?style=flat-square&logo=postgresql)
[![CI](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml/badge.svg)](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)

[![Stars](https://img.shields.io/github/stars/chocitagaming-art/mma-ingesta?style=flat-square&color=ef4444)](https://github.com/chocitagaming-art/mma-ingesta/stargazers)
![Forks](https://img.shields.io/github/forks/chocitagaming-art/mma-ingesta?style=flat-square&color=ef4444)
![Last commit](https://img.shields.io/github/last-commit/chocitagaming-art/mma-ingesta?style=flat-square&color=ef4444)

English · [Español](./README.es.md)

</div>

## What it is

This repo does three jobs:

1. **Scrape and clean.** Pull fighters, fights, stats, events, and rankings from UFC and ESPN. Pull odds from The Odds API and fight videos from YouTube. Write it all to one PostgreSQL database on Neon.
2. **Train a model.** Turn the raw fight history into features and train an XGBoost classifier that predicts who wins a matchup.
3. **Serve predictions.** Run a FastAPI service that takes two fighter ids and returns a calibrated probability, the signals behind it, and the features that moved the call.

The web app ([mma-app](https://github.com/chocitagaming-art/mma-app)) reads the same database and calls the prediction service. It never writes. All writes happen here.

## The model

- Trained on fighter stats only: records, physical attributes, striking, grappling, form, and quality of opposition, mapped into 20 features. Odds are never an input to the model. Every feature is a red minus blue difference, so corner order does not leak.
- Accuracy is about 63% (0.6289), with a Brier score of 0.2266. The reported metric is the out-of-sample calibrated one, not the optimistic training number, and the model is symmetrized so both corners are scored the same way.
- It is built on a dataset of roughly 2,838 fighters and 8,750 fights.
- It is benchmarked against a majority class baseline, so "is it actually learning anything" has an answer.
- Thin histories (debutants) are flagged as low confidence instead of being given a fake favorite.
- The feature mapping has one source of truth (`build_feature_row`), shared by training and serving, so the two cannot drift apart. Golden and parity tests pin it.

The current model card lives in [`src/prediction/model_metrics.md`](./src/prediction/model_metrics.md).

## The prediction service

A FastAPI app in `src/prediction/service.py`. It loads the model and a fighter history dataframe once, then answers:

- `POST /predict` with two fighter ids, returning probabilities, per corner signals, and signed feature attribution.
- `GET /health` for a real readiness check (model loaded plus a database ping).

It runs with API key auth and a shared connection pool, and the web app calls it to serve predictions.

Example request:

```bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $PREDICTION_SERVICE_API_KEY" \
  -d '{"red": 1234, "blue": 5678}'
```

Response (trimmed):

```json
{
  "redProbability": 0.63,
  "blueProbability": 0.37,
  "lowConfidence": false,
  "topFeatures": [
    { "name": "striking_accuracy_diff", "direction": "red" }
  ],
  "modelTrainedAt": "2026-06-01"
}
```

## Data sources

| Data | Source |
|------|--------|
| Fighters, fights, stats, events, rankings | UFC and ESPN scrapers |
| Odds | The Odds API (upcoming events only) |
| Videos | YouTube Data API (official UFC channels) |
| Predictions | XGBoost model trained here |

## Tech stack

Python 3.12, PostgreSQL on Neon, XGBoost, scikit-learn, pandas, FastAPI, BeautifulSoup, `psycopg`. Scheduled refresh runs on GitHub Actions.

## Run it locally

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-scrapers.txt -r requirements-service.txt
```

Copy the variable names from [`.env.example`](./.env.example) into a `.env`:

- `DATABASE_URL` (required)
- `ANTHROPIC_API_KEY`, `YOUTUBE_API_KEY`, `ODDS_API_KEY`
- `PREDICTION_ENV`, `PREDICTION_DB_POOL_MAX`, `PREDICTION_DATA_TTL_SECONDS`, `PREDICTION_SERVICE_API_KEY`

### Scrapers

The scrapers are Python modules under `src/scrapers`, run with `python -m`. Anything that can change data defaults to a dry run and needs `--apply` to actually write, so you can always see what a script would do first.

### Train the model

The pipeline runs in four steps:

```bash
python -m src.prediction.features    # build the training dataset from the database
python -m src.prediction.train       # train the XGBoost model
python -m src.prediction.calibrate   # fit the out-of-sample calibrator
python -m src.prediction.evaluate    # report calibrated, production-like metrics
```

Back up `src/prediction/model.joblib` before retraining.

### Serve predictions

```bash
python -m uvicorn src.prediction.service:app --port 8000
# GET http://localhost:8000/health  ->  {"status":"ok"}
```

## Scheduled refresh

GitHub Actions keeps the data current on a schedule, in `.github/workflows`:

- `refresh-rankings.yml`
- `refresh-upcoming.yml`
- `refresh-odds.yml`
- `refresh-news.yml`

`ci.yml` runs the tests and lint on every push.

## Tests

```bash
python -m pytest tests/ -q
```

The suite includes golden and parity tests that lock the model features, plus corner symmetry and leak checks, so a refactor or a retrain cannot silently change the inputs.

## Project layout

```
src/
  scrapers/      # UFC + ESPN scrapers, odds, news, videos, data cleanup, CLI
  prediction/    # ML pipeline
    features/    # feature engineering (data loading, history, build_feature_row)
    train.py     # train the model
    calibrate.py # out-of-sample calibration
    evaluate.py  # production-like metrics
    service.py   # FastAPI prediction service
    api.py       # prediction logic and feature attribution
    model.joblib # the trained model
tests/           # pytest suite
```

## The other repo

[**mma-app**](https://github.com/chocitagaming-art/mma-app) is the Next.js site that turns this data into a live product. The screenshots and the full feature list are over there.

## License

MIT. See [LICENSE](./LICENSE). This is a personal project, but issues and pull requests are welcome.
