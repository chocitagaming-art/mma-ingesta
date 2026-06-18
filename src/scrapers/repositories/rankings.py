from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from psycopg2.extensions import connection as PgConnection


@dataclass(frozen=True)
class RankingRecord:
    fighter_id: int | None
    promotion_id: int
    division: str
    rank_position: int
    snapshot_date: date
    is_champion: bool
    fighter_name: str
    rank_change: int | None


def ensure_ufc_promotion(connection: PgConnection) -> int:
    """Return the UFC promotion id, creating the row if it does not exist."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO promotions (name, slug)
            VALUES ('UFC', 'ufc')
            ON CONFLICT (name) DO UPDATE SET slug = EXCLUDED.slug
            RETURNING id
            """
        )
        return int(cursor.fetchone()[0])


def delete_rankings_for_snapshot(
    connection: PgConnection, promotion_id: int, snapshot_date: date
) -> int:
    """Remove an existing snapshot so the scraper can be re-run idempotently."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM rankings WHERE promotion_id = %s AND snapshot_date = %s",
            (promotion_id, snapshot_date),
        )
        return cursor.rowcount


def insert_ranking(connection: PgConnection, ranking: RankingRecord) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO rankings (
                fighter_id, promotion_id, division, rank_position, snapshot_date,
                is_champion, fighter_name, rank_change
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fighter_id, promotion_id, division, snapshot_date)
            DO UPDATE SET
                rank_position = EXCLUDED.rank_position,
                is_champion = EXCLUDED.is_champion,
                fighter_name = EXCLUDED.fighter_name,
                rank_change = EXCLUDED.rank_change
            """,
            (
                ranking.fighter_id,
                ranking.promotion_id,
                ranking.division,
                ranking.rank_position,
                ranking.snapshot_date,
                ranking.is_champion,
                ranking.fighter_name,
                ranking.rank_change,
            ),
        )
