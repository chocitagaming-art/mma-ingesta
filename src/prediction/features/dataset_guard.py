"""La guarda que impide escribir un CSV de entrenamiento envenenado.

EL FALLO QUE CAZA, medido el 20-sep-2026: training_dataset.csv, generado el
27-jun, traia sus 171 filas de 2026 con target=1 el 97,08 % de las veces. ufcstats
lista primero al ganador y la ingesta lo guardaba como esquina roja (arreglado en
5b8ef87, 22-ago); las variables son rojo-menos-azul, asi que el resultado se
colaba en ellas. El generador escribio el CSV sin rechistar y el modelo publicado
se midio contra el.

Lo que se comprueba ANTES de escribir, y por que:

* La tasa de victoria roja por ano. Fuera de RED_WIN_RATE_BAND, en un ano con al
  menos MIN_ROWS_PER_YEAR peleas decididas, la esquina esta diciendo quien gano.
  Umbrales sacados de los datos (30-sep-2026): los anos con 100 o mas peleas van
  del 44,4 % (2008) al 58,3 % (2011) de victorias rojas en el CSV limpio, y del
  44,7 % (2016) al 59,7 % (2011) en la base; 2026, ya con las esquinas de ufc.com,
  sale al 57,0 %. La banda deja mas de 10 puntos por cada lado, y el 97,1 % de
  junio queda 27 puntos fuera. Con pocas filas manda el azar (1997 tenia 2 filas,
  las 2 con target=1): esos anos no se miran.
  En el CSV de ganador, gana el rojo = target 1. En el de metodo el target dice
  COMO acabo y no delata la fuga, asi que quien gano se mira en fights; un empate
  o una pelea sin resultado no cuenta ni a favor ni en contra.
* fight_id repetidos: la misma pelea pesa doble y puede caer en train y en test.
* Peleas posteriores a hoy: con resultado, no pueden existir.
* Targets fuera de sus clases: 0/1 el ganador, 0/1/2 el metodo.

Limite conocido: se mira el ano entero. Una fuga que empiece en noviembre se
diluye en el resto del ano y puede quedarse dentro de la banda.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

MIN_ROWS_PER_YEAR = 100
RED_WIN_RATE_BAND = (0.30, 0.70)
WINNER_TARGETS = {0, 1}
METHOD_TARGETS = {0, 1, 2}


class PoisonedDatasetError(RuntimeError):
    """El dataset trae un fallo que envenenaria el modelo: el CSV no se escribe."""


def _percent(value: float, decimals: int = 1) -> str:
    return f"{value * 100:.{decimals}f} %".replace(".", ",")


def find_dataset_problems(
    dataset: pd.DataFrame,
    *,
    red_wins: pd.Series,
    allowed_targets: set[int],
    today: date | None = None,
) -> list[str]:
    """Los problemas del dataset, en espanol; lista vacia si esta sano.

    ``red_wins`` va alineado con ``dataset``: 1 si gano el rojo, 0 si gano el
    azul y NaN si no hay ganador de una esquina (empate, sin resultado)."""
    today = today or date.today()
    problems: list[str] = []

    repeated = dataset["fight_id"][dataset["fight_id"].duplicated()].unique()
    if len(repeated):
        examples = ", ".join(str(fight_id) for fight_id in sorted(repeated)[:5])
        problems.append(f"{len(repeated)} fight_id repetidos (por ejemplo {examples})")

    dates = pd.to_datetime(dataset["event_date"])
    future = dates > pd.Timestamp(today)
    if future.any():
        problems.append(
            f"{int(future.sum())} peleas con fecha posterior a hoy "
            f"({today.isoformat()}); la ultima, {dates.max().date().isoformat()}"
        )

    bad_targets = ~dataset["target"].isin(allowed_targets)
    if bad_targets.any():
        values = sorted({str(value) for value in dataset.loc[bad_targets, "target"]})
        problems.append(
            f"{int(bad_targets.sum())} filas con un target fuera de "
            f"{sorted(allowed_targets)}: {', '.join(values[:5])}"
        )

    low, high = RED_WIN_RATE_BAND
    decided = red_wins.isin([0, 1])
    years = dates[decided].dt.year
    per_year = red_wins[decided].astype(float).groupby(years).agg(["count", "mean"])
    for year, row in per_year.iterrows():
        if row["count"] < MIN_ROWS_PER_YEAR:
            continue
        if not low <= row["mean"] <= high:
            problems.append(
                f"{int(year)}: el rojo gana el {_percent(row['mean'])} de "
                f"{int(row['count'])} peleas (banda sana {_percent(low, 0)}-"
                f"{_percent(high, 0)}): la esquina esta diciendo quien gano"
            )
    return problems


def _raise_if_problems(kind: str, problems: list[str]) -> None:
    if not problems:
        return
    lines = "\n".join(f"  - {problem}" for problem in problems)
    raise PoisonedDatasetError(
        f"El CSV de {kind} NO se ha escrito: el dataset trae {len(problems)} "
        f"problema(s) que envenenarian el modelo.\n{lines}\n"
        "El CSV que hubiera se queda como estaba. Arregla el dato antes de "
        "regenerar; si el cambio es legitimo, revisa los umbrales de "
        "src/prediction/features/dataset_guard.py."
    )


def check_winner_dataset(dataset: pd.DataFrame, today: date | None = None) -> None:
    """En el CSV de ganador, target 1 es exactamente 'gano el rojo'."""
    problems = find_dataset_problems(
        dataset, red_wins=dataset["target"], allowed_targets=WINNER_TARGETS, today=today
    )
    _raise_if_problems("ganador", problems)


def _red_win_by_fight(fights_df: pd.DataFrame) -> pd.Series:
    """fight_id -> 1.0 si gano el rojo, 0.0 si gano el azul, NaN si ninguno."""
    winner = fights_df["winner_id"]
    red_win = pd.Series(float("nan"), index=fights_df.index)
    red_win.loc[winner == fights_df["fighter_red_id"]] = 1.0
    red_win.loc[winner == fights_df["fighter_blue_id"]] = 0.0
    by_fight = pd.Series(red_win.to_numpy(), index=fights_df["fight_id"].to_numpy())
    return by_fight[~by_fight.index.duplicated()]


def check_method_dataset(
    dataset: pd.DataFrame, fights_df: pd.DataFrame, today: date | None = None
) -> None:
    """El target del metodo no dice quien gano: se saca de fights_df, la misma
    tabla de la que sale el dataset."""
    red_wins = dataset["fight_id"].map(_red_win_by_fight(fights_df))
    problems = find_dataset_problems(
        dataset, red_wins=red_wins, allowed_targets=METHOD_TARGETS, today=today
    )
    _raise_if_problems("metodo", problems)
