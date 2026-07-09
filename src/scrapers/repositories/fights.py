from __future__ import annotations

from dataclasses import dataclass

from psycopg2.extensions import connection as PgConnection

from ..models import FightRecord, FightStatsRecord, FightStatsRoundRecord


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
    # Title-fight flag parsed from ufc.com's class text ("... Title Bout").
    # None means "unknown" and never overwrites a stored value (COALESCE);
    # the default keeps old constructions valid.
    is_title_fight: bool | None = None


def delete_upcoming_fights(connection: PgConnection, event_id: int, source: str) -> int:
    """Remove an event's previously-scraped bouts so re-runs reflect card changes.

    LEGACY: the daily upcoming reconciliation no longer deletes — it marks the
    bouts that dropped off the card as status='cancelled' instead (see
    cancel_missing_upcoming_fights), so a fight the user has already seen never
    vanishes. Kept for manual/one-off cleanups only.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM fights WHERE event_id = %s AND source = %s",
            (event_id, source),
        )
        return cursor.rowcount


def cancel_missing_upcoming_fights(
    connection: PgConnection, event_id: int, source: str, kept_fight_ids: list[int]
) -> int:
    """Mark this event's bouts that dropped off the latest scraped card as
    status='cancelled' (instead of deleting them, the old behaviour).

    ``kept_fight_ids`` are the row ids the current scrape just upserted — one
    per bout actually on the card, keyed by ufc.com's per-fight fmid, which
    names the fighter pairing — so every OTHER row of this (event, source) is a
    pairing no longer scheduled. Guards:
      - a bout that already has a result (winner/method) is never touched;
      - already-cancelled rows are skipped (no row churn on every run);
      - a cancelled bout that REAPPEARS is reactivated by upsert_upcoming_fight
        itself (its ON CONFLICT sets status back to NULL).
    Returns the number of rows flipped to cancelled.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fights
            SET status = 'cancelled', updated_at = NOW()
            WHERE event_id = %s AND source = %s
              AND winner_id IS NULL AND method IS NULL
              AND status IS DISTINCT FROM 'cancelled'
              AND NOT (id = ANY(%s))
            """,
            # Sentinel -1 keeps ANY() typed when the card parsed to zero bouts
            # (psycopg2 cannot adapt an empty list); no real row has id -1.
            (event_id, source, list(kept_fight_ids) or [-1]),
        )
        return cursor.rowcount


def reconcile_upcoming_fight_source_id(
    connection: PgConnection,
    event_id: int,
    source: str,
    source_id: str,
    fighter_red_id: int | None,
    fighter_blue_id: int | None,
    fighter_red_name: str,
    fighter_blue_name: str,
) -> int | None:
    """Adopt an existing bout row of the same (event, fighter pairing) whose
    source_id has drifted, relabelling it to the incoming source_id so the
    following upsert_upcoming_fight updates it IN PLACE instead of inserting a
    phantom duplicate. Returns the adopted row id, or None if nothing matched.

    Why this is needed: upcoming bouts are keyed by (source, source_id) where
    source_id is ufc.com's per-fight fmid — which is NOT stable across scrapes.
    An early-prelim first scraped without a data-fmid gets the fallback
    "<event>#<order>"; once UFC assigns the real fmid, a re-scrape can't match
    on (source, source_id) and inserts a NEW row, and the old one — no longer
    among the kept ids — is flipped to 'cancelled'. Result: two rows for the
    same pairing sharing a bout_order (a phantom 'cancelled' + the active one),
    exactly what was seen on event 1060. The logical identity of an upcoming
    bout is its (event, pairing), not the volatile fmid.

    Matching is order-insensitive on the corners and works both by fighter id
    (rows already linked) and by normalized name (rows stored before linking,
    ids NULL) — never on the "TBD" placeholder. Guards:
      - a bout with a result (winner/method) is never adopted (COALESCE policy
        elsewhere already protects decided fights, but this makes it explicit);
      - the relabel is skipped when a row already holds the incoming source_id,
        so the UNIQUE (source, source_id) is never violated — the stale row is
        then simply cancelled by the usual reconciliation (still one active).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fights
            SET source_id = %(source_id)s, updated_at = NOW()
            WHERE id = (
                SELECT id FROM fights
                WHERE event_id = %(event_id)s
                  AND source = %(source)s
                  AND source_id <> %(source_id)s
                  AND winner_id IS NULL AND method IS NULL
                  AND (
                    (fighter_red_id = %(red_id)s AND fighter_blue_id = %(blue_id)s)
                    OR (fighter_red_id = %(blue_id)s AND fighter_blue_id = %(red_id)s)
                    OR (
                        %(red_name)s <> 'TBD' AND %(blue_name)s <> 'TBD'
                        AND (
                          (LOWER(fighter_red_name) = LOWER(%(red_name)s)
                           AND LOWER(fighter_blue_name) = LOWER(%(blue_name)s))
                          OR (LOWER(fighter_red_name) = LOWER(%(blue_name)s)
                              AND LOWER(fighter_blue_name) = LOWER(%(red_name)s))
                        )
                    )
                  )
                ORDER BY id
                LIMIT 1
            )
            AND NOT EXISTS (
                SELECT 1 FROM fights existing
                WHERE existing.source = %(source)s
                  AND existing.source_id = %(source_id)s
            )
            RETURNING id
            """,
            {
                "event_id": event_id,
                "source": source,
                "source_id": source_id,
                "red_id": fighter_red_id,
                "blue_id": fighter_blue_id,
                "red_name": fighter_red_name,
                "blue_name": fighter_blue_name,
            },
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None


def upsert_upcoming_fight(connection: PgConnection, fight: UpcomingFightRecord) -> int:
    """Insert an upcoming bout (no result yet: winner/method/end_* stay NULL).

    A bout present on the scraped card is by definition NOT cancelled, so the
    conflict branch resets status to NULL: a fight cancelled on a previous run
    that reappears on the card is reactivated here. is_title_fight follows the
    no-NULL-overwrite policy (COALESCE both on insert — the column is NOT NULL
    — and on update).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fights (
                event_id, fighter_red_id, fighter_blue_id, fighter_red_name,
                fighter_blue_name, weight_class, scheduled_rounds, bout_order,
                card_segment, is_title_fight, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, FALSE), %s, %s)
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
                -- Not EXCLUDED.is_title_fight: that already went through the
                -- insert-side COALESCE(placeholder, FALSE), so a NULL argument
                -- would arrive as FALSE and stomp a stored TRUE. The raw
                -- argument is bound again (last parameter) so NULL really
                -- means "keep the current value".
                is_title_fight = COALESCE(%s, fights.is_title_fight),
                status = NULL,
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
                fight.is_title_fight,
                fight.source,
                fight.source_id,
                fight.is_title_fight,
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
                -- weight_class/scheduled_rounds come from markup that can drift
                -- (weight cell unparsed -> NULL) or from inference: the #20
                -- no-NULL-overwrite policy applies to them too, so a re-scrape
                -- that resolves nothing never wipes a stored value.
                weight_class = COALESCE(EXCLUDED.weight_class, fights.weight_class),
                scheduled_rounds = COALESCE(EXCLUDED.scheduled_rounds, fights.scheduled_rounds),
                -- Result / separately-enriched columns: a re-scrape that no longer
                -- resolves these (HTML changed, or odds/weight come from other
                -- sources and arrive NULL here) must never wipe a stored value (#20).
                winner_id = COALESCE(EXCLUDED.winner_id, fights.winner_id),
                method = COALESCE(EXCLUDED.method, fights.method),
                end_round = COALESCE(EXCLUDED.end_round, fights.end_round),
                end_time = COALESCE(EXCLUDED.end_time, fights.end_time),
                weight_grams = COALESCE(EXCLUDED.weight_grams, fights.weight_grams),
                odds_red = COALESCE(EXCLUDED.odds_red, fights.odds_red),
                odds_blue = COALESCE(EXCLUDED.odds_blue, fights.odds_blue),
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


def upsert_fight_stats_rounds(connection: PgConnection, stats: FightStatsRoundRecord) -> None:
    """Upsert one fighter's stats for one round (migration 012 / BE4).

    COALESCE on the conflict branch: a re-scrape that fails to parse a metric
    (NULL) never wipes a stored value, mirroring the no-NULL-overwrite policy
    of the fights result columns.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fight_stats_rounds (
                fight_id, fighter_id, round, sig_strikes_landed,
                sig_strikes_attempted, takedowns_landed, takedowns_attempted,
                submission_attempts, control_time_seconds, knockdowns
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fight_id, fighter_id, round)
            DO UPDATE SET
                sig_strikes_landed = COALESCE(EXCLUDED.sig_strikes_landed, fight_stats_rounds.sig_strikes_landed),
                sig_strikes_attempted = COALESCE(EXCLUDED.sig_strikes_attempted, fight_stats_rounds.sig_strikes_attempted),
                takedowns_landed = COALESCE(EXCLUDED.takedowns_landed, fight_stats_rounds.takedowns_landed),
                takedowns_attempted = COALESCE(EXCLUDED.takedowns_attempted, fight_stats_rounds.takedowns_attempted),
                submission_attempts = COALESCE(EXCLUDED.submission_attempts, fight_stats_rounds.submission_attempts),
                control_time_seconds = COALESCE(EXCLUDED.control_time_seconds, fight_stats_rounds.control_time_seconds),
                knockdowns = COALESCE(EXCLUDED.knockdowns, fight_stats_rounds.knockdowns)
            """,
            (
                stats.fight_id,
                stats.fighter_id,
                stats.round,
                stats.sig_strikes_landed,
                stats.sig_strikes_attempted,
                stats.takedowns_landed,
                stats.takedowns_attempted,
                stats.submission_attempts,
                stats.control_time_seconds,
                stats.knockdowns,
            ),
        )


