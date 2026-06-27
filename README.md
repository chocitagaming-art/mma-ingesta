# mma-ingesta

The data and machine learning half of [MMA STATUS](https://mma-app-ruby.vercel.app). It scrapes UFC and ESPN, keeps a PostgreSQL database current, trains the fight prediction model, and serves predictions over a small FastAPI service.

![Python](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python)
![XGBoost](https://img.shields.io/badge/XGBoost-model-ff6600?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-service-009688?style=flat-square&logo=fastapi)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-4169e1?style=flat-square&logo=postgresql)
![Tests](https://img.shields.io/badge/tests-113%20passing-22c55e?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)

Read this in: **English** · [Español](./README.es.md)

## What it is

This repo does three jobs:

1. **Scrape and clean.** Pull fighters, fights, stats, events, and rankings from UFC and ESPN. Pull odds from The Odds API and fight videos from YouTube. Write it all to one PostgreSQL database on Neon.
2. **Train a model.** Turn the raw fight history into features and train an XGBoost classifier that predicts who wins a matchup.
3. **Serve predictions.** Run a FastAPI service that takes two fighter ids and returns a calibrated probability, the signals behind it, and the features that moved the call.

The web app ([mma-app](https://github.com/chocitagaming-art/mma-app)) reads the same database and calls the prediction service. It never writes. All writes happen here.

## The model

- Trained **only on fighter stats**: records, physical attributes, striking, grappling, form, and quality of opposition. Odds are never a feature. Every feature is a red minus blue difference, so corner order does not leak.
- Accuracy is about **63%** (0.6289), with a Brier score around 0.226. The model is calibrated out of sample and symmetrized, so the number you see in production is the honest one, not the optimistic training number.
- It is benchmarked against a majority class baseline, so "is it actually learning anything" has an answer.
- Thin histories (debutants) are flagged as low confidence instead of being given a fake favorite.
- The feature mapping has one source of truth (`build_feature_row`), shared by training and serving, so the two cannot drift apart. Golden and parity tests pin it.

The current model card lives in [`src/prediction/model_metrics.md`](./src/prediction/model_metrics.md).

## The prediction service

A FastAPI app in `src/prediction/service.py`. It loads the model and a fighter history dataframe once, then answers:

- `POST /predict` with two fighter ids, returning probabilities, per corner signals, and signed feature attribution.
- `GET /health` for a real readiness check (model loaded plus a database ping).

It is hardened for production (auth, connection pooling, graceful degradation) but is not deployed in this setup. It runs locally during development, and the web app handles its absence quietly.

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
python -m pytest tests/ -q   # 113 tests
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

## Roadmap

Done so far:

- [x] Scrapers for UFC and ESPN, plus odds, news, and videos
- [x] Feature pipeline with a single source of truth shared by training and serving
- [x] Calibrated XGBoost model with honest baselines and low confidence handling
- [x] FastAPI prediction service with signed feature attribution
- [x] Scheduled refresh and CI on GitHub Actions
- [x] `features.py` split into a small `features/` package

Planned:

- [ ] Deploy the prediction service (Render or Railway) so production predictions go live
- [ ] Backfill historical rankings and bout order for stronger features
- [ ] A read-only database role for the web app
- [ ] Anchor head to head signals to "today" for fighters who already met

## The other repo

[**mma-app**](https://github.com/chocitagaming-art/mma-app) is the Next.js site that turns this data into a live product. The screenshots and the full feature list are over there.

## License

MIT. See [LICENSE](./LICENSE). This is a personal project, but issues and pull requests are welcome.
