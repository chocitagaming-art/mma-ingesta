"""CENTINELA DEL DIRECTO: decide si esta noche hay velada y a que hora despertar.

EL AGUJERO QUE TAPA: `live-event-loop.yml` tiene dos crons de sabado (20:45 y
00:45 UTC) y punto. De los 8 eventos futuros con hora, esos dos slots solo
cubren una parte: el 1063 (1-ago, prelims 14:00Z) y el 1087 (8-ago) no los cubre
NADA. El 25-jul hubo que lanzarlo a mano. Y un cron de un solo uso no vale: esta
medido que el scheduler de GitHub da ~1 run/hora pidas lo que pidas, con un peor
caso observado de 3h33 de retraso. Los `workflow_dispatch`, en cambio, arrancan
al instante — y el 26-jul-2026 se verifico que el GITHUB_TOKEN con
`permissions: actions: write` puede lanzarlos (run 30203474005 -> run hijo
30203478541 con actor `github-actions[bot]`).

De ahi el diseno: el centinela se despierta cada pocas horas, y si la velada cae
dentro de su ventana DUERME DENTRO DEL JOB hasta T-15min y dispara entonces. Asi
la hora de arranque es exacta aunque el scheduler llegue tarde.

TRES COSAS QUE NO SON OBVIAS Y CUESTAN CARO SI SE OLVIDAN:

* El ancla NO es `start_time`. 7 de los 8 eventos futuros tienen `prelims_time`
  NULL, y hay una tercera columna `early_prelims_time`. Anclar en `start_time`
  habria perdido las 3 h de prelims del 25-jul.

* Se razona en hora UTC ABSOLUTA, nunca por `event_date`. El 1087 tiene
  event_date sabado 8-ago y start_time domingo 9-ago 00:00Z: 20 h de error.

* La duracion del bucle se CALCULA desde la hora real de arranque. El 25-jul el
  plan decia 185 min, se lanzo una hora antes y hubo que recalcular a 244 para
  que el relevo cayera igual en el hueco entre prelims y estelar. Fijarla es lo
  que habria matado el job A en mitad de los prelims.

Solo hace SELECT. Uso:

    python -m scripts.live_sentinel --lookahead-hours 5 --json
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import psycopg2

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

UTC = timezone.utc

# Cuanto antes del comienzo se arranca el bucle. 15 min da margen para que el
# runner arranque, instale dependencias y haga la primera pasada antes del
# primer combate, sin quemar ventana de mas.
ADELANTO_MINUTOS = 15

# El relevo del job A tiene que caer ANTES del estelar, no en mitad. El 25-jul
# se eligio el hueco muerto entre el ultimo prelim y el main card justamente
# para que un fallo se descubriera con la velada aun por delante.
MARGEN_ANTES_DEL_ESTELAR_MINUTOS = 10

# Tope duro de un job de GitHub: 360 min. El sueno se acota MUY por debajo: si
# el centinela se pasa, muere antes de disparar y la velada se pierde en VERDE,
# que es el peor fallo posible porque nadie se entera.
SUENO_MAXIMO_MINUTOS = 300

# Cuanto dura como mucho el job A, y su suelo. El techo real medido es ~290 min.
MINUTOS_A_MAXIMO = 285
MINUTOS_A_MINIMO = 60

# Duracion del relevo (job B). Encadena por `concurrency` al morir A.
MINUTOS_B = 235

# Pasado esto desde el estelar, la velada esta muerta y no hay nada que grabar.
HORAS_DE_GRACIA_TRAS_EL_ESTELAR = 4

# Los dos contadores del final son el guard de "esto ya se ha peleado": el reloj
# solo no basta (ver hay_algo_que_grabar). Se filtra `status IS DISTINCT FROM
# 'cancelled'` porque el valor 'active' NO EXISTE — los combates vivos tienen
# status NULL, y un `= 'active'` devolveria cero siempre.
EVENTOS_SQL = """
    SELECT e.id, e.name, e.early_prelims_time, e.prelims_time, e.start_time,
           count(f.id) FILTER (WHERE f.status IS DISTINCT FROM 'cancelled')
               AS combates_activos,
           count(f.id) FILTER (WHERE f.status IS DISTINCT FROM 'cancelled'
                                 AND (f.winner_id IS NOT NULL
                                      OR f.method IS NOT NULL))
               AS combates_resueltos
    FROM events e
    LEFT JOIN fights f ON f.event_id = e.id
    WHERE e.status = 'upcoming'
      AND COALESCE(e.start_time, e.prelims_time, e.early_prelims_time) IS NOT NULL
      AND COALESCE(e.start_time, e.prelims_time, e.early_prelims_time)
          > now() - interval '%s hours'
    GROUP BY e.id, e.name, e.early_prelims_time, e.prelims_time, e.start_time
    ORDER BY COALESCE(e.early_prelims_time, e.prelims_time, e.start_time)
    LIMIT 5
