from __future__ import annotations

from collections.abc import Iterator, Mapping

import numpy as np
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

# One consistent, read-only view for the SELECTs that follow on the same connection
# (begin_read_only_snapshot). It has to be the FIRST statement of the transaction,
# and it lasts only for that transaction, so a pooled connection keeps its defaults.
READ_ONLY_SNAPSHOT_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"


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


def begin_read_only_snapshot(connection: PgConnection) -> None:
    """Make the rest of this transaction ONE read-only REPEATABLE READ snapshot.

    Call it before any other statement on a connection that is not in a
    transaction yet (a fresh one, or one the pool rolled back on return): psycopg2
    opens the transaction with this statement, every later SELECT sees what was
    committed when the first of them ran, and the setting ends with the
    transaction (src.scrapers.db.connect rolls back a pooled connection, a direct
    one is closed). On an autocommit connection PostgreSQL only warns and the
    SELECTs fall back to one READ COMMITTED transaction each."""
    with cursor(connection) as db_cursor:
        db_cursor.execute(READ_ONLY_SNAPSHOT_SQL)


def fetch_espn_history_rows(connection: PgConnection) -> list[dict]:
    """The raw rows of ESPN_HISTORY_SQL, as psycopg2 hands them back (int, date,
    str, bool, None), one dict per row. preufc_snapshot.py freezes exactly these;
    load_espn_history_dataframe runs the same SQL with a plain cursor."""
    with cursor(connection) as db_cursor:
        db_cursor.execute(ESPN_HISTORY_SQL)
        return db_cursor.fetchall()


def load_espn_history_dataframe(connection: PgConnection) -> pd.DataFrame:
    """Every fight_history_espn row the pre-UFC block reads, DWCS included.

    Takes an open connection (read-only is enough) so the caller decides whether it
    is pooled. An empty table still yields the ESPN_HISTORY_COLUMNS, so the index
    and the block keep working on it.

    Plain tuples, not RealDictCursor rows: the frame is the same one (rows, order,
    dtypes and values, pinned by tests/test_preufc_history_memory.py), but the ~36k
    dicts a RealDictCursor builds and throws away are never made. In the
    long-lived service they left heap behind at every refresh (B6, steady RSS).
    """
    with connection.cursor() as db_cursor:
        db_cursor.execute(ESPN_HISTORY_SQL)
        rows = db_cursor.fetchall()
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
    whole table. The input frame is not modified. The training CSV uses it;
    the long-lived service keeps CompactEspnIndex instead (same rows, less RAM).
    """
    if history.empty:
        return {}
    ordered = history.sort_values(["event_date", "id"], kind="stable")
    return {
        int(fighter_id): group.reset_index(drop=True)
        for fighter_id, group in ordered.groupby("fighter_id", sort=False)
    }


class CompactEspnIndex(Mapping[int, pd.DataFrame]):
    """index_espn_history as ONE frame plus offsets, for the long-lived service.

    A dict of ~2,600 small DataFrames costs far more than their rows (B6: 10.8
    MiB of index for a 10.8 MiB table, +42 MiB of steady RSS on Windows). This
    keeps a single copy of the table, sorted by fighter, and each fighter's
    [start, stop) in it. ``index[fighter_id]`` hands back exactly the frame
    index_espn_history would: the same rows in the same order (stably by
    (event_date, id), then the input order), the same dtypes and a fresh 0..n-1
    index (tests/test_preufc_history_memory.py). Missing fighters raise KeyError,
    so ``.get(fighter_id, default)`` works as on the dict. Read-only: nothing in
    the block writes to the frames it is handed. Iteration goes by fighter id.

    The input frame is not modified."""

    def __init__(self, history: pd.DataFrame) -> None:
        # The same stable (event_date, id) sort as index_espn_history, THEN a stable
        # sort by fighter: inside each fighter the first order is kept exactly.
        # groupby drops a NULL key; so does this (fighter_id is NOT NULL anyway).
        ordered = history.sort_values(["event_date", "id"], kind="stable")
        ordered = ordered[ordered["fighter_id"].notna()]
        self._frame = ordered.sort_values("fighter_id", kind="stable").reset_index(
            drop=True
        )
        fighter_ids = self._frame["fighter_id"].to_numpy()
        if len(fighter_ids) == 0:
            self._bounds: dict[int, tuple[int, int]] = {}
            return
        starts = np.flatnonzero(np.r_[True, fighter_ids[1:] != fighter_ids[:-1]])
        stops = np.r_[starts[1:], len(fighter_ids)]
        self._bounds = {
            int(fighter_ids[start]): (int(start), int(stop))
            for start, stop in zip(starts, stops)
        }

    def __getitem__(self, fighter_id: int) -> pd.DataFrame:
        start, stop = self._bounds[fighter_id]
        return self._frame.iloc[start:stop].reset_index(drop=True)

    def __iter__(self) -> Iterator[int]:
        return iter(self._bounds)

    def __len__(self) -> int:
        return len(self._bounds)

    def __contains__(self, fighter_id: object) -> bool:
        return fighter_id in self._bounds
