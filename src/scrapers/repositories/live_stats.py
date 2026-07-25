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
ya mostrado. Una vez SELLADA (is_final), la fila es INMUTABLE: el DO UPDATE
lleva `WHERE NOT is_final`, así que ningún escritor solapado rezagado puede
pisar el total final bueno con números de mitad de pelea (hallazgo 4 y su
re-revisión: el sello protege el DATO, no solo el flag).
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
            WHERE NOT live_fight_stats.is_final
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


def insert_live_fight_stat_sample(connection: PgConnection, fight_id: int) -> int:
    """Timeline del directo (migración 024): copia la fila viva YA FUSIONADA
    como muestra append-only en live_fight_stat_samples.

    Se llama justo después de upsert_live_fight_stats, en la MISMA transacción,
    así que ve el snapshot recién mezclado (aunque el fetch de la pasada fuera
    parcial, la muestra lleva los dos lados). Guardas EN SQL, por construcción:
      - sin stats (NULL o '{}' — el merge del upsert convierte NULL||NULL en
        '{}') o en walkouts (period 0) no hay muestra: la serie empieza con
        el primer asalto;
      - dedup contra la ÚLTIMA muestra de la pelea (state, period, reloj y
        stats idénticos => 0 filas): el cron de respaldo live-results (*/10)
        solapado con el bucle no duplica, y los descansos no acumulan filas
        congeladas.
    sampled_at usa clock_timestamp() (hora REAL del insert), no NOW(): NOW()
    es el INICIO de la transacción, y como la transacción por evento abarca
    los GETs a ESPN, un escritor solapado lento (cron */10) estamparía
    timestamps retroactivos y desordenaría la serie para siempre (hallazgo de
    la revisión adversarial 19-jul).
    La serie NO se poda (decisión del dueño 19-jul: la película se conserva);
    una cancelada la borra delete_live_fight_stat_samples y borrar la pelea
    arrastra en cascada. Devuelve las filas insertadas (0 o 1)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO live_fight_stat_samples
                (fight_id, state, status_name, status_detail, period,
                 display_clock, stats, sampled_at)
            SELECT lfs.fight_id, lfs.state, lfs.status_name, lfs.status_detail,
                   lfs.period, lfs.display_clock, lfs.stats, clock_timestamp()
            FROM live_fight_stats lfs
            WHERE lfs.fight_id = %s
              AND lfs.stats IS NOT NULL
              AND lfs.stats <> '{}'::jsonb
              AND COALESCE(lfs.period, 0) >= 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM (
                      SELECT state, period, display_clock, stats
                      FROM live_fight_stat_samples
                      WHERE fight_id = %s
                      ORDER BY sampled_at DESC, id DESC
                      LIMIT 1
                  ) last
                  WHERE last.state = lfs.state
                    AND last.period IS NOT DISTINCT FROM lfs.period
                    AND last.display_clock IS NOT DISTINCT FROM lfs.display_clock
                    AND last.stats IS NOT DISTINCT FROM lfs.stats
              )
            """,
            (fight_id, fight_id),
        )
        return cursor.rowcount


def insert_live_fight_stat_sample_at(
    connection: PgConnection,
    fight_id: int,
    state: str,
    status_name: str | None,
    status_detail: str | None,
    period: int | None,
    display_clock: str | None,
    stats: dict[str, dict[str, int]],
    sampled_at,
) -> None:
    """Inserta una muestra con sampled_at EXPLÍCITO (datetime aware).

    Solo para el backfill desde capturas (backfill_live_samples_from_capture):
    la hora de la muestra es la hora REAL de la pasada capturada, no NOW().
    El dedup/guardas los hace el propio backfill en memoria (replay ordenado)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO live_fight_stat_samples
                (fight_id, state, status_name, status_detail, period,
                 display_clock, stats, sampled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                fight_id,
                state,
                status_name,
                status_detail,
                period,
                display_clock,
                json.dumps(stats, ensure_ascii=False),
                sampled_at,
            ),
        )


def count_live_fight_stat_samples(connection: PgConnection, fight_id: int) -> int:
    """Muestras existentes de una pelea (guard anti-machaque del backfill:
    no reemplazar una serie en vivo más densa por una captura más pobre)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM live_fight_stat_samples WHERE fight_id = %s",
            (fight_id,),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0


def delete_live_fight_stat_samples(connection: PgConnection, fight_id: int) -> None:
    """Borra la serie de muestras de una pelea (cancelada/aplazada a mitad de
    evento): sin serie, la película no pinta walkouts de algo que no ocurrió."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM live_fight_stat_samples WHERE fight_id = %s", (fight_id,)
        )


def last_in_progress_clock(connection: PgConnection, fight_id: int) -> str | None:
    """Último reloj visto con la pelea EN CURSO, en formato mm:ss.

    Lo usa `elapsed_end_time` para distinguir la cuenta atrás congelada del
    tiempo transcurrido: una cuenta atrás nunca sube dentro del asalto. Se pide
    solo al escribir el resultado (una vez por pelea), no en cada pasada.
    Devuelve None si aún no hay serie — entonces se asume cuenta atrás, que es
    lo que ESPN sirve mientras el combate está vivo.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT display_clock
            FROM live_fight_stat_samples
            WHERE fight_id = %s
              AND state = 'in'
              AND display_clock ~ '^[0-9]+:[0-9]{2}$'
            ORDER BY sampled_at DESC
            LIMIT 1
            """,
            (fight_id,),
        )
        row = cursor.fetchone()
    return row[0] if row else None


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