"""


@dataclass
class Plan:
    """Que hacer con la velada que viene."""

    arranque: datetime
    dormir_segundos: int
    minutos_a: int
    minutos_b: int = MINUTOS_B


def ancla_de_evento(
    early_prelims_time: datetime | None,
    prelims_time: datetime | None,
    start_time: datetime | None,
) -> datetime | None:
    """Cuando EMPIEZA de verdad la velada, no cuando empieza el estelar.

    El equivalente en SQL seria
    `COALESCE(early_prelims_time, prelims_time, start_time - interval '4 hours')`,
    pero se hace aqui para poder probarlo sin base de datos.
    """
    if early_prelims_time is not None:
        return early_prelims_time
    if prelims_time is not None:
        return prelims_time
    if start_time is not None:
        return start_time - timedelta(hours=4)
    return None


def hay_algo_que_grabar(combates_activos: int, combates_resueltos: int) -> bool:
    """¿Queda velada por delante, o esto ya se ha peleado entero?

    EL FALLO QUE TAPA, cazado en caliente el 1-ago-2026 a las 20:36Z: el 1063
    habia terminado a las ~19:40Z y el centinela lo daba por vivo, con
    `dormir_segundos = 0`. Habria disparado los cuatro workflows sobre una
    velada acabada.

    Por que la hora no basta: la gracia se mide sobre `start_time + 4 h`, y el
    estelar del 1063 empezaba a las 17:00Z, asi que hasta las 21:00Z el reloj
    decia que seguia viva. Alargar la gracia no arregla nada — la acorta para
    las veladas largas, que es justo cuando el relevo hace falta. El unico dato
    que sabe de verdad si la velada acabo es el resultado de los combates.

    Ante la duda, GRABAR: una cartelera sin combates cargados todavia no es una
    velada terminada, y una pasada de mas del bucle sale en verde en segundos
    (su primera pasada ve el scoreboard vacio y termina), mientras que una
    velada perdida no se recupera nunca.

    RESUELTO no es CON GANADOR. Un empate y un no contest no tienen ganador y no
    se lo va a dar nadie, asi que exigir `winner_id` repetia el mismo fallo que
    este guard vino a tapar, pero por el lado del dato: la velada se quedaba
    viva para siempre. La senal de que el combate se celebro es `method`.
    """
    if combates_activos == 0:
        return True
    return combates_resueltos < combates_activos


def plan_de_arranque(
    ancla: datetime | None,
    start_time: datetime | None,
    ahora: datetime,
    lookahead_horas: int,
) -> Plan | None:
    """El plan, o None si no hay nada que hacer (el 95 % de las ejecuciones)."""
    if ancla is None:
        return None

    referencia_final = start_time or ancla
    if ahora > referencia_final + timedelta(hours=HORAS_DE_GRACIA_TRAS_EL_ESTELAR):
        return None  # velada terminada: no hay pelicula que grabar

    if ancla - ahora > timedelta(hours=lookahead_horas):
        return None  # todavia lejos; ya lo cogera un run posterior

    arranque = max(ancla - timedelta(minutes=ADELANTO_MINUTOS), ahora)
    dormir = max(0, int((arranque - ahora).total_seconds()))
    dormir = min(dormir, SUENO_MAXIMO_MINUTOS * 60)

    # La duracion se mide desde el arranque REAL, no desde un numero fijo.
    if start_time is not None:
        relevo = start_time - timedelta(minutes=MARGEN_ANTES_DEL_ESTELAR_MINUTOS)
        minutos_a = int((relevo - arranque).total_seconds() // 60)
    else:
        minutos_a = MINUTOS_A_MAXIMO
    minutos_a = max(MINUTOS_A_MINIMO, min(MINUTOS_A_MAXIMO, minutos_a))

    return Plan(arranque=arranque, dormir_segundos=dormir, minutos_a=minutos_a)


def _proximo_evento(dsn: str, lookahead_horas: int) -> tuple[dict, Plan] | None:
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(EVENTOS_SQL % HORAS_DE_GRACIA_TRAS_EL_ESTELAR)
            filas = cur.fetchall()
    finally:
        conn.close()

    ahora = datetime.now(UTC)
    for id_, nombre, early, prelims, inicio, activos, resueltos in filas:
        if not hay_algo_que_grabar(activos, resueltos):
            continue  # esta ya se ha peleado entera; el reloj no lo sabe
        ancla = ancla_de_evento(early, prelims, inicio)
        plan = plan_de_arranque(ancla, inicio, ahora, lookahead_horas)
        if plan is not None:
            evento = {
                "event_id": id_,
                "nombre": nombre,
                "ancla_utc": ancla.astimezone(UTC).isoformat(),
                "estelar_utc": inicio.astimezone(UTC).isoformat() if inicio else None,
            }
            return evento, plan
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--lookahead-hours", type=int, default=5,
        help="cuantas horas por delante mira (default 5)",
    )
    ap.add_argument("--json", action="store_true", help="salida para el workflow")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")

    encontrado = _proximo_evento(dsn, args.lookahead_hours)

    if encontrado is None:
        salida = {"hay_evento": False}
        print(json.dumps(salida) if args.json else "Sin velada en la ventana. Nada que hacer.")
        return

    evento, plan = encontrado
    salida = {
        "hay_evento": True,
        **evento,
        **asdict(plan),
        "arranque": plan.arranque.astimezone(UTC).isoformat(),
    }
    if args.json:
        print(json.dumps(salida))
    else:
        print(f"Velada: {evento['nombre']} (id {evento['event_id']})")
        print(f"  ancla   : {evento['ancla_utc']}")
        print(f"  arranque: {salida['arranque']}  (dormir {plan.dormir_segundos // 60} min)")
        print(f"  job A   : {plan.minutos_a} min · job B: {plan.minutos_b} min")


if __name__ == "__main__":  # pragma: no cover
    main()
