"""WATCHDOG DEL DIRECTO: ¿hay velada en marcha y NO esta entrando dato?

EL PUNTO CIEGO QUE TAPA. El 1-ago-2026 la velada del 1063 (prelims 14:00Z,
estelar 17:00Z) se perdio entera y los 35 workflows del repo terminaron en verde
toda la tarde. La causa no fue un fallo, fue una AUSENCIA: nadie lanzo el bucle,
porque el cron de `live-event-loop.yml` solo cubre dos franjas de sabado noche
US y el centinela tenia el cron comentado. Y `notify-on-failure.yml` reacciona a
`workflow_run: completed`, asi que **solo puede ver workflows que han corrido**.
Un workflow que no arranca no falla, no termina y no emite ningun evento: era
invisible por construccion.

QUE HACE DISTINTO: no vigila procesos, vigila que el BUCLE este vivo. La
pregunta se contesta igual si el bucle nunca se lanzo, si murio por timeout a
mitad del estelar o si alguien lo cancelo sin querer — los tres modos de fallo
reales.

ANTES PREGUNTABA "¿han entrado muestras en 45 min?", Y ERA UNA PREGUNTA MALA
(corregido el 17-ago-2026). Las muestras las escribe TAMBIEN el cron de respaldo
`live-results.yml`, que ejecuta el mismo modulo sin --duration-minutes: entra 6
veces por hora y escribe lo que haya cambiado. O sea que "entra dato" no
significaba "el bucle vive". Medido: cruzando los 8 runs de este watchdog de la
noche del UFC 330 contra los del respaldo, LOS OCHO tenian un respaldo dentro de
su ventana de 45 min. Con el bucle muerto toda la noche, los ocho habrian dicho
OK y no habria habido ni un rescate.

Y hay una ironia que conviene no repetir: el commit que creo este watchdog
(3bea1fe, 1-ago) anadio EN EL MISMO CAMBIO el cron de respaldo de los sabados
por el dia. O sea que el incidente que lo hizo nacer tampoco se detectaria hoy.

AHORA PREGUNTA POR EL LATIDO (`service_heartbeats`, servicio 'live-loop'), que
solo escribe `run_bounded_loop` y el respaldo no puede fingir. Las muestras
quedan de salvaguarda para cuando no hay fila de latido. Ver
UMBRAL_LATIDO_MINUTOS y VENTANA_MINUTOS abajo.

POR QUE 45 MINUTOS (la salvaguarda): el bucle escribe cada ~20 s, asi que en 45
min deberia haber cientos de muestras. El margen es generoso a proposito: cubre
el arranque del runner (checkout + pip, ~90 s), un hueco entre el job A y su
relevo, y los minutos muertos entre el final de los prelims y el comienzo del
estelar, donde ESPN a veces no publica nada. Mas corto daria falsos rescates;
mas largo deja media cartelera sin grabar antes de reaccionar.

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

# La fuente de verdad del nombre del servicio, no una cadena repetida aqui: si
# alguien lo renombra alli, este SELECT dejaria de encontrar la fila y el
# watchdog volveria al criterio viejo SIN QUE NADA FALLE.
from src.scrapers.repositories.service_heartbeats import SERVICIO_BUCLE

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

# Sin muestras en esta ventana, se considera que el directo esta caido.
# SOLO se usa cuando NO hay fila de latido; desde el 17-ago-2026 manda el latido.
VENTANA_MINUTOS = 45

# Cuanto puede callar el LATIDO del bucle antes de darlo por caido.
#
# POR QUE EL LATIDO Y NO LAS MUESTRAS. "Entran muestras" no significa "el bucle
# esta vivo": live-results.yml (*/10) ejecuta el MISMO modulo sin
# --duration-minutes, asi que escribe muestras sin pasar jamas por
# run_bounded_loop, que es lo unico que late. Cruzados los 8 runs del watchdog
# de la noche del UFC 330 contra los del respaldo, LOS OCHO tenian respaldo
# dentro de su ventana de 45 min: con el bucle muerto toda la noche, los ocho
# habrian dicho OK. Y el commit que creo este watchdog (3bea1fe) anadio en el
# MISMO cambio el cron de respaldo de los sabados, asi que el incidente que lo
# hizo nacer —el 1063 perdido entero— hoy tampoco se detectaria.
#
# POR QUE 15 Y NO LOS 10 DEL PANEL. El bucle late como mucho una vez por minuto
# (LATIDO_CADA_SEGUNDOS en espn_live_results.py), y en el relevo A->B hay 2 s de
# concurrency mas ~90 s de arranque del runner (medido: A murio 00:51:26Z, B
# arranco 00:51:28Z). El panel avisa a un humano a los 10; aqui se autoriza un
# runner, asi que se deja mas margen. Afinar al minuto no compra latencia — este
# cron se sirve con 88 min de media— y si compra falsos positivos.
UMBRAL_LATIDO_MINUTOS = 15

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
    muestras_recientes: int,
    combates_activos: int,
    combates_resueltos: int,
    latido_min: float | None = None,
) -> str:
    """'OK' | 'CAIDO' | 'TERMINADA' para una velada que ya ha empezado.

    El orden de las comprobaciones importa: primero se descarta que la velada
    haya acabado. Al final de una cartelera el bucle sigue escribiendo unos
    minutos, y sin este orden una velada terminada con muestras frescas se
    trataria como viva. TERMINADA sigue siendo lo primero tambien con el latido:
    al acabar el cartel el bucle deja de latir en cuanto muere, y sin ese orden
    la cola de la noche daria CAIDO cada hora (el 15-ago la velada siguio "en
    marcha" 27 min despues de sellarse la ultima pelea).

    QUIEN MANDA ES EL LATIDO (17-ago-2026), no las muestras. Ver
    UMBRAL_LATIDO_MINUTOS arriba: las muestras las escribe tambien el cron de
    respaldo, asi que no distinguen un bucle vivo de uno muerto; el latido si,
    porque solo lo escribe run_bounded_loop.

    Y ESO NO PUEDE COSTAR UNA VELADA, que es lo que lo hace aceptable: el latido
    entra en la DETECCION, nunca en el permiso para disparar. Ese permiso sigue
    siendo `steps.guard.outputs.vivos == '0'` en live-watchdog.yml. Si el latido
    fallara por su cuenta con el bucle vivo, el guard contaria 1 y bloquearia:
    un CAIDO falso cuesta un correo, no un runner de mas.

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
    if latido_min is None:
        # Salvaguarda, no camino normal: hoy la fila existe. Cubre que alguien
        # renombre el servicio o vacie la tabla, para no dar CAIDO ocho veces
        # seguidas por un dato que falta.
        return "OK" if muestras_recientes > 0 else "CAIDO"
    return "OK" if latido_min <= UMBRAL_LATIDO_MINUTOS else "CAIDO"


# El latido del bucle. Una fila por servicio, sin historial (migracion 026); si
# el bucle no ha latido nunca no hay fila y esto devuelve None.
LATIDO_SQL = """
    SELECT extract(epoch FROM (now() - last_ok_at)) / 60
      FROM service_heartbeats
     WHERE service = %s
"""


def _veladas_en_marcha(dsn: str) -> list[dict]:
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(VELADAS_SQL)
            filas = cur.fetchall()
            # Misma conexion de solo lectura: es un SELECT de una fila.
            cur.execute(LATIDO_SQL, (SERVICIO_BUCLE,))
            fila_latido = cur.fetchone()
    finally:
        conn.close()

    latido_min = float(fila_latido[0]) if fila_latido and fila_latido[0] is not None else None

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
                # Va en el diagnostico a proposito: el correo de las 3 de la
                # manana tiene que dejar distinguir de un vistazo "el bucle no
                # late" de "late pero no entra dato".
                "latido_min": round(latido_min, 1) if latido_min is not None else None,
                "estado": estado_de_velada(muestras, activos, resueltos, latido_min),
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
