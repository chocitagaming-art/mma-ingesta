"""WATCHDOG DEL DIRECTO: ¿hay velada en marcha y NO esta entrando dato?

EL PUNTO CIEGO QUE TAPA. El 1-ago-2026 la velada del 1063 (prelims 14:00Z,
estelar 17:00Z) se perdio entera y los 35 workflows del repo terminaron en verde
toda la tarde. La causa no fue un fallo, fue una AUSENCIA: nadie lanzo el bucle,
porque el cron de `live-event-loop.yml` solo cubre dos franjas de sabado noche
US y el centinela tenia el cron comentado. Y `notify-on-failure.yml` reacciona a
`workflow_run: completed`, asi que **solo puede ver workflows que han corrido**.
Un workflow que no arranca no falla, no termina y no emite ningun evento: era
invisible por construccion.

QUE HACE DISTINTO: no vigila procesos, vigila el DATO. Pregunta "¿hay velada
empezada y no han entrado muestras en los ultimos 45 min?". Esa pregunta se
contesta igual si el bucle nunca se lanzo, si murio por timeout a mitad del
estelar o si alguien lo cancelo sin querer — los tres modos de fallo reales.

POR QUE 45 MINUTOS: el bucle escribe cada ~20 s, asi que en 45 min deberia haber
cientos de muestras. El margen es generoso a proposito: cubre el arranque del
runner (checkout + pip, ~90 s), un hueco entre el job A y su relevo, y los
minutos muertos entre el final de los prelims y el comienzo del estelar, donde
ESPN a veces no publica nada. Mas corto daria falsos rescates; mas largo deja
media cartelera sin grabar antes de reaccionar.

POR QUE NO SE FILTRA POR `status`: el 1063 seguia en 'upcoming' horas despues de
terminar (el flip a 'completed' lo hace refresh-upcoming al dia siguiente, y con
razon: guarda en `event_date < CURRENT_DATE` para no cerrar un evento que aun no
ha ocurrido). Anclar en el status habria hecho al watchdog tan ciego como al
resto. Se ancla en la HORA para entrar en la ventana y en el RESULTADO para
salir de ella.

Solo hace SELECT. Nunca decide nada: imprime un JSON y sale 0 SIEMPRE — quien
dispara (y quien comprueba que no haya ya un bucle vivo) es el workflow.

    python -m scripts.live_watchdog --json
"""

from __future__ import annotations

import argparse
import json
import os

import psycopg2

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

# Sin muestras en esta ventana, se considera que el directo esta caido.
VENTANA_MINUTOS = 45

# Cuanto se estira la ventana de vigilancia tras el comienzo del estelar. Una
# cartelera numerada larga ronda las 4 h desde el estelar; con 5 se cubre la
# sobremesa de las entrevistas sin quedarse vigilando toda la noche.
HORAS_TRAS_EL_ESTELAR = 5

# Misma ancla que el centinela (live_sentinel.ancla_de_evento): el principio de
# la velada NO es `start_time`, que es cuando empieza el estelar. 7 de los 8
# eventos futuros tienen `prelims_time` NULL, de ahi el `- interval '4 hours'`.
VELADAS_SQL = f"""
    SELECT e.id, e.name,
           COALESCE(e.early_prelims_time, e.prelims_time,
                    e.start_time - interval '4 hours') AS ancla,
           count(f.id) FILTER (WHERE f.status IS DISTINCT FROM 'cancelled')
               AS combates_activos,
           count(f.id) FILTER (WHERE f.status IS DISTINCT FROM 'cancelled'
                                 AND (f.winner_id IS NOT NULL
                                      OR f.method IS NOT NULL))
               AS combates_resueltos,
           (SELECT count(*)
              FROM live_fight_stat_samples s
              JOIN fights sf ON sf.id = s.fight_id
             WHERE sf.event_id = e.id
               AND s.sampled_at > now() - interval '{VENTANA_MINUTOS} minutes')
               AS muestras_recientes
    FROM events e
    LEFT JOIN fights f ON f.event_id = e.id
    WHERE COALESCE(e.early_prelims_time, e.prelims_time,
                   e.start_time - interval '4 hours') <= now()
      AND COALESCE(e.start_time, e.prelims_time, e.early_prelims_time)
          + interval '{HORAS_TRAS_EL_ESTELAR} hours' >= now()
    GROUP BY e.id, e.name, e.early_prelims_time, e.prelims_time, e.start_time
    ORDER BY ancla
"""


def estado_de_velada(
    muestras_recientes: int, combates_activos: int, combates_resueltos: int
) -> str:
    """'OK' | 'CAIDO' | 'TERMINADA' para una velada que ya ha empezado.

    El orden de las comprobaciones importa: primero se descarta que la velada
    haya acabado. Al final de una cartelera el bucle sigue escribiendo unos
    minutos, y sin este orden una velada terminada con muestras frescas se
    trataria como viva.

    Ante la duda, CAIDO: una cartelera cuyos combates el scraper todavia no ha
    traido no es una velada terminada. Un rescate de mas cuesta un runner que
    sale en verde en segundos; un rescate de menos cuesta la velada entera, y
    eso no se recupera (la serie del 1063 esta perdida para siempre).

    RESUELTO no es lo mismo que CON GANADOR: un empate y un no contest no
    tienen ganador y no se lo va a dar nadie nunca, asi que exigir winner_id
    dejaba la velada eternamente sin terminar y la relanzaba en falso al parar
    el bucle. La senal de que el combate se celebro es `method`.
    """
    if combates_activos > 0 and combates_resueltos >= combates_activos:
        return "TERMINADA"
    return "OK" if muestras_recientes > 0 else "CAIDO"


def _veladas_en_marcha(dsn: str) -> list[dict]:
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(VELADAS_SQL)
            filas = cur.fetchall()
    finally:
        conn.close()

    veladas = []
    for id_, nombre, ancla, activos, resueltos, muestras in filas:
        veladas.append(
            {
                "event_id": id_,
                "nombre": nombre,
                "ancla_utc": ancla.isoformat() if ancla else None,
                "combates_activos": activos,
                "combates_resueltos": resueltos,
                "muestras_recientes": muestras,
                "estado": estado_de_velada(muestras, activos, resueltos),
            }
        )
    return veladas


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="salida para el workflow")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")

    veladas = _veladas_en_marcha(dsn)

    # Con dos veladas el mismo dia (pasa: numerada + Contender Series), manda la
    # que este peor. Un solo rescate levanta el bucle, que las cubre todas
    # porque pregunta al scoreboard de ESPN, no a un evento concreto.
    if not veladas:
        estado = "SIN_VELADA"
    elif any(v["estado"] == "CAIDO" for v in veladas):
        estado = "CAIDO"
    elif any(v["estado"] == "OK" for v in veladas):
        estado = "OK"
    else:
        estado = "TERMINADA"

    salida = {"estado": estado, "veladas": veladas}

    if args.json:
        print(json.dumps(salida, default=str))
        return

    if estado == "SIN_VELADA":
        print("No hay ninguna velada en marcha. Nada que vigilar.")
        return
    for v in veladas:
        print(
            f"[{v['estado']}] {v['nombre']} (id {v['event_id']}): "
            f"{v['muestras_recientes']} muestras en los ultimos {VENTANA_MINUTOS} min, "
            f"{v['combates_resueltos']}/{v['combates_activos']} combates resueltos"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
