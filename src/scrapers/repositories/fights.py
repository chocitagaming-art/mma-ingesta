from __future__ import annotations

from psycopg2.extensions import connection as PgConnection

from ..models import FightRecord, FightStatsRecord


def upsert_fight(connection: PgConnection, fight: FightRecord) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fights (
                event_id, fighter_red_id, fighter_blue_id, weight_class, weight_grams,
                scheduled_rounds, winner_id, method, end_round, end_time, odds_red,
                odds_blue, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, source_id)
            DO UPDATE SET
                event_id = EXCLUDED.event_id,
                fighter_red_id = EXCLUDED.fighter_red_id,
                fighter_blue_id = EXCLUDED.fighter_blue_id,
                weight_class = EXCLUDED.weight_class,
                weight_grams = EXCLUDED.weight_grams,
                scheduled_rounds = EXCLUDED.scheduled_rounds,
                winner_id = EXCLUDED.winner_id,
                method = EXCLUDED.method,
                end_round = EXCLUDED.end_round,
                end_time = EXCLUDED.end_time,
                odds_red = EXCLUDED.odds_red,
                odds_blue = EXCLUDED.odds_blue,
                updated_at = NOW()
            RETURNING id
            """,
            (
                fight.event_id,
                fight.fighter_red_id,
                fight.fighter_blue_id,
                fight.weight_class,
                fight.weight_grams,
                fight.scheduled_rounds,
                fight.winner_id,
                fight.method,
                fight.end_round,
                fight.end_time,
                fight.odds_red,
                fight.odds_blue,
                fight.source,
                fight.source_id,
            ),
        )
        return int(cursor.fetchone()[0])


def upsert_fight_stats(connection: PgConnection, stats: FightStatsRecord) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fight_stats (
                fight_id, fighter_id, sig_strikes_landed, sig_strikes_attempted,
                takedowns_landed, takedowns_attempted, submission_attempts,
                control_time_seconds, knockdowns
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fight_id, fighter_id)
            DO UPDATE SET
                sig_strikes_landed = EXCLUDED.sig_strikes_landed,
                sig_strikes_attempted = EXCLUDED.sig_strikes_attempted,
                takedowns_landed = EXCLUDED.takedowns_landed,
                takedowns_attempted = EXCLUDED.takedowns_attempted,
                submission_attempts = EXCLUDED.submission_attempts,
                control_time_seconds = EXCLUDED.control_time_seconds,
                knockdowns = EXCLUDED.knockdowns
            """,
            (
                stats.fight_id,
                stats.fighter_id,
                stats.sig_strikes_landed,
                stats.sig_strikes_attempted,
                stats.takedowns_landed,
                stats.takedowns_attempted,
                stats.submission_attempts,
                stats.control_time_seconds,
                stats.knockdowns,
            ),
        )


def list_fights_for_winner_repair(connection: PgConnection) -> list[tuple[int, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, source_id
            FROM fights
            WHERE source = 'ufcstats'
            ORDER BY id ASC
            """
        )
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def update_fight_winner(connection: PgConnection, fight_id: int, winner_id: int | None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fights
            SET winner_id = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (winner_id, fight_id),
        )


def get_fight_corner_assignment(connection: PgConnection, fight_id: int) -> tuple[int, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fighter_red_id, fighter_blue_id
            FROM fights
            WHERE id = %s
            """,
            (fight_id,),
        )
        row = cursor.fetchone()
        return int(row[0]), int(row[1])


def swap_fight_corners(connection: PgConnection, fight_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fights
            SET fighter_red_id = fighter_blue_id,
                fighter_blue_id = fighter_red_id,
                updated_at = NOW()
            WHERE id = %s
            """,
            (fight_id,),
        )