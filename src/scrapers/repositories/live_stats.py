"""Repositorio de live_fight_stats (migración 017, T3-A fase B).

Tabla APARTE de `fights`/`fight_stats` a propósito: el dato EN VIVO
(estado fino, asalto, reloj y stats provisionales de ESPN durante la
ventana de evento) vive aquí y NUNCA toca el flujo oficial de ufcstats
ni el dataset del modelo. Ver la cabecera de
db/migrations/017_live_fight_stats.sql.

A diferencia de fill_fight_result (COALESCE, el almacenado gana), aquí el
UPSERT pisa el estado vivo (asalto, reloj, estado fino): es dato vivo y la
última lectura es la buena. Las stats, en cambio, se MEZCLAN por lado
(`stats || EXCLUDED.stats`): un fetch parcial de un solo luchador actualiza
su lado sin borrar al rival (hallazgo 2), y un fetch NULL total conserva lo
ya mostrado. Además el estado 'post' es PEGAJOSO e is_final MONOTÓNICA para
que un escritor solapado no retroceda un final ya bueno (hallazgo 4).
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
    is_final: bool = False,
) -> None:
    """Escribe/pisa la fila viva de una pelea (una fila por fight_id).

    is_final: solo TRUE cuando el llamador confirmó stats FRESCAS de ambos
    lados en 'post' (ver _process_live_stats). Se guarda con OR sobre el valor
    almacenado: una vez sellado, ningún escritor posterior lo desella."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO live_fight_stats
                (fight_id, state, status_name, status_detail, period,
                 display_clock, stats, is_final, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (fight_id) DO UPDATE SET
                -- 'post' pegajoso: una pelea terminada no vuelve a 'in' aunque
                -- un escritor solapado llegue con un scoreboard rezagado.
                state = CASE
                    WHEN live_fight_stats.state = 'post' THEN 'post'
                    ELSE EXCLUDED.state
                END,
                status_name = EXCLUDED.status_name,
                status_detail = EXCLUDED.status_detail,
                period = EXCLUDED.period,
                display_clock = EXCLUDED.display_clock,
                -- Merge por lado: el JSON entrante (aunque sea de un solo
                -- luchador) actualiza SU clave y conserva la del rival; NULL
                -- entrante deja intacto lo guardado.
                stats = COALESCE(live_fight_stats.stats, '{}'::jsonb)
                        || COALESCE(EXCLUDED.stats, '{}'::jsonb),
                is_final = live_fight_stats.is_final OR EXCLUDED.is_final,
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
                is_final,
            ),
        )


def get_final_stats_fight_ids(connection: PgConnection, fight_ids: list[int]) -> set[int]:
    """Peleas cuyo total final SELLADO (is_final) ya está guardado: el bucle
    deja de re-pedirlas a ESPN. Una fila 'post' que NO llegó a is_final (el
    endpoint de stats falló o solo respondió un lado en la pasada de cierre)
    se reintenta barato en cada pasada hasta capturar el total completo."""
    if not fight_ids:
        return set()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fight_id
            FROM live_fight_stats
            WHERE fight_id = ANY(%s)
              AND is_final = true
            """,
            (fight_ids,),
        )
        return {int(row[0]) for row in cursor.fetchall()}


def delete_live_fight_stats(connection: PgConnection, fight_id: int) -> None:
    """Borra la fila viva de una pelea (p. ej. cancelada/aplazada a mitad de
    evento): sin fila, /en-vivo no pinta un 'Final 0/0' de algo que no ocurrió."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM live_fight_stats WHERE fight_id = %s", (fight_id,))


def prune_live_fight_stats(connection: PgConnection, older_than_hours: int = 48) -> int:
    """Poda filas viejas: la web solo consulta el evento del día y así la
    tabla se mantiene en ~una cartelera de tamaño."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM live_fight_stats WHERE updated_at < NOW() - make_interval(hours => %s)",
            (older_than_hours,),
        )
        return cursor.rowcount
