"""Cancelled bouts must not enter the prediction model's fight history.

Measured on 1-oct-2026: ``load_base_dataframe`` did not filter
``fights.status``, so a bout flipped to ``status='cancelled'`` (no winner, no
method) reached ``build_fighter_history_dataframe`` as a prior fight with
result ``'other'``. It moved ``days_since_last_fight`` (Ortega: the Moicano
bout cancelled on 19-sep counted as his last fight), ``total_prior_fights``,
``wins_last_5``, the opponent-quality signal and ``lowConfidence``. NULL status
is a normal fight (and the reactivation path writes NULL back), so the filter
must be NULL-safe: ``IS DISTINCT FROM``, never ``<>``.

Second half: ``_get_latest_matchup_context`` treated ANY bout without a winner
as the pending one, so an old draw / no contest (winner NULL but method set)
became the anchor of a pair that has a later decided rematch. Pending means no
winner AND no method.

The loader tests run the REAL query against an in-memory SQLite (no network,
no Neon): a mutant that only keeps the predicate's text (``... AND false``, a
JOIN condition gone wrong, ``<>`` dropping NULL rows) changes the rows that
come back, so the behaviour assertions catch what a text-only check would not.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest

from src.prediction import api
from src.prediction.features import db as features_db


# --------------------------------------------------------------- SQLite fake

SCHEMA = """
CREATE TABLE events (id INTEGER PRIMARY KEY, event_date TEXT);
CREATE TABLE fighters (
    id INTEGER PRIMARY KEY, birth_date TEXT, height_cm REAL, reach_cm REAL
);
CREATE TABLE fights (
    id INTEGER PRIMARY KEY,
    event_id INTEGER,
    fighter_red_id INTEGER,
    fighter_blue_id INTEGER,
    winner_id INTEGER,
    method TEXT,
    end_round INTEGER,
    end_time TEXT,
    is_title_fight INTEGER,
    scheduled_rounds INTEGER,
    weight_class TEXT,
    status TEXT
);
CREATE TABLE fight_stats (
    fight_id INTEGER,
    fighter_id INTEGER,
    sig_strikes_landed INTEGER,
    sig_strikes_attempted INTEGER,
    takedowns_landed INTEGER,
    takedowns_attempted INTEGER,
    submission_attempts INTEGER,
    control_time_seconds INTEGER,
    knockdowns INTEGER
);
"""

# Fighters: HOOKER and ORTEGA meet on 21-nov (pending). ORTEGA's Moicano bout of
# 19-sep was cancelled. ORTEGA's last REAL fight is 13-oct-2025, 404 days before
# the pending bout — the number the 1-oct measurement expected for him.
HOOKER, ORTEGA, MOICANO, OTHER = 1, 2, 3, 4

DECIDED_HOOKER = 100    # 2024-06-01, status NULL, Hooker beats OTHER
DECIDED_ORTEGA = 101    # 2025-10-13, status NULL, Ortega beats OTHER
CANCELLED = 102         # 2026-09-19, status 'cancelled', Moicano vs Ortega
PENDING = 103           # 2026-11-21, status NULL, Hooker vs Ortega, no result yet

PENDING_DATE = date(2026, 11, 21)
ORTEGA_LAST_REAL_FIGHT = date(2025, 10, 13)
CANCELLED_DATE = date(2026, 9, 19)


def _seed(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO events (id, event_date) VALUES (?, ?)",
        [
            (10, "2024-06-01"),
            (11, ORTEGA_LAST_REAL_FIGHT.isoformat()),
            (12, CANCELLED_DATE.isoformat()),
            (13, PENDING_DATE.isoformat()),
        ],
    )
    connection.executemany(
        "INSERT INTO fighters (id, birth_date, height_cm, reach_cm) VALUES (?, ?, ?, ?)",
        [
            (HOOKER, "1990-02-13", 183.0, 191.0),
            (ORTEGA, "1991-02-21", 173.0, 175.0),
            (MOICANO, "1989-05-21", 180.0, 183.0),
            (OTHER, "1992-01-01", 178.0, 180.0),
        ],
    )
    connection.executemany(
        """INSERT INTO fights (id, event_id, fighter_red_id, fighter_blue_id, winner_id,
               method, end_round, end_time, is_title_fight, scheduled_rounds,
               weight_class, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (DECIDED_HOOKER, 10, HOOKER, OTHER, HOOKER, "KO/TKO", 1, "2:30", 0, 3, "Lightweight", None),
            (DECIDED_ORTEGA, 11, ORTEGA, OTHER, ORTEGA, "SUB", 2, "4:10", 0, 3, "Lightweight", None),
            (CANCELLED, 12, MOICANO, ORTEGA, None, None, None, None, 0, 3, "Lightweight", "cancelled"),
            (PENDING, 13, HOOKER, ORTEGA, None, None, None, None, 0, 5, "Lightweight", None),
        ],
    )
    stats = (40, 90, 1, 3, 1, 120, 0)
    connection.executemany(
        """INSERT INTO fight_stats (fight_id, fighter_id, sig_strikes_landed,
               sig_strikes_attempted, takedowns_landed, takedowns_attempted,
               submission_attempts, control_time_seconds, knockdowns)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (DECIDED_HOOKER, HOOKER, *stats),
            (DECIDED_HOOKER, OTHER, *stats),
            (DECIDED_ORTEGA, ORTEGA, *stats),
            (DECIDED_ORTEGA, OTHER, *stats),
        ],
    )
    connection.commit()


class _DictCursor:
    """Just enough of psycopg2's RealDictCursor for load_base_dataframe."""

    def __init__(self, connection: sqlite3.Connection, executed: list[str]):
        self._connection = connection
        self._executed = executed
        self._rows: list[sqlite3.Row] = []

    def execute(self, sql, params=None):
        self._executed.append(sql)
        runnable = sql
        # IS DISTINCT FROM landed in SQLite 3.39; older builds (e.g. Ubuntu
        # 22.04's system lib) spell the same NULL-safe comparison as IS NOT.
        if sqlite3.sqlite_version_info < (3, 39, 0):
            runnable = runnable.replace("IS DISTINCT FROM", "IS NOT")
        self._rows = self._connection.execute(runnable, params or ()).fetchall()

    def fetchall(self):
        return [dict(row) for row in self._rows]


@pytest.fixture
def sqlite_base(monkeypatch):
    """Point load_base_dataframe at a seeded in-memory SQLite; return the SQL log."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _seed(connection)
    executed: list[str] = []

    @contextmanager
    def fake_connect(_database_url):
        yield connection

    @contextmanager
    def fake_cursor(conn):
        yield _DictCursor(conn, executed)

    monkeypatch.setattr(features_db, "connect", fake_connect)
    monkeypatch.setattr(features_db, "cursor", fake_cursor)
    yield executed
    connection.close()


def _where_clause(sql: str) -> str:
    normalized = " ".join(sql.split())
    match = re.search(r"\bWHERE (.*?) ORDER BY\b", normalized)
    assert match, normalized
    return match.group(1)


# ------------------------------------------------------- load_base_dataframe


def test_load_base_dataframe_drops_cancelled_and_keeps_null_status(sqlite_base):
    fights_df = features_db.load_base_dataframe("sqlite://unused")

    # The cancelled bout is gone; the NULL-status decided fights AND the
    # NULL-status pending bout stay, in date order.
    assert fights_df["fight_id"].tolist() == [DECIDED_HOOKER, DECIDED_ORTEGA, PENDING]
    # The anchor needs `method` to tell a pending bout from an old draw / NC.
    assert "method" in fights_df.columns
    pending = fights_df.set_index("fight_id").loc[PENDING]
    assert pd.isna(pending["winner_id"]) and pd.isna(pending["method"])


def test_load_base_dataframe_where_clause_is_exactly_the_null_safe_filter(sqlite_base):
    features_db.load_base_dataframe("sqlite://unused")

    assert len(sqlite_base) == 1
    # Exact match, not `in`: an extra `AND false` (or any other conjunct) in
    # the WHERE changes this string, and `<>` would silently drop NULL status.
    assert _where_clause(sqlite_base[0]) == (
        "events.event_date IS NOT NULL "
        "AND fights.status IS DISTINCT FROM 'cancelled'"
    )


def test_cancelled_bout_does_not_move_ortega_history_features(sqlite_base):
    """End to end through the serving builder: the cancelled 19-sep bout must
    not count as Ortega's last fight nor as one more prior fight."""
    fights_df = features_db.load_base_dataframe("sqlite://unused")
    rankings_df = pd.DataFrame(
        columns=["fighter_id", "division", "rank_position", "snapshot_date"]
    )

    _row, _method_row, context, low_confidence = api._build_feature_row(
        fights_df, rankings_df, HOOKER, ORTEGA, physical={}
    )

    assert context["matchupDate"] == PENDING_DATE.isoformat()
    ortega = context["blueHistory"]
    assert ortega["days_since_last_fight"] == (PENDING_DATE - ORTEGA_LAST_REAL_FIGHT).days == 404
    assert ortega["days_since_last_fight"] != (PENDING_DATE - CANCELLED_DATE).days
    assert ortega["total_prior_fights"] == 1
    assert ortega["wins_last_5"] == 1
    assert ortega["win_streak"] == 1
    # One real prior fight each: still a thin history, and flagged as such.
    assert low_confidence is True


# ------------------------------------------------ _get_latest_matchup_context

RED, BLUE, THIRD = 11, 22, 33

# Methods that UFCStats stores for a bout WITHOUT a winner: draws (majority,
# split, unanimous) and no contests. Measured in production on 4-oct-2026; a
# pending bout is the only one with winner AND method NULL.
DRAW_OR_NO_CONTEST_METHODS = [
    "M-DEC",
    "S-DEC",
    "U-DEC",
    "CNC",
    "Overturned",
    "Overturned - Punch",
    "Other",
]


def _bout(
    fight_id: int,
    event_date: date,
    red: int,
    blue: int,
    winner: int | None,
    method: str | None,
    *,
    weight_class: str = "Lightweight",
    scheduled_rounds: int = 3,
    is_title_fight: bool = False,
) -> dict:
    return {
        "fight_id": fight_id,
        "event_date": event_date,
        "fighter_red_id": red,
        "fighter_blue_id": blue,
        "winner_id": winner,
        "method": method,
        "weight_class": weight_class,
        "scheduled_rounds": scheduled_rounds,
        "is_title_fight": is_title_fight,
    }


def _frame(*bouts: dict) -> pd.DataFrame:
    return pd.DataFrame(list(bouts))


@pytest.mark.parametrize("method", DRAW_OR_NO_CONTEST_METHODS)
def test_old_draw_or_no_contest_is_not_the_pending_bout(method):
    """A pair with an old draw / NC and a LATER decided rematch anchors to the
    rematch (their latest meeting), not to the old no-winner bout."""
    fights_df = _frame(
        _bout(1, date(2019, 3, 2), RED, BLUE, None, method, weight_class="Featherweight"),
        _bout(2, date(2021, 7, 10), BLUE, RED, BLUE, "U-DEC", scheduled_rounds=5),
        _bout(3, date(2020, 1, 1), RED, THIRD, RED, "KO/TKO"),
    )

    matchup_date, weight_class, scheduled_rounds, _title = api._get_latest_matchup_context(
        fights_df, RED, BLUE
    )

    assert matchup_date == date(2021, 7, 10)
    assert weight_class == "Lightweight"
    assert scheduled_rounds == 5


@pytest.mark.parametrize("method", DRAW_OR_NO_CONTEST_METHODS)
def test_pending_bout_still_wins_over_old_draw_or_no_contest(method):
    fights_df = _frame(
        _bout(1, date(2019, 3, 2), RED, BLUE, None, method),
        _bout(2, date(2021, 7, 10), RED, BLUE, RED, "SUB"),
        _bout(
            3,
            date(2026, 11, 21),
            BLUE,
            RED,
            None,
            None,
            weight_class="Featherweight",
            scheduled_rounds=5,
            is_title_fight=True,
        ),
    )

    matchup_date, weight_class, scheduled_rounds, is_title = api._get_latest_matchup_context(
        fights_df, RED, BLUE
    )

    assert (matchup_date, weight_class, scheduled_rounds, is_title) == (
        date(2026, 11, 21),
        "Featherweight",
        5,
        True,
    )


def test_pending_bout_is_preferred_even_when_a_decided_meeting_is_later():
    """The pending preference itself (not just "latest wins"): a bout with no
    winner and no method beats a later decided meeting."""
    fights_df = _frame(
        _bout(1, date(2024, 2, 3), RED, BLUE, None, None, scheduled_rounds=5),
        _bout(2, date(2024, 8, 17), RED, BLUE, RED, "KO/TKO"),
    )

    matchup_date, _wc, scheduled_rounds, _title = api._get_latest_matchup_context(
        fights_df, RED, BLUE
    )

    assert matchup_date == date(2024, 2, 3)
    assert scheduled_rounds == 5


def test_pending_needs_both_no_winner_and_no_method():
    """A bout with a winner but no method yet (a result written before its
    method) is not pending either: the latest meeting stays the anchor."""
    fights_df = _frame(
        _bout(1, date(2024, 2, 3), RED, BLUE, RED, None),
        _bout(2, date(2024, 8, 17), RED, BLUE, BLUE, "KO/TKO", scheduled_rounds=5),
    )

    matchup_date, _wc, scheduled_rounds, _title = api._get_latest_matchup_context(
        fights_df, RED, BLUE
    )

    assert matchup_date == date(2024, 8, 17)
    assert scheduled_rounds == 5


def test_pair_without_pending_bout_keeps_anchoring_to_latest_real_meeting():
    """Product behaviour kept on purpose (owner decision pending): a pair that
    only met in the past anchors to that meeting, not to today."""
    fights_df = _frame(
        _bout(1, date(2017, 4, 22), RED, BLUE, BLUE, "U-DEC", weight_class="Featherweight"),
        _bout(2, date(2025, 5, 5), RED, THIRD, RED, "KO/TKO"),
    )

    matchup_date, weight_class, scheduled_rounds, _title = api._get_latest_matchup_context(
        fights_df, RED, BLUE
    )

    assert matchup_date == date(2017, 4, 22)
    assert weight_class == "Featherweight"
    assert scheduled_rounds == 3
