"""Repositorio de service_heartbeats (migración 026).

Una fila por servicio, sin historial: la única pregunta es «cuándo fue la
última vez que este servicio dio señales de vida». Ver la cabecera de
db/migrations/026_service_heartbeats.sql.

QUIÉN ESCRIBE AQUÍ. Hasta hoy, solo `keepalive-prediction.yml` con un psql a
pelo. Desde el 17-ago-2026, también el bucle del directo, con
`upsert_service_heartbeat(conn, SERVICIO_BUCLE, ...)`.

POR QUÉ EL BUCLE NECESITA UN LATIDO PROPIO. El bucle del directo
(`live-event-loop.yml`, cada 20 s) y el cron de respaldo (`live-results.yml`,
cada 10 min) son EL MISMO PROGRAMA: lo único que los distingue es el argumento
`--duration-minutes`. Los dos escriben en `live_fight_stats`, así que el
«pulso» que mira el panel no puede demostrar cuál de los dos está vivo. Con el
bucle muerto y el respaldo escribiendo sus 2-6 muestras por hora, el panel
aguanta en verde hasta 60 minutos (el cálculo está en veredicto.ts) y el correo
llega aún más tarde.

Con este latido, «el bucle está vivo» pasa de indemostrable a demostrable.

EL SITIO DONDE SE LLAMA ES LA MITAD DEL ARREGLO: dentro de `run_bounded_loop`
y solo ahí. Si se llamara desde `refresh_live_results`, que es la ruta que
comparten los dos, el respaldo escribiría el mismo latido y esto valdría cero
PARECIENDO que funciona, que es el peor final posible.
"""

from __future__ import annotations

from psycopg2.extensions import connection as PgConnection

# Nombre del servicio en la tabla. Corto y estable, como pide la migración: si
# esto cambia, el panel deja de encontrar la fila y se queda sin latido — que
# NO es rojo por diseño (un latido ausente es "sin noticias", no "caído"), así
# que el fallo sería silencioso. No renombrar sin tocar el lado que lo lee.
SERVICIO_BUCLE = "live-loop"


def upsert_service_heartbeat(
    connection: PgConnection,
    service: str,
    detail: str | None = None,
) -> None:
    """Deja constancia de que `service` sigue vivo AHORA.

    Mismo SQL que el paso "Anotar el latido" de keepalive-prediction.yml. No
    devuelve nada: siempre escribe una fila, la cree o la actualice.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO service_heartbeats (service, last_ok_at, detail)
            VALUES (%s, NOW(), %s)
            ON CONFLICT (service) DO UPDATE
              SET last_ok_at = EXCLUDED.last_ok_at, detail = EXCLUDED.detail
            """,
            (service, detail),
        )
