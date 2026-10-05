from __future__ import annotations

import pandas as pd
from psycopg2.extensions import connection as PgConnection

from src.scrapers.db import connect, cursor

from .preufc import ESPN_HISTORY_COLUMNS

# Phase 4 pre-UFC block (preufc.py). NO league filter and no WHERE at all, as in the
# 30-sep experiment's HISTORY_SQL: league 3321 (the Contender Series) is the arm that
# won it. The scraper already keeps UFC bouts out of this table, and the strict date
# cut is applied per bout by espn_features_for_fighter, never here.
ESPN_HISTORY_SQL = """
    SELECT id, fighter_id, event_date, result, method, is_title_fight, league_id
    FROM fight_history_espn
    ORDER BY fighter_id, event_date, id
"""

# Fighters whose ESPN history is KNOWN: linked to an ESPN athlete and swept at least
# once by the Tuesday cron (espn_fight_history stamps espn_history_checked_at after
# each successful sweep). Anyone else is "unknown history" for build_preufc_block.
ESPN_KNOWN_FIGHTERS_SQL = """
    SELECT id
    FROM fighters
    WHERE espn_id IS NOT NULL
        AND espn_history_checked_at IS NOT NULL
"""


def load_base_dataframe(database_url: str) -> pd.DataFrame:
    query = """
        SELECT
            fights.id AS fight_id,
            events.event_date,
            fights.event_id,
            fights.fighter_red_id,
            fights.fighter_blue_id,
            fights.winner_id,
            fights.method,
            fights.end_round,
            -- end_time ('M:SS' dentro del asalto) e is_title_fight alimentan las
            -- señales de dominio del modelo de MÉTODO: cuánto duran de media las
            -- peleas previas de cada esquina y si el combate es de título.
            fights.end_time,
            fights.is_title_fight,
            fights.scheduled_rounds,
            fights.weight_class,
            red.birth_date AS red_birth_date,
            red.height_cm AS red_height_cm,
            red.reach_cm AS red_reach_cm,
            blue.birth_date AS blue_birth_date,
            blue.height_cm AS blue_height_cm,
            blue.reach_cm AS blue_reach_cm,
            red_stats.sig_strikes_landed AS red_sig_strikes_landed,
            red_stats.sig_strikes_attempted AS red_sig_strikes_attempted,
            red_stats.takedowns_landed AS red_takedowns_landed,
            red_stats.takedowns_attempted AS red_takedowns_attempted,
            red_stats.submission_attempts AS red_submission_attempts,
            red_stats.control_time_seconds AS red_control_time_seconds,
            red_stats.knockdowns AS red_knockdowns,
            blue_stats.sig_strikes_landed AS blue_sig_strikes_landed,
            blue_stats.sig_strikes_attempted AS blue_sig_strikes_attempted,
            blue_stats.takedowns_landed AS blue_takedowns_landed,
            blue_stats.takedowns_attempted AS blue_takedowns_attempted,
            blue_stats.submission_attempts AS blue_submission_attempts,
            blue_stats.control_time_seconds AS blue_control_time_seconds,
            blue_stats.knockdowns AS blue_knockdowns
        FROM fights
        INNER JOIN events ON events.id = fights.event_id
        INNER JOIN fighters AS red ON red.id = fights.fighter_red_id
        INNER JOIN fighters AS blue ON blue.id = fights.fighter_blue_id
        LEFT JOIN fight_stats AS red_stats
            ON red_stats.fight_id = fights.id
            AND red_stats.fighter_id = fights.fighter_red_id
        LEFT JOIN fight_stats AS blue_stats
            ON blue_stats.fight_id = fights.id
            AND blue_stats.fighter_id = fights.fighter_blue_id
        -- A cancelled bout never happened: kept, it reached the fight history as
        -- a prior fight with result 'other' and moved days_since_last_fight,
        -- total_prior_fights, wins_last_5... NULL status is a normal fight (the
        -- reactivation path writes NULL back), hence NULL-safe, never <>.
        WHERE events.event_date IS NOT NULL
            AND fights.status IS DISTINCT FROM 'cancelled'
        ORDER BY events.event_date ASC, fights.id ASC
    """
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute(query)
            dataframe = pd.DataFrame(db_cursor.fetchall())
    dataframe["event_date"] = pd.to_datetime(dataframe["event_date"]).dt.date
    dataframe["red_birth_date"] = pd.to_datetime(dataframe["red_birth_date"]).dt.date
    dataframe["blue_birth_date"] = pd.to_datetime(dataframe["blue_birth_date"]).dt.date
    return dataframe


def load_rankings_dataframe(database_url: str) -> pd.DataFrame:
    query = """
        SELECT
            fighter_id,
            division,
            rank_position,
            snapshot_date
        FROM rankings
        ORDER BY snapshot_date ASC, fighter_id ASC
    """
    with connect(database_url) as connection:
        with cursor(connection) as db_cursor:
            db_cursor.execute(query)
            dataframe = pd.DataFrame(db_cursor.fetchall())
    if dataframe.empty:
        return dataframe
    dataframe["snapshot_date"] = pd.to_datetime(dataframe["snapshot_date"]).dt.date
    return dataframe


def fetch_espn_history_rows(connection: PgConnection) -> list[dict]:
    """The raw rows of ESPN_HISTORY_SQL, as psycopg2 hands them back (int, date,
    str, bool, None). preufc_snapshot.py freezes exactly these."""
    with cursor(connection) as db_cursor:
        db_cursor.execute(ESPN_HISTORY_SQL)
        return db_cursor.fetchall()


def load_espn_history_dataframe(connection: PgConnection) -> pd.DataFrame:
    """Every fight_history_espn row the pre-UFC block reads, DWCS included.

    Takes an open connection (read-only is enough) so the caller decides whether it
    is pooled. An empty table still yields the ESPN_HISTORY_COLUMNS, so the index
    and the block keep working on it.
    """
    rows = fetch_espn_history_rows(connection)
    return pd.DataFrame(rows, columns=ESPN_HISTORY_COLUMNS)


def load_espn_known_fighter_ids(connection: PgConnection) -> set[int]:
    """Ids of the fighters whose ESPN history is known (ESPN_KNOWN_FIGHTERS_SQL)."""
    with cursor(connection) as db_cursor:
        db_cursor.execute(ESPN_KNOWN_FIGHTERS_SQL)
        rows = db_cursor.fetchall()
    return {int(row["id"]) for row in rows}


def index_espn_history(history: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """fighter_id -> that fighter's rows, sorted stably by (event_date, id).

    Built once so each corner of each bout reads a handful of rows instead of the
    whole table. The input frame is not modified.
    """
    if history.empty:
        return {}
    ordered = history.sort_values(["event_date", "id"], kind="stable")
    return {
        int(fighter_id): group.reset_index(drop=True)
        for fighter_id, group in ordered.groupby("fighter_id", sort=False)
    }
