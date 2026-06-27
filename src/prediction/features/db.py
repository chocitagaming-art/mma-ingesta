from __future__ import annotations

import pandas as pd

from src.scrapers.db import connect, cursor


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
        WHERE events.event_date IS NOT NULL
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
