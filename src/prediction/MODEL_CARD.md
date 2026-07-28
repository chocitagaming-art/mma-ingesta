# Ficha del modelo — predicción de ganador · Model card

*[English below](#model-card--fight-winner-prediction)*

---

## Español

Resumen público del modelo que sirve las predicciones de
[MMA STATUS](https://mmastatus.app). Escrito a mano y a propósito: la ficha
técnica completa que genera el entrenamiento se queda fuera de este repositorio.

## Qué predice

Dado un enfrentamiento entre dos luchadores, devuelve la probabilidad de que
gane cada esquina. No predice el método de victoria en esta salida (hay un
segundo modelo para eso) ni el número de asaltos.

## Con qué se entrena

Solo con estadísticas de los luchadores: récord, físico, golpeo, grappling,
forma reciente y calidad del rival. Un clasificador de árboles con gradient
boosting, sobre un histórico de unas 8.750 peleas y 2.838 luchadores.

**Las cuotas nunca entran como variable.** Cuando la web muestra el mercado
al lado del modelo, es una comparación entre dos opiniones independientes, no
una entrada del modelo. Si las cuotas alimentaran la predicción, esa
comparación no significaría nada.

Cada variable se construye como una diferencia entre esquinas (rojo menos
azul), de forma que el orden en que se pidan los luchadores no altera el
resultado.

## Qué tan bien funciona

| | |
| --- | --- |
| Acierto | **~62,9 %** |
| Brier score | **0,2266** |

Las dos cifras son **fuera de muestra y calibradas**: se miden sobre combates
posteriores a los del entrenamiento, no sobre los que ya ha visto. Es la
métrica equivalente a producción, no la optimista del entrenamiento.

**Contexto honesto, que casi nadie publica:** los favoritos de las casas de
apuestas ganan entre el 65 % y el 68 % de los combates de UFC. Este modelo
está *por debajo* de esa referencia. Lo interesante no es que gane al mercado
—no lo hace—, sino **dónde discrepa de él y por qué**, que es justo lo que la
web enseña lado a lado.

## Límites conocidos

- **Es una estimación, no un pronóstico.** En MMA un favorito claro cae por KO
  con normalidad. Un 65 % significa 65 de cada 100, no una certeza.
- **Debutantes y luchadores con poco historial** se marcan explícitamente como
  baja confianza y se quedan cerca del 50/50, en lugar de inventar seguridad
  que el dato no respalda.
- **No modela** lesiones, cortes de peso, cambios de rival de última hora,
  altitud, ni nada que no esté en las estadísticas.
- **Sesgo histórico**: el entrenamiento incluye combates desde 1995, y el MMA
  de entonces se parece poco al de hoy.

## Cómo se comprueba que no se rompe

- Métricas calibradas fuera de muestra, nunca las de entrenamiento.
- Comparación contra un baseline trivial, para responder a "¿de verdad está
  aprendiendo algo?".
- Simetría de esquinas: el mismo combate puntúa igual con los luchadores en
  cualquier orden.
- Tests que fijan las entradas del modelo, para que un refactor o un reentreno
  no las cambien en silencio.
- Comprobaciones de fuga de datos: nada del futuro entra en el pasado.

## Datos

Los datos proceden de fuentes de terceros y no son propiedad de este proyecto.
Ver [LICENSE](../../LICENSE).

---

# Model card — fight winner prediction

Public summary of the model that powers predictions on
[MMA STATUS](https://mmastatus.app). Hand-written on purpose: the full technical
card produced by training stays out of this repository.

## What it predicts

Given a matchup between two fighters, it returns the probability that each
corner wins. This output does not predict the method of victory (a separate
model does that) or the number of rounds.

## What it trains on

Fighter statistics only: record, physicals, striking, grappling, recent form and
opponent quality. A gradient boosting tree classifier, over a history of roughly
8,750 bouts and 2,838 fighters.

**Odds are never an input.** When the site shows the market next to the model,
that is a comparison between two independent opinions, not a model feature. If
odds fed the prediction, that comparison would mean nothing.

Every feature is built as a difference between corners (red minus blue), so the
order in which fighters are requested does not change the result.

## How well it works

| | |
| --- | --- |
| Accuracy | **~62.9%** |
| Brier score | **0.2266** |

Both figures are **out-of-sample and calibrated**: measured on bouts later than
the training set, not on ones already seen. This is the production-equivalent
metric, not the optimistic training one.

**Honest context, which few publish:** bookmaker favourites win between 65% and
68% of UFC bouts. This model sits *below* that benchmark. The interesting part
is not beating the market — it doesn't — but **where it disagrees and why**,
which is exactly what the site shows side by side.

## Known limits

- **It is an estimate, not a forecast.** In MMA a clear favourite gets knocked
  out routinely. 65% means 65 out of 100, not certainty.
- **Debutants and fighters with thin history** are explicitly flagged as low
  confidence and stay near 50/50, rather than inventing confidence the data
  does not support.
- **It does not model** injuries, weight cuts, late opponent changes, altitude,
  or anything that isn't in the statistics.
- **Historical bias**: training includes bouts back to 1995, and the MMA of
  then barely resembles today's.

## How it's kept honest

- Calibrated out-of-sample metrics, never the training ones.
- Comparison against a trivial baseline, to answer "is it actually learning
  anything?".
- Corner symmetry: the same bout scores identically with the fighters in either
  order.
- Tests that pin the model's inputs, so a refactor or a retrain cannot change
  them silently.
- Data leakage checks: nothing from the future leaks into the past.

## Data

Data comes from third-party sources and is not owned by this project. See
[LICENSE](../../LICENSE).
