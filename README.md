<div align="center">

<img src="docs/banner.png" alt="MMA STATUS" width="640">

# mma-ingesta

The data and machine learning half of **[MMA STATUS](https://mmastatus.app)**.

![Python](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-4169e1?style=flat-square&logo=postgresql)
![FastAPI](https://img.shields.io/badge/FastAPI-service-009688?style=flat-square&logo=fastapi)
[![CI](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml/badge.svg)](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml)

English · [Español](./README.es.md)

</div>

## What this is

This repository keeps the database behind MMA STATUS alive: it collects
fighters, bouts, statistics, events and rankings, normalises them, and trains
the model that predicts matchups.

The web app ([mma-app](https://github.com/chocitagaming-art/mma-app)) only
**reads**. Every write goes through here.

## What it does

- **Collects and cleans MMA data** from public sources and keeps it consistent:
  one profile per fighter, no duplicate bouts, results that complete themselves
  once the official source publishes the detail.
- **Trains a model** that estimates each corner's win probability from fight
  history. Public card in
  [`MODEL_CARD.md`](./src/prediction/MODEL_CARD.md).
- **Serves predictions** to the website through a microservice.
- **Runs itself**: scheduled jobs refresh rankings, upcoming cards, odds and
  news unattended, and raise an alert when something breaks.

## During a live event

The part this project is proudest of: while a card is running, a process samples
the fight every few seconds and builds **the film of the bout** — how the fight
evolves minute by minute, not just the final result.

That data **cannot be recovered afterwards**: if it isn't captured live, it is
gone for good. Hence the dedicated watchdog, the chained relay job and the
parallel backup capture.

## How it fits together

```
Public MMA sources
        │
        ▼
   mma-ingesta  ──writes──▶  PostgreSQL (Neon)  ◀──reads──  mma-app (web)
        │                                                       │
        └──▶  prediction microservice  ◀────────calls───────────┘
```

Python 3.12, PostgreSQL on Neon, FastAPI and a gradient boosting classifier.
Scheduled jobs run on GitHub Actions.

## Quality

- **704 tests** running on every push.
- Golden and parity tests that pin the model's inputs, so a refactor or a
  retrain cannot change them silently.
- Corner symmetry and data leakage checks.
- Anything that can modify data starts in **dry-run mode** and requires
  `--apply` to actually write.
- Daily database backup, **restored and verified table by table on every run**,
  with a written recovery plan.

## Layout

```
src/
  scrapers/      # data collection and cleaning
  prediction/    # model and prediction microservice
tests/           # pytest suite
```

## Running locally

You need Python 3.12 and a PostgreSQL database with the project schema.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-scrapers.txt
python -m pytest tests/ -q
```

Environment variable names are listed in [`.env.example`](./.env.example).

## Data and license

The data this software collects is **not owned by this project**: it comes from
third-party sources, each with its own terms of use. This repository does not
redistribute it.

The code is **source-visible, not open source**: you may read and study it, but
not deploy it or use it commercially without permission. See
[LICENSE](./LICENSE).

## The other repository

[**mma-app**](https://github.com/chocitagaming-art/mma-app) is the website that
turns this data into a live product.