def find_fight_id_by_fighters(
    connection: PgConnection, event_id: int, fighter_a_id: int, fighter_b_id: int
) -> int | None:
    """Fight row of this event pairing these two fighters, either corner order.

    Used by the ESPN live-results updater (BE1), which resolves fighters by
    ESPN athlete id / name and therefore cannot key on (source, source_id).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM fights
            WHERE event_id = %s
              AND (
                (fighter_red_id = %s AND fighter_blue_id = %s)
                OR (fighter_red_id = %s AND fighter_blue_id = %s)
              )
            ORDER BY id
            LIMIT 1
            """,
            (event_id, fighter_a_id, fighter_b_id, fighter_b_id, fighter_a_id),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None


def fill_fight_result(
    connection: PgConnection,
    fight_id: int,
    winner_id: int | None,
    method: str | None,
    end_round: int | None,
    end_time: str | None,
) -> bool:
    """Fill ONLY the still-NULL result columns of a fight (BE1, casi-en-vivo).

    COALESCE(column, %s): the STORED value always wins, so ESPN's provisional
    result can never stomp data already written by ufcstats (better quality).
    The WHERE guard skips the row entirely when nothing would be filled
    (no churn on every 10-minute poll). Returns True if the row changed.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fights
            SET winner_id = COALESCE(winner_id, %s),
                method = COALESCE(method, %s),
                end_round = COALESCE(end_round, %s),
                end_time = COALESCE(end_time, %s),
                updated_at = NOW()
            WHERE id = %s
              AND (
                (winner_id IS NULL AND %s IS NOT NULL)
                OR (method IS NULL AND %s IS NOT NULL)
                OR (end_round IS NULL AND %s IS NOT NULL)
                OR (end_time IS NULL AND %s IS NOT NULL)
              )
            """,
            (
                winner_id, method, end_round, end_time, fight_id,
                winner_id, method, end_round, end_time,
            ),
        )
        return cursor.rowcount > 0


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
