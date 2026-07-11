"""Repositorio de live_fight_stats (migración 017, T3-A fase B).

Tabla APARTE de `fights`/`fight_stats` a propósito: el dato EN VIVO
(estado fino, asalto, reloj y stats provisionales de ESPN durante la
ventana de evento) vive aquí y NUNCA toca el flujo oficial de ufcstats
ni el dataset del modelo. Ver la cabecera de
db/migrations/017_live_fight_stats.sql.

A diferencia de fill_fight_result (COALESCE, el almacenado gana), aquí el
UPSERT pisa la fila entera: es dato vivo y la última lectura es la buena.
Única excepción: stats con NULL entrante conserva el último JSON conocido
(un fallo puntual del endpoint de stats no borra lo ya mostrado).
"""

from __future__ import annotations

import json

from psycopg2.extensions import connection as PgConnection


def upsert_live_fight_stats(
    connection: PgConnection,
    fight_id: int,
    state: str,
    status_name: str | None,
    status_detail: str | None,
    period: int | None,
    display_clock: str | None,
    stats: dict[str, dict[str, int]] | None,
) -> None:
    """Escribe/pisa la fila viva de una pelea (una fila por fight_id)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO live_fight_stats
                (fight_id, state, status_name, status_detail, period,
                 display_clock, stats, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (fight_id) DO UPDATE SET
                state = EXCLUDED.state,
                status_name = EXCLUDED.status_name,
                status_detail = EXCLUDED.status_detail,
                period = EXCLUDED.period,
                display_clock = EXCLUDED.display_clock,
                stats = COALESCE(EXCLUDED.stats, live_fight_stats.stats),
                updated_at = NOW()
            """,
            (
                fight_id,
                state,
                status_name,
                status_detail,
                period,
                display_clock,
                json.dumps(stats, ensure_ascii=False) if stats is not None else None,
            ),
        )


def get_final_stats_fight_ids(connection: PgConnection, fight_ids: list[int]) -> set[int]:
    """Peleas cuyo total final provisional ya quedó guardado (state='post'
    CON stats): el bucle deja de re-pedirlas a ESPN. Una fila 'post' sin
    stats (el endpoint falló justo al acabar) NO cuenta como final y se
    reintenta en la siguiente pasada."""
    if not fight_ids:
        return set()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fight_id
            FROM live_fight_stats
            WHERE fight_id = ANY(%s)
              AND state = 'post'
              AND stats IS NOT NULL
            """,
            (fight_ids,),
        )
        return {int(row[0]) for row in cursor.fetchall()}


def prune_live_fight_stats(connection: PgConnection, older_than_hours: int = 48) -> int:
    """Poda filas viejas: la web solo consulta el evento del día y así la
    tabla se mantiene en ~una cartelera de tamaño."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM live_fight_stats WHERE updated_at < NOW() - make_interval(hours => %s)",
            (older_than_hours,),
        )
        return cursor.rowcount
