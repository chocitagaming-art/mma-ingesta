"""La guarda que impide escribir un CSV de entrenamiento envenenado.

EL FALLO QUE CAZA, medido el 20-sep-2026: training_dataset.csv, generado el
27-jun, traía sus 171 filas de 2026 con target=1 el 97,08 % de las veces. ufcstats
lista primero al ganador y la ingesta lo guardaba como esquina roja (arreglado en
5b8ef87, 22-ago); las variables son rojo-menos-azul, así que el resultado se
colaba en ellas. El generador escribió el CSV sin rechistar y el modelo publicado
se midió contra él.

Lo que se comprueba ANTES de escribir, y por qué:

* Que se sepa quién ganó. En el CSV de ganador, gana el rojo = target 1. En el de
  método el target dice CÓMO acabó y no delata la fuga, así que quién ganó se mira
  en fights; un empate o una pelea sin resultado no cuenta ni a favor ni en
  contra. Si el cruce con fights falla (un fight_id de otro tipo, fights vacío),
  ninguna fila tiene ganador conocido y las comprobaciones de la tasa no ven nada:
  hace falta un ganador conocido en al menos MIN_KNOWN_WINNER_SHARE de las filas,
  en total y en las RECENT_FIGHTS más recientes. Medido el 30-sep con la base: lo
  tiene el 99,4 % de las filas del CSV de método, y el 100 % de las recientes.
* La tasa de victoria roja por año. Fuera de RED_WIN_RATE_BAND, en un año con al
  menos MIN_ROWS_PER_YEAR peleas decididas, la esquina está diciendo quién ganó.
  Umbrales sacados de los datos (30-sep-2026): los años con 100 o más peleas van
  del 44,4 % (2008) al 58,3 % (2011) de victorias rojas en el CSV limpio, y del
  44,7 % (2016) al 59,7 % (2011) en la base; 2026, ya con las esquinas de ufc.com,
  sale al 57,0 %. La banda deja más de 10 puntos por cada lado, y el 97,1 % de
  junio queda 27 puntos fuera. Con pocas filas manda el azar (1997 tenía 2 filas,
  las 2 con target=1): esos años no se miran.
* La misma banda en las RECENT_FIGHTS peleas más recientes, sin mirar el año. El
  mínimo de 100 filas deja ciego el año en curso hasta abril o mayo (el CSV de
  junio cortado al 15-abr, con 96 filas de 2026 todas rojas, pasaba), y una fuga
  que empieza a final de año se diluye en el año entero. Una fuga nueva entra por
  las peleas más recientes. En el CSV limpio, las ventanas de 150 peleas van del
  39,3 % al 62,7 %, y la última da el 56,0 %. Se mira solo la última: con el rojo
  ganando el 57 % (2026), mirar todas las ventanas de cinco años de peleas daría
  alguna falsa alarma el 4,9 % de las veces, y la última sola el 0,06 % (simulado).
* fight_id repetidos: la misma pelea pesa doble y puede caer en train y en test.
* Peleas posteriores a hoy: con resultado, no pueden existir.
* Targets fuera de sus clases: 0/1 el ganador, 0/1/2 el método.

Límites conocidos: la ventana necesita unas 40-60 peleas con fuga total para
salir de la banda. El CSV de junio, cortado fecha a fecha, salta desde la fila 43
de 2026 (28-feb); solo por año saltaba desde la 102 (18-abr). Con menos, pasa. Y
una fuga corta que ya se cortó, en mitad de un año, se diluye en ese año y ya no
está entre las peleas más recientes.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

MIN_ROWS_PER_YEAR = 100
RECENT_FIGHTS = 150
MIN_KNOWN_WINNER_SHARE = 0.90
RED_WIN_RATE_BAND = (0.30, 0.70)
WINNER_TARGETS = {0, 1}
METHOD_TARGETS = {0, 1, 2}


class PoisonedDatasetError(RuntimeError):
    """El dataset trae un fallo que envenenaría el modelo: el CSV no se escribe."""


def _percent(value: float, decimals: int = 1) -> str:
    return f"{value * 100:.{decimals}f} %".replace(".", ",")


def _outside_band(rate: float) -> bool:
    low, high = RED_WIN_RATE_BAND
    return not low <= rate <= high


def _band_note() -> str:
    low, high = RED_WIN_RATE_BAND
    return (
        f"(banda sana {_percent(low, 0)}-{_percent(high, 0)}): la esquina está "
        "diciendo quién ganó"
    )


def _blind_note() -> str:
    return (
        "tienen un ganador conocido, y hace falta al menos el "
        f"{_percent(MIN_KNOWN_WINNER_SHARE, 0)}: sin saber quién ganó, la guarda no "
        "ve la fuga de esquinas (¿ha fallado el cruce con fights?)"
    )


def find_dataset_problems(
    dataset: pd.DataFrame,
    *,
    red_wins: pd.Series,
    allowed_targets: set[int],
    today: date | None = None,
) -> list[str]:
    """Los problemas del dataset, en español; lista vacía si está sano.

    ``red_wins`` va alineado con ``dataset``: 1 si ganó el rojo, 0 si ganó el
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
            f"({today.isoformat()}); la última, {dates.max().date().isoformat()}"
        )

    bad_targets = ~dataset["target"].isin(allowed_targets)
    if bad_targets.any():
        values = sorted({str(value) for value in dataset.loc[bad_targets, "target"]})
        problems.append(
            f"{int(bad_targets.sum())} filas con un target fuera de "
            f"{sorted(allowed_targets)}: {', '.join(values[:5])}"
        )

    known = red_wins.isin([0, 1])
    if known.mean() < MIN_KNOWN_WINNER_SHARE:
        problems.append(
            f"solo {int(known.sum())} de {len(known)} filas "
            f"({_percent(known.mean())}) {_blind_note()}"
        )

    years = dates[known].dt.year
    per_year = red_wins[known].astype(float).groupby(years).agg(["count", "mean"])
    for year, row in per_year.iterrows():
        if row["count"] >= MIN_ROWS_PER_YEAR and _outside_band(row["mean"]):
            problems.append(
                f"{int(year)}: el rojo gana el {_percent(row['mean'])} de "
                f"{int(row['count'])} peleas {_band_note()}"
            )

    # Las más recientes, sin mirar el año: por ahí entra una fuga nueva.
    recent = (
        pd.DataFrame(
            {"date": dates, "fight_id": dataset["fight_id"], "red_win": red_wins}
        )
        .sort_values(["date", "fight_id"], kind="stable")
        .tail(RECENT_FIGHTS)
    )
    if len(recent) == RECENT_FIGHTS:
        where = (
            f"las {RECENT_FIGHTS} peleas más recientes (del "
            f"{recent['date'].iloc[0].date().isoformat()} al "
            f"{recent['date'].iloc[-1].date().isoformat()})"
        )
        recent_known = recent["red_win"].isin([0, 1])
        if recent_known.mean() < MIN_KNOWN_WINNER_SHARE:
            problems.append(
                f"de {where}, solo {int(recent_known.sum())} {_blind_note()}"
            )
        else:
            rate = recent.loc[recent_known, "red_win"].astype(float).mean()
            if _outside_band(rate):
                problems.append(
                    f"en {where}, el rojo gana el {_percent(rate)} de las "
                    f"{int(recent_known.sum())} con ganador conocido {_band_note()}"
                )
    return problems


