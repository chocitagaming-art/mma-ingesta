"""Repositorio de fight_history_espn (migración 016, S3-G).

Tabla APARTE de `fights` a propósito: el historial no-UFC (Bellator y
regionales) que pinta la ficha vive aquí y NUNCA entra en el dataset del
modelo ni en las stats/rachas solo-UFC (que leen `fights`). Ver la cabecera
de db/migrations/016_espn_history.sql.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from psycopg2.extensions import connection as PgConnection


@dataclass(frozen=True)
class EspnHistoryRecord:
    fighter_id: int
    espn_competition_id: str
    espn_event_id: str | None
    league_id: str | None
    promotion: str | None
    event_name: str | None
    event_date: date | None
    opponent_name: str | None
    opponent_espn_id: str | None
    opponent_fighter_id: int | None
    result: str | None  # win / loss / draw / nc
    method: str | None
    end_round: int | None
    end_time: str | None
    is_title_fight: bool


def upsert_espn_history(connection: PgConnection, record: EspnHistoryRecord) -> bool:
    """Upsert idempotente por (fighter_id, espn_competition_id).

    El refresco GANA (ESPN puede corregir un resultado a NC o afinar el
    método), salvo opponent_fighter_id, donde un re-run que ya no resuelva al
    rival no debe borrar un enlace logrado antes (COALESCE con lo almacenado).
    Devuelve True si la fila se insertó o cambió de verdad (xmax=0 detecta el
    INSERT; el WHERE IS DISTINCT FROM evita churn en re-runs idénticos).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fight_history_espn (
                fighter_id, espn_competition_id, espn_event_id, league_id,
                promotion, event_name, event_date, opponent_name,
                opponent_espn_id, opponent_fighter_id, result, method,
                end_round, end_time, is_title_fight
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fighter_id, espn_competition_id)
            DO UPDATE SET
                espn_event_id = EXCLUDED.espn_event_id,
                league_id = EXCLUDED.league_id,
                promotion = EXCLUDED.promotion,
                event_name = EXCLUDED.event_name,
                event_date = EXCLUDED.event_date,
                opponent_name = EXCLUDED.opponent_name,
                opponent_espn_id = EXCLUDED.opponent_espn_id,
                opponent_fighter_id = COALESCE(
                    EXCLUDED.opponent_fighter_id, fight_history_espn.opponent_fighter_id
                ),
                result = EXCLUDED.result,
                method = EXCLUDED.method,
                end_round = EXCLUDED.end_round,
                end_time = EXCLUDED.end_time,
                is_title_fight = EXCLUDED.is_title_fight,
                updated_at = NOW()
            WHERE
                (fight_history_espn.espn_event_id, fight_history_espn.league_id,
                 fight_history_espn.promotion, fight_history_espn.event_name,
                 fight_history_espn.event_date, fight_history_espn.opponent_name,
                 fight_history_espn.opponent_espn_id, fight_history_espn.result,
                 fight_history_espn.method, fight_history_espn.end_round,
                 fight_history_espn.end_time, fight_history_espn.is_title_fight)
                IS DISTINCT FROM
                (EXCLUDED.espn_event_id, EXCLUDED.league_id,
                 EXCLUDED.promotion, EXCLUDED.event_name,
                 EXCLUDED.event_date, EXCLUDED.opponent_name,
                 EXCLUDED.opponent_espn_id, EXCLUDED.result,
                 EXCLUDED.method, EXCLUDED.end_round,
                 EXCLUDED.end_time, EXCLUDED.is_title_fight)
                OR fight_history_espn.opponent_fighter_id IS DISTINCT FROM COALESCE(
                    EXCLUDED.opponent_fighter_id, fight_history_espn.opponent_fighter_id
                )
            """,
            (
                record.fighter_id,
                record.espn_competition_id,
                record.espn_event_id,
                record.league_id,
                record.promotion,
                record.event_name,
                record.event_date,
                record.opponent_name,
                record.opponent_espn_id,
                record.opponent_fighter_id,
                record.result,
                record.method,
                record.end_round,
                record.end_time,
                record.is_title_fight,
            ),
        )
        return cursor.rowcount > 0


def get_espn_id_to_fighter_map(connection: PgConnection) -> dict[str, int]:
    """Mapa athlete-id de ESPN -> fighters.id para enlazar rivales.

    Une las dos representaciones de identidad ESPN del esquema: la columna
    nueva espn_id (migración 016) y el legado source='espn' + source_id.
    Si ambas existen para el mismo atleta, espn_id (más reciente) gana.
    """
    mapping: dict[str, int] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT source_id, id FROM fighters WHERE source = 'espn' AND source_id IS NOT NULL"
        )
        for source_id, fighter_id in cursor.fetchall():
            mapping[str(source_id)] = int(fighter_id)
        cursor.execute("SELECT espn_id, id FROM fighters WHERE espn_id IS NOT NULL")
        for espn_id, fighter_id in cursor.fetchall():
            mapping[str(espn_id)] = int(fighter_id)
    return mapping


def stamp_history_checked(connection: PgConnection, fighter_id: int) -> None:
    """Sella el barrido de historial de un luchador (scope incremental del cron)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE fighters SET espn_history_checked_at = NOW() WHERE id = %s",
            (fighter_id,),
        )


def transfer_espn_assets(cursor, from_id: int, to_id: int) -> None:
    """Antes de que una fusión borre al duplicado `from_id`: mueve su historial
    ESPN y su identidad espn_id al keeper `to_id`.

    Sin esto, el DELETE FROM fighters del duplicado dispararía el ON DELETE
    CASCADE de la migración 016 y destruiría en silencio las filas de
    fight_history_espn (hallazgo de la revisión adversarial de S3-G: la guarda
    3 no cubría el camino de borrado). A nivel de cursor porque los tres
    scripts de fusión (consolidate_fighters, merge_duplicate_fighters,
    reconcile) lo invocan dentro de su propia transacción, justo antes de su
    DELETE. No-op si la migración 016 no está aplicada.
    """
    cursor.execute("SELECT to_regclass('fight_history_espn') IS NOT NULL")
    row = cursor.fetchone()
    if not row or not row[0]:
        return
    # Peleas que el keeper ya tiene (misma competición ESPN): la copia del
    # duplicado sobra y chocaría con UNIQUE(fighter_id, espn_competition_id).
    cursor.execute(
        """
        DELETE FROM fight_history_espn
        WHERE fighter_id = %s
          AND EXISTS (
            SELECT 1 FROM fight_history_espn existing
            WHERE existing.fighter_id = %s
              AND existing.espn_competition_id = fight_history_espn.espn_competition_id
          )
        """,
        (from_id, to_id),
    )
    cursor.execute(
        "UPDATE fight_history_espn SET fighter_id = %s WHERE fighter_id = %s",
        (to_id, from_id),
    )
    # Enlaces de rival: reasignar (el ON DELETE SET NULL los perdería) y
    # anular los que quedarían apuntándose a sí mismos tras la fusión.
    cursor.execute(
        "UPDATE fight_history_espn SET opponent_fighter_id = %s WHERE opponent_fighter_id = %s",
        (to_id, from_id),
    )
    cursor.execute(
        "UPDATE fight_history_espn SET opponent_fighter_id = NULL "
        "WHERE fighter_id = %s AND opponent_fighter_id = %s",
        (to_id, to_id),
    )
    # Identidad ESPN: liberar la del duplicado y heredarla si el keeper no
    # tiene (espn_history_checked_at a NULL para que el cron re-barra la
    # carrera bajo la identidad fusionada).
    cursor.execute("SELECT espn_id FROM fighters WHERE id = %s", (from_id,))
    row = cursor.fetchone()
    duplicate_espn_id = row[0] if row else None
    if duplicate_espn_id:
        cursor.execute("UPDATE fighters SET espn_id = NULL WHERE id = %s", (from_id,))
        cursor.execute(
            """
            UPDATE fighters
            SET espn_id = %s, espn_history_checked_at = NULL, updated_at = NOW()
            WHERE id = %s AND espn_id IS NULL
            """,
            (duplicate_espn_id, to_id),
        )
