<div align="center">

<img src="docs/banner.png" alt="MMA STATUS" width="640">

# mma-ingesta

La mitad de datos y machine learning de [MMA STATUS](https://mma-app-ruby.vercel.app). Scrapea UFC y ESPN, mantiene al día una base de datos PostgreSQL, entrena el modelo de predicción de peleas y sirve las predicciones a través de un microservicio FastAPI.

![Python](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python)
![XGBoost](https://img.shields.io/badge/XGBoost-modelo-ff6600?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-servicio-009688?style=flat-square&logo=fastapi)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-4169e1?style=flat-square&logo=postgresql)
[![CI](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml/badge.svg)](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml)
![Licencia](https://img.shields.io/badge/licencia-MIT-blue?style=flat-square)

[![Estrellas](https://img.shields.io/github/stars/chocitagaming-art/mma-ingesta?style=flat-square&color=ef4444)](https://github.com/chocitagaming-art/mma-ingesta/stargazers)
![Forks](https://img.shields.io/github/forks/chocitagaming-art/mma-ingesta?style=flat-square&color=ef4444)
![Último commit](https://img.shields.io/github/last-commit/chocitagaming-art/mma-ingesta?style=flat-square&color=ef4444)

[English](./README.md) · Español

</div>

## Qué es

Este repo hace tres trabajos:

1. **Scrapear y limpiar.** Saca luchadores, peleas, estadísticas, eventos y rankings de UFC y ESPN. Saca cuotas de The Odds API y vídeos de combates de YouTube. Lo escribe todo en una base de datos PostgreSQL en Neon.
2. **Entrenar un modelo.** Convierte el historial de peleas en features y entrena un clasificador XGBoost que predice quién gana un enfrentamiento.
3. **Servir predicciones.** Levanta un microservicio FastAPI que recibe dos ids de luchador y devuelve una probabilidad calibrada, las señales que hay detrás y las features que más inclinan la decisión.

La web ([mma-app](https://github.com/chocitagaming-art/mma-app)) lee la misma base de datos y llama al servicio de predicción. Nunca escribe. Todas las escrituras pasan aquí.

## El modelo

- Entrenado solo con estadísticas de los peleadores: récords, físico, golpeo, grappling, forma y calidad del rival, mapeadas en 20 features. Las cuotas nunca son variable de entrada del modelo. Cada feature es una diferencia rojo menos azul, así que el orden de las esquinas no se filtra.
- La precisión ronda el 63% (0.6289), con un Brier de 0.2266. La métrica reportada es la calibrada fuera de muestra, no la optimista del entrenamiento, y el modelo se simetriza para puntuar las dos esquinas igual.
- Está construido sobre un dataset de unos 2.838 luchadores y 8.750 peleas.
- Se compara contra un baseline de clase mayoritaria, así que "¿de verdad está aprendiendo algo?" tiene respuesta.
- Los historiales pobres (debutantes) se marcan como baja confianza en vez de regalarles un favorito falso.
- El mapeo de features tiene una única fuente de verdad (`build_feature_row`), compartida por entrenamiento y servicio, así que no pueden divergir. Los tests golden y de paridad lo fijan.

La ficha del modelo está en [`src/prediction/model_metrics.md`](./src/prediction/model_metrics.md).

## El servicio de predicción

Una app FastAPI en `src/prediction/service.py`. Carga el modelo y un dataframe con el historial de los peleadores una vez, y luego responde:

- `POST /predict` con dos ids de luchador, devolviendo probabilidades, señales por esquina y atribución de features con signo.
- `GET /health` para una comprobación de salud real (modelo cargado más un ping a la base de datos).

Corre con autenticación por API key y un pool de conexiones compartido, y la web lo llama para servir las predicciones.

Ejemplo de petición:

```bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $PREDICTION_SERVICE_API_KEY" \
  -d '{"red": 1234, "blue": 5678}'
```

Respuesta (recortada):

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

## Fuentes de datos

| Dato | Fuente |
|------|--------|
| Luchadores, peleas, estadísticas, eventos, rankings | Scrapers de UFC y ESPN |
| Cuotas | The Odds API (solo eventos próximos) |
| Vídeos | YouTube Data API (canales oficiales de UFC) |
| Predicciones | Modelo XGBoost entrenado aquí |

## Stack

Python 3.12, PostgreSQL en Neon, XGBoost, scikit-learn, pandas, FastAPI, BeautifulSoup, `psycopg`. El refresco programado corre en GitHub Actions.

## Ejecutar en local

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; en macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-scrapers.txt -r requirements-service.txt
```

Copia los nombres de variables de [`.env.example`](./.env.example) a un `.env`:

- `DATABASE_URL` (obligatoria)
- `ANTHROPIC_API_KEY`, `YOUTUBE_API_KEY`, `ODDS_API_KEY`
- `PREDICTION_ENV`, `PREDICTION_DB_POOL_MAX`, `PREDICTION_DATA_TTL_SECONDS`, `PREDICTION_SERVICE_API_KEY`

### Scrapers

Los scrapers son módulos de Python en `src/scrapers`, que se ejecutan con `python -m`. Todo lo que pueda cambiar datos arranca en modo dry-run y necesita `--apply` para escribir de verdad, así siempre ves antes lo que haría un script.

### Entrenar el modelo

El pipeline va en cuatro pasos:

```bash
python -m src.prediction.features    # construye el dataset de entrenamiento desde la BD
python -m src.prediction.train       # entrena el modelo XGBoost
python -m src.prediction.calibrate   # ajusta el calibrador fuera de muestra
python -m src.prediction.evaluate    # reporta métricas calibradas, como en producción
```

Haz copia de `src/prediction/model.joblib` antes de reentrenar.

### Servir predicciones

```bash
python -m uvicorn src.prediction.service:app --port 8000
# GET http://localhost:8000/health  ->  {"status":"ok"}
```

## Refresco programado

GitHub Actions mantiene los datos al día con un horario, en `.github/workflows`:

- `refresh-rankings.yml`
- `refresh-upcoming.yml`
- `refresh-odds.yml`
- `refresh-news.yml`

`ci.yml` corre los tests y el lint en cada push.

## Tests

```bash
python -m pytest tests/ -q
```

La suite incluye tests golden y de paridad que bloquean las features del modelo, además de simetría de esquina y comprobaciones de fuga de datos, así que un refactor o un reentreno no pueden cambiar las entradas en silencio.

## Estructura

```
src/
  scrapers/      # scrapers de UFC + ESPN, cuotas, noticias, vídeos, limpieza de datos, CLI
  prediction/    # pipeline de ML
    features/    # ingeniería de features (carga de datos, historial, build_feature_row)
    train.py     # entrena el modelo
    calibrate.py # calibración fuera de muestra
    evaluate.py  # métricas como en producción
    service.py   # microservicio FastAPI de predicción
    api.py       # lógica de predicción y atribución de features
    model.joblib # el modelo entrenado
tests/           # suite de pytest
```

## El otro repo

[**mma-app**](https://github.com/chocitagaming-art/mma-app) es la web de Next.js que convierte estos datos en un producto vivo. Las capturas y la lista completa de funciones están allí.

## Licencia

MIT. Ver [LICENSE](./LICENSE). Es un proyecto personal, pero los issues y pull requests son bienvenidos.