def _raise_if_problems(kind: str, problems: list[str]) -> None:
    if not problems:
        return
    lines = "\n".join(f"  - {problem}" for problem in problems)
    raise PoisonedDatasetError(
        f"El CSV de {kind} NO se ha escrito: el dataset trae {len(problems)} "
        f"problema(s) que envenenarían el modelo.\n{lines}\n"
        "El CSV que hubiera se queda como estaba. Arregla el dato antes de "
        "regenerar; si el cambio es legítimo, revisa los umbrales de "
        "src/prediction/features/dataset_guard.py."
    )


def check_winner_dataset(dataset: pd.DataFrame, today: date | None = None) -> None:
    """En el CSV de ganador, target 1 es exactamente 'ganó el rojo'."""
    problems = find_dataset_problems(
        dataset, red_wins=dataset["target"], allowed_targets=WINNER_TARGETS, today=today
    )
    _raise_if_problems("ganador", problems)


def _red_win_by_fight(fights_df: pd.DataFrame) -> pd.Series:
    """fight_id -> 1.0 si ganó el rojo, 0.0 si ganó el azul, NaN si ninguno."""
    winner = fights_df["winner_id"]
    red_win = pd.Series(float("nan"), index=fights_df.index)
    red_win.loc[winner == fights_df["fighter_red_id"]] = 1.0
    red_win.loc[winner == fights_df["fighter_blue_id"]] = 0.0
    by_fight = pd.Series(red_win.to_numpy(), index=fights_df["fight_id"].to_numpy())
    return by_fight[~by_fight.index.duplicated()]


def check_method_dataset(
    dataset: pd.DataFrame, fights_df: pd.DataFrame, today: date | None = None
) -> None:
    """El target del método no dice quién ganó: se saca de fights_df, la misma
    tabla de la que sale el dataset. Si el cruce no encuentra el ganador, la
    comprobación de cobertura de find_dataset_problems lo convierte en problema."""
    red_wins = dataset["fight_id"].map(_red_win_by_fight(fights_df))
    problems = find_dataset_problems(
        dataset, red_wins=red_wins, allowed_targets=METHOD_TARGETS, today=today
    )
    _raise_if_problems("método", problems)
