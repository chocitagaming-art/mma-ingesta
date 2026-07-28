<div align="center">

<img src="docs/banner.png" alt="MMA STATUS" width="640">

# mma-ingesta

La mitad de datos y machine learning de **[MMA STATUS](https://mmastatus.app)**.

![Python](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square&logo=python)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon-4169e1?style=flat-square&logo=postgresql)
![FastAPI](https://img.shields.io/badge/FastAPI-servicio-009688?style=flat-square&logo=fastapi)
[![CI](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml/badge.svg)](https://github.com/chocitagaming-art/mma-ingesta/actions/workflows/ci.yml)

[English](./README.md) · Español

</div>

## Qué es

Este repositorio mantiene viva la base de datos que hay detrás de MMA STATUS:
recoge luchadores, combates, estadísticas, eventos y clasificaciones, los
normaliza, y entrena el modelo que predice enfrentamientos.

La web ([mma-app](https://github.com/chocitagaming-art/mma-app)) solo **lee**.
Todas las escrituras pasan por aquí.

## Qué hace

- **Recoge y limpia datos** de MMA de fuentes públicas, y los deja consistentes:
  mismo luchador con un solo perfil, combates que no se duplican, resultados que
  se completan solos cuando la fuente oficial publica el detalle.
- **Entrena un modelo** que estima la probabilidad de victoria de cada esquina a
  partir del historial. Ficha pública en
  [`MODEL_CARD.md`](./src/prediction/MODEL_CARD.md).
- **Sirve las predicciones** a la web mediante un microservicio.
- **Se mantiene solo**: los procesos programados refrescan clasificaciones,
  cartelera, cuotas y noticias sin intervención, y avisan si algo falla.

## Durante un evento en directo

La pieza de la que más orgulloso está el proyecto: mientras se celebra una
velada, un proceso va tomando muestras del combate cada pocos segundos y
construye **la película de la pelea** — cómo evoluciona el enfrentamiento
minuto a minuto, no solo el resultado final.

Ese dato **no se puede recuperar después**: si no se captura en directo, se
pierde para siempre. De ahí que el sistema tenga vigilancia propia, un relevo
encadenado y una captura de seguridad en paralelo.

## Cómo está montado

```
Fuentes públicas de MMA
        │
        ▼
   mma-ingesta  ──escribe──▶  PostgreSQL (Neon)  ◀──lee──  mma-app (web)
        │                                                      │
        └──▶  microservicio de predicción  ◀────llama──────────┘
```

Python 3.12, PostgreSQL en Neon, FastAPI y un clasificador de gradient
boosting. Los procesos programados corren en GitHub Actions.

## Calidad

- Suite de **704 pruebas** que corren en cada push.
- Pruebas *golden* y de paridad que fijan las entradas del modelo, para que un
  refactor o un reentreno no las cambien en silencio.
- Comprobaciones de simetría de esquinas y de fuga de datos.
- Todo lo que puede modificar datos arranca en **modo simulación** y exige
  `--apply` para escribir de verdad.
- Copia de seguridad diaria de la base de datos, **restaurada y verificada tabla
  por tabla en cada ejecución**, con plan de recuperación escrito.

## Estructura

```
src/
  scrapers/      # recogida y limpieza de datos
  prediction/    # modelo y microservicio de predicción
tests/           # suite de pytest
```

## Ejecutar en local

Necesitas Python 3.12 y una base PostgreSQL con el esquema del proyecto.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; en macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-scrapers.txt
python -m pytest tests/ -q
```

Los nombres de las variables de entorno están en [`.env.example`](./.env.example).

## Datos y licencia

Los datos que este software recoge **no son propiedad de este proyecto**:
provienen de fuentes de terceros y cada una tiene sus propias condiciones de
uso. Este repositorio no los redistribuye.

El código es de **consulta pública, no de uso libre**: puedes leerlo y
estudiarlo, pero no desplegarlo ni usarlo con fines comerciales sin permiso.
Ver [LICENSE](./LICENSE).

## El otro repositorio

[**mma-app**](https://github.com/chocitagaming-art/mma-app) es la web que
convierte estos datos en un producto vivo.
