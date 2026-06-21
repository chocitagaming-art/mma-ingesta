from __future__ import annotations

from dataclasses import dataclass

from psycopg2.extensions import connection as PgConnection

from ..models import FightRecord, FightStatsRecord


@dataclass(frozen=True)
class UpcomingFightRecord:
    event_id: int
    fighter_red_id: int | None
    fighter_blue_id: int | None
    fighter_red_name: str
    fighter_blue_name: str
    weight_class: str | None
    scheduled_rounds: int | None
    bout_order: int
    card_segment: str | None
    source: str
    source_id: str


def delete_upcoming_fights(connection: PgConnection, event_id: int, source: str) -> int:
    """Remove an event's previously-scraped bouts so re-runs reflect card changes."""
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM fights WHERE event_id = %s AND source = %s",
            (event_id, source),
        )
        return cursor.rowcount


def upsert_upcoming_fight(connection: PgConnection, fight: UpcomingFightRecord) -> int:
    """Insert an upcoming bout (no result yet: winner/method/end_* stay NULL)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fights (
                event_id, fighter_red_id, fighter_blue_id, fighter_red_name,
                fighter_blue_name, weight_class, scheduled_rounds, bout_order,
                card_segment, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, source_id)
            DO UPDATE SET
                event_id = EXCLUDED.event_id,
                fighter_red_id = EXCLUDED.fighter_red_id,
                fighter_blue_id = EXCLUDED.fighter_blue_id,
                fighter_red_name = EXCLUDED.fighter_red_name,
                fighter_blue_name = EXCLUDED.fighter_blue_name,
                weight_class = EXCLUDED.weight_class,
                scheduled_rounds = EXCLUDED.scheduled_rounds,
                bout_order = EXCLUDED.bout_order,
                card_segment = EXCLUDED.card_segment,
                updated_at = NOW()
            RETURNING id
            """,
            (
                fight.event_id,
                fight.fighter_red_id,
                fight.fighter_blue_id,
                fight.fighter_red_name,
                fight.fighter_blue_name,
                fight.weight_class,
                fight.scheduled_rounds,
                fight.bout_order,
                fight.card_segment,
                fight.source,
                fight.source_id,
            ),
        )
        return int(cursor.fetchone()[0])


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
                control_time_seconds, knockdowns,
                sig_str_head_landed, sig_str_head_attempted,
                sig_str_body_landed, sig_str_body_attempted,
                sig_str_leg_landed, sig_str_leg_attempted,
                sig_str_distance_landed, sig_str_distance_attempted,
                sig_str_clinch_landed, sig_str_clinch_attempted,
                sig_str_ground_landed, sig_str_ground_attempted
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fight_id, fighter_id)
            DO UPDATE SET
                sig_strikes_landed = EXCLUDED.sig_strikes_landed,
                sig_strikes_attempted = EXCLUDED.sig_strikes_attempted,
                takedowns_landed = EXCLUDED.takedowns_landed,
                takedowns_attempted = EXCLUDED.takedowns_attempted,
                submission_attempts = EXCLUDED.submission_attempts,
                control_time_seconds = EXCLUDED.control_time_seconds,
                knockdowns = EXCLUDED.knockdowns,
                sig_str_head_landed = EXCLUDED.sig_str_head_landed,
                sig_str_head_attempted = EXCLUDED.sig_str_head_attempted,
                sig_str_body_landed = EXCLUDED.sig_str_body_landed,
                sig_str_body_attempted = EXCLUDED.sig_str_body_attempted,
                sig_str_leg_landed = EXCLUDED.sig_str_leg_landed,
                sig_str_leg_attempted = EXCLUDED.sig_str_leg_attempted,
                sig_str_distance_landed = EXCLUDED.sig_str_distance_landed,
                sig_str_distance_attempted = EXCLUDED.sig_str_distance_attempted,
                sig_str_clinch_landed = EXCLUDED.sig_str_clinch_landed,
                sig_str_clinch_attempted = EXCLUDED.sig_str_clinch_attempted,
                sig_str_ground_landed = EXCLUDED.sig_str_ground_landed,
                sig_str_ground_attempted = EXCLUDED.sig_str_ground_attempted
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
                stats.sig_str_head_landed,
                stats.sig_str_head_attempted,
                stats.sig_str_body_landed,
                stats.sig_str_body_attempted,
                stats.sig_str_leg_landed,
                stats.sig_str_leg_attempted,
                stats.sig_str_distance_landed,
                stats.sig_str_distance_attempted,
                stats.sig_str_clinch_landed,
                stats.sig_str_clinch_attempted,
                stats.sig_str_ground_landed,
                stats.sig_str_ground_attempted,
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