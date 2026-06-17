from __future__ import annotations

from psycopg2.extensions import connection as PgConnection

from ..models import FighterRecord


def upsert_fighter(connection: PgConnection, fighter: FighterRecord) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fighters (
                name, nickname, nationality, birth_date, height_cm, reach_cm, stance,
                weight_grams, wins, losses, draws, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, source_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                nickname = EXCLUDED.nickname,
                nationality = EXCLUDED.nationality,
                birth_date = EXCLUDED.birth_date,
                height_cm = EXCLUDED.height_cm,
                reach_cm = EXCLUDED.reach_cm,
                stance = EXCLUDED.stance,
                weight_grams = EXCLUDED.weight_grams,
                wins = EXCLUDED.wins,
                losses = EXCLUDED.losses,
                draws = EXCLUDED.draws,
                updated_at = NOW()
            RETURNING id
            """,
            (
                fighter.name,
                fighter.nickname,
                fighter.nationality,
                fighter.birth_date,
                fighter.height_cm,
                fighter.reach_cm,
                fighter.stance,
                fighter.weight_grams,
                fighter.wins,
                fighter.losses,
                fighter.draws,
                fighter.source,
                fighter.source_id,
            ),
        )
        return int(cursor.fetchone()[0])


def get_fighter_id_by_source(connection: PgConnection, source: str, source_id: str) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM fighters WHERE source = %s AND source_id = %s",
            (source, source_id),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None