"""Phase 4 in the SERVICE: the served winner row carries ``ufc_prev_fights`` and the
pre-UFC block exactly as training builds them, cut at the anchor's date.

Training (the CSV v2) composes, for each bout and with ``cutoff`` = the bout's date:
``count_prior_ufc_fights`` per corner, ``build_preufc_block`` and
``preufc_diff_values``. The pre-registration of the phase-4 measurement
(docs/experiments/preufc-fase4-2026-10-05) needs the service to build THE SAME row
when it is anchored to that bout (``fightId``): that is the parity the shadow and
the deploy depend on. These tests pin it offline, on a synthetic card and a
synthetic fight_history_espn, with the REAL builders and the REAL api.predict (only
what would open a socket to Neon is stubbed):

* the row served with anchor "fight" equals the training composition, column by
  column, in the order of WINNER_FEATURE_COLUMNS, and the 20 + 2 columns the real
  training builder already writes;
* ESPN rows (and UFC bouts) dated the day of the bout or later never count;
* the cut is the anchor's date (fight, pending, today, none), as a ``date``;
* only the groups the bundle reads are added: with a legacy bundle the row (and
  featureValues) is exactly today's and fight_history_espn is never read;
* a bundle with the block is exactly corner-symmetric and its pairs come out as
  one factor each;
* in the service: fight_history_espn is cached with the same TTL as fights, a
  failed load degrades to "unknown" (200, block None, counted, /health says so),
  and /health answers from memory only.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from xgboost import XGBClassifier

import src.prediction.service as service
import src.scrapers.db as scrapers_db
from src.prediction import api
from src.prediction.features import (
    build_fighter_history_dataframe,
    build_training_dataset,
)
from src.prediction.features.db import (
    ESPN_HISTORY_SQL,
    ESPN_KNOWN_FIGHTERS_SQL,
    index_espn_history,
)
from src.prediction.features.fighter_history import count_prior_ufc_fights
from src.prediction.features.preufc import (
    ESPN_HISTORY_COLUMNS,
    build_preufc_block,
    preufc_diff_values,
)
from src.prediction.features.types import (
    CORNER_PAIR_BASES,
    FEATURE_COLUMNS,
    FEATURE_SETS,
    PREUFC_BASES,
    PREUFC_COLUMNS,
    PREUFC_DIFF_COLUMNS,
    UFC_COUNT_COLUMNS,
    WINNER_FEATURE_COLUMNS,
)
from src.prediction.preprocessing import NanPassthrough

STUB_URL = "postgresql://stub.invalid"

# ------------------------------------------------------------------ the synthetic world

A, B, X, Y, D, U, LONER, STRANGER = 1, 2, 3, 4, 5, 6, 7, 8

PHYSICAL = {
    A: {"birth_date": date(1994, 1, 1), "height_cm": 180.0, "reach_cm": 185.0},
    B: {"birth_date": date(1995, 6, 1), "height_cm": 178.0, "reach_cm": 183.0},
    X: {"birth_date": date(1990, 3, 3), "height_cm": 175.0, "reach_cm": 178.0},
    Y: {"birth_date": date(1992, 9, 9), "height_cm": 183.0, "reach_cm": 190.0},
    # D debuts at FIGHT_DAY (Contender Series rows in his ESPN record) and has no
    # reach on record: a NaN physical diff on both sides of the parity.
    D: {"birth_date": date(1999, 2, 2), "height_cm": 177.0, "reach_cm": None},
    # U has one UFC bout but no ESPN link: unknown history, his ESPN rows ignored.
    U: {"birth_date": date(1997, 7, 7), "height_cm": 181.0, "reach_cm": 186.0},
    # Neither has any UFC bout: the "none" anchor.
    LONER: {"birth_date": date(1998, 4, 4), "height_cm": 170.0, "reach_cm": 172.0},
    STRANGER: {"birth_date": date(1996, 5, 5), "height_cm": 172.0, "reach_cm": 175.0},
}

FIGHT_DAY = date(2026, 10, 3)
PENDING_DATE = date(2026, 12, 12)
TODAY = date(2026, 10, 10)
THE_FIGHT = 20  # A-B on FIGHT_DAY, decided: B won
SAME_CARD = 21  # X-Y on FIGHT_DAY too
DEBUT = 22  # D-U on FIGHT_DAY, D's UFC debut
AFTER = 23  # A-X two weeks AFTER the fight (and after TODAY)
PENDING = 30  # B-D booked ahead, no result


class _FrozenToday(date):
    """`date` with `today()` pinned to TODAY, patched into the api module."""

    @classmethod
    def today(cls) -> date:
        return TODAY


def _bout(
    fight_id: int,
    event_date: date,
    red: int,
    blue: int,
    winner: int | None,
    method: str | None = "KO/TKO",
    *,
    end_round: int | None = 2,
    end_time: str | None = "3:10",
    scheduled_rounds: int = 3,
    landed: int = 40,
) -> dict:
    """A bout with every column load_base_dataframe returns."""
    row = {
        "fight_id": fight_id,
        "event_date": event_date,
        "event_id": fight_id,
        "fighter_red_id": red,
        "fighter_blue_id": blue,
        "winner_id": winner,
        "method": method,
        "end_round": end_round,
        "end_time": end_time,
        "is_title_fight": False,
        "scheduled_rounds": scheduled_rounds,
        "weight_class": "Welterweight",
    }
    decided = winner is not None
    for corner, fighter in (("red", red), ("blue", blue)):
        physical = PHYSICAL[fighter]
        row[f"{corner}_birth_date"] = physical["birth_date"]
        row[f"{corner}_height_cm"] = physical["height_cm"]
        row[f"{corner}_reach_cm"] = physical["reach_cm"]
        stats = {
            "sig_strikes_landed": landed + (7 if corner == "red" else 0),
            "sig_strikes_attempted": 95,
            "takedowns_landed": 1 if corner == "red" else 0,
            "takedowns_attempted": 3 if corner == "red" else 2,
            "submission_attempts": 1,
            "control_time_seconds": 140 if corner == "red" else 60,
            "knockdowns": 1 if corner == "red" else 0,
        }
        for stat, value in stats.items():
            row[f"{corner}_{stat}"] = value if decided else None
    return row


def _card() -> pd.DataFrame:
    bouts = [
        _bout(10, date(2024, 1, 10), A, X, A),
        _bout(
            11, date(2024, 6, 15), B, Y, B, "U-DEC",
            end_round=3, end_time="5:00", landed=55,
        ),
        _bout(12, date(2025, 2, 1), A, Y, A, "SUB - Rear Naked Choke", landed=33),
        _bout(13, date(2025, 3, 1), X, B, B, landed=47),
        _bout(14, date(2025, 9, 20), U, Y, Y, "S-DEC", end_round=3, end_time="5:00"),
        _bout(THE_FIGHT, FIGHT_DAY, A, B, B, landed=61),
        _bout(SAME_CARD, FIGHT_DAY, X, Y, Y),
        _bout(DEBUT, FIGHT_DAY, D, U, D, "SUB"),
        _bout(AFTER, date(2026, 10, 24), A, X, A),
        _bout(
            PENDING, PENDING_DATE, B, D, None, None,
            end_round=None, end_time=None, scheduled_rounds=5,
        ),
    ]
    frame = pd.DataFrame(bouts).sort_values(["event_date", "fight_id"])
    return frame.reset_index(drop=True)


# (id, fighter_id, event_date, result, method, is_title_fight, league_id). Shuffled on
# purpose: the index has to sort them. 3321 is the Contender Series.
ESPN_ROWS = [
    (105, A, date(2026, 11, 1), "loss", "Decision", False, "1"),  # after everything
    (101, A, date(2019, 5, 1), "win", "KO/TKO", False, "1"),
    (202, B, date(2023, 9, 1), "win", "Decision - Split", False, "5"),
    (104, A, FIGHT_DAY, "win", "KO/TKO", False, "1"),  # same day as THE_FIGHT
    (102, A, date(2020, 2, 1), "loss", "Decision - Unanimous", False, "1"),
    (502, D, date(2025, 8, 19), "win", "Submission (Rear-Naked Choke)", False, "3321"),
    (103, A, date(2021, 8, 14), "win", "Submission", False, "3321"),
    (201, B, date(2018, 3, 3), "win", "TKO - Doctor Stoppage", True, "5"),
    (503, D, FIGHT_DAY, "loss", "Decision", False, "8"),  # same day as the debut
    (501, D, date(2022, 7, 1), "win", "KO/TKO", False, "8"),
    (601, U, date(2020, 1, 1), "win", "KO", False, "8"),  # U is unknown: never read
    (701, LONER, date(2025, 1, 1), "win", "KO/TKO", False, "8"),
]
KNOWN = frozenset({A, B, X, Y, D, LONER})  # U and STRANGER: unknown history

EMPTY_RANKINGS = pd.DataFrame(
    columns=["fighter_id", "division", "rank_position", "snapshot_date"]
)


def _espn() -> pd.DataFrame:
    return pd.DataFrame(ESPN_ROWS, columns=ESPN_HISTORY_COLUMNS)


def _preufc_history() -> api.PreUfcHistory:
    return api.PreUfcHistory.from_frames(_espn(), KNOWN)


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _same(served, expected) -> bool:
    """None and NaN are the same "unknown"; anything else must be equal exactly."""
    if _missing(served) or _missing(expected):
        return _missing(served) and _missing(expected)
    return float(served) == float(expected)


_SYNTHETIC_HISTORY = object()  # default: the synthetic fight_history_espn


def _served(
    red: int,
    blue: int,
    fight_id: int | None = None,
    feature_columns=tuple(WINNER_FEATURE_COLUMNS),
    preufc_history=_SYNTHETIC_HISTORY,
):
    card = _card()
    if preufc_history is _SYNTHETIC_HISTORY:
        preufc_history = _preufc_history()
    return api._build_feature_row(
        card,
        EMPTY_RANKINGS,
        red,
        blue,
        PHYSICAL,
        history_df=build_fighter_history_dataframe(card),
        fight_id=fight_id,
        feature_columns=list(feature_columns),
        preufc_history=preufc_history,
    )


def _training_composition(red: int, blue: int, cutoff: date) -> dict:
    """What training writes for the bout red-blue on `cutoff` beyond the 20 diffs."""
    history_df = build_fighter_history_dataframe(_card())
    counts = (
        count_prior_ufc_fights(history_df, red, cutoff),
        count_prior_ufc_fights(history_df, blue, cutoff),
    )
    block = build_preufc_block(red, blue, cutoff, index_espn_history(_espn()), KNOWN)
    return {
        **dict(zip(UFC_COUNT_COLUMNS, counts)),
        **block,
        **preufc_diff_values(block),
    }


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(api, "date", _FrozenToday)


# ------------------------------------------------------------- the row, training parity


@pytest.mark.parametrize(
    ("fight_id", "red", "blue"),
    [(THE_FIGHT, A, B), (DEBUT, D, U)],
    ids=["veterans", "debutant-vs-unknown"],
)
def test_served_row_with_fight_anchor_is_the_training_composition(fight_id, red, blue):
    row, _method_row, context, _low = _served(red, blue, fight_id=fight_id)

    assert context["anchor"] == "fight"
    assert context["matchupDate"] == FIGHT_DAY.isoformat()
    # Same columns, same order as the CSV v2.
    assert list(row) == WINNER_FEATURE_COLUMNS
    expected = _training_composition(red, blue, FIGHT_DAY)
    assert list(expected) == UFC_COUNT_COLUMNS + PREUFC_COLUMNS + PREUFC_DIFF_COLUMNS
    for column, value in expected.items():
        assert _same(row[column], value), (column, row[column], value)
    # And the whole row of the REAL training builder (the CSV v2), same bout, same
    # ESPN history and known ids: all 49 columns.
    dataset = build_training_dataset(
        _card(),
        EMPTY_RANKINGS,
        espn_by_fighter=index_espn_history(_espn()),
        known_fighter_ids=KNOWN,
    ).dataset
    training_row = dataset[dataset["fight_id"] == fight_id].iloc[0]
    for column in WINNER_FEATURE_COLUMNS:
        assert _same(row[column], training_row[column]), (
            column,
            row[column],
            training_row[column],
        )


def test_rows_on_or_after_the_fight_date_never_count():
    row, *_ = _served(A, B, fight_id=THE_FIGHT)

    # UFC: A had 10 and 12 before; THE_FIGHT itself and AFTER do not count.
    assert row["ufc_prev_fights_red"] == 2
    assert row["ufc_prev_fights_blue"] == 2
    # ESPN: 101-103; the same-day 104 and the later 105 do not count.
    assert row["espn_has_history_red"] == 1
    assert row["espn_prev_fights_red"] == 3
    assert row["espn_win_rate_red"] == pytest.approx(2 / 3)
    assert row["espn_streak_red"] == 1
    assert row["espn_days_since_last_red"] == (FIGHT_DAY - date(2021, 8, 14)).days
    assert row["espn_prev_fights_blue"] == 2
    assert row["espn_title_fights_blue"] == 1

    row, *_ = _served(D, U, fight_id=DEBUT)

    assert row["ufc_prev_fights_red"] == 0
    assert row["ufc_prev_fights_blue"] == 1
    # The Contender Series row counts; the loss on the debut's own day does not.
    assert row["espn_prev_fights_red"] == 2
    assert row["espn_win_rate_red"] == 1.0
    assert row["espn_sub_rate_red"] == 0.5
    # U has ESPN rows but no ESPN link: "don't know", has_history included.
    assert all(row[f"{base}_blue"] is None for base in PREUFC_BASES)
    assert all(row[column] is None for column in PREUFC_DIFF_COLUMNS)


@pytest.mark.parametrize(
    ("kind", "red", "blue", "fight_id", "cutoff"),
    [
        ("fight", A, B, THE_FIGHT, FIGHT_DAY),
        ("pending", B, D, None, PENDING_DATE),
        ("today", A, Y, None, TODAY),
        ("none", LONER, STRANGER, None, TODAY),
    ],
)
def test_cut_is_the_anchor_date_as_a_date(
    monkeypatch, kind, red, blue, fight_id, cutoff
):
    """Every phase-4 value is cut at the anchor's matchup date, as a plain
    datetime.date like the training rows (load_base_dataframe -> .dt.date)."""
    seen: list = []
    real_count, real_block = api.count_prior_ufc_fights, api.build_preufc_block

    def count_spy(history_df, fighter_id, when):
        seen.append(when)
        return real_count(history_df, fighter_id, when)

    def block_spy(red_id, blue_id, when, *args, **kwargs):
        seen.append(when)
        return real_block(red_id, blue_id, when, *args, **kwargs)

    monkeypatch.setattr(api, "count_prior_ufc_fights", count_spy)
    monkeypatch.setattr(api, "build_preufc_block", block_spy)

    _row, _method_row, context, _low = _served(red, blue, fight_id=fight_id)

    assert context["anchor"] == kind
    assert context["matchupDate"] == cutoff.isoformat()
    assert len(seen) == 3  # two counts and one block
    assert all(type(when) is date and when == cutoff for when in seen), seen


def test_today_anchor_counts_everything_before_today_only():
    row, *_ = _served(A, Y)

    # A: 10, 12 and THE_FIGHT; AFTER (24-oct) is after TODAY. Y: 11, 12, 14, SAME_CARD.
    assert row["ufc_prev_fights_red"] == 3
    assert row["ufc_prev_fights_blue"] == 4
    # 101-104 (the FIGHT_DAY row is history by now); 105 is after TODAY.
    assert row["espn_prev_fights_red"] == 4
    assert row["espn_days_since_last_red"] == (TODAY - FIGHT_DAY).days
    # Y is known and has no rows: 0, 0, 0 and None for the rates.
    assert row["espn_has_history_blue"] == 0
    assert row["espn_prev_fights_blue"] == 0
    assert row["espn_win_rate_blue"] is None


def test_none_anchor_still_reads_the_known_record():
    row, _method, context, low = _served(LONER, STRANGER)

    assert context["anchor"] == "none"
    assert low is True
    assert row["ufc_prev_fights_red"] == row["ufc_prev_fights_blue"] == 0
    assert row["espn_has_history_red"] == 1
    assert row["espn_days_since_last_red"] == (TODAY - date(2025, 1, 1)).days
    assert all(row[f"{base}_blue"] is None for base in PREUFC_BASES)


# ------------------------------------------------------- only what the bundle reads


def _todays_call(red: int, blue: int, fight_id: int | None):
    """_build_feature_row exactly as it was called before phase 4."""
    card = _card()
    return api._build_feature_row(
        card,
        EMPTY_RANKINGS,
        red,
        blue,
        PHYSICAL,
        history_df=build_fighter_history_dataframe(card),
        fight_id=fight_id,
    )


@pytest.mark.parametrize(
    ("red", "blue", "fight_id"),
    [(A, B, THE_FIGHT), (D, U, DEBUT), (A, Y, None), (LONER, STRANGER, None)],
    ids=["fight", "debut", "today", "none"],
)
def test_legacy_bundle_row_is_todays_row_and_never_reads_espn(
    monkeypatch, red, blue, fight_id
):
    def boom(*_args, **_kwargs):  # pragma: no cover - must be unreachable
        raise AssertionError("a legacy bundle must not compute any phase-4 value")

    today = _todays_call(red, blue, fight_id)
    monkeypatch.setattr(api, "count_prior_ufc_fights", boom)
    monkeypatch.setattr(api, "build_preufc_block", boom)

    served = _served(
        red,
        blue,
        fight_id=fight_id,
        feature_columns=FEATURE_COLUMNS,
        preufc_history=None,
    )

    assert list(served[0]) == FEATURE_COLUMNS
    assert served == today


def test_each_bundle_gets_exactly_its_groups():
    for name in ("base", "preufc", "preufc_diff"):
        row, *_ = _served(A, B, fight_id=THE_FIGHT, feature_columns=FEATURE_SETS[name])
        assert list(row) == FEATURE_SETS[name], name
    # The base set does not read fight_history_espn at all.
    row, *_ = api._build_feature_row(
        _card(),
        EMPTY_RANKINGS,
        A,
        B,
        PHYSICAL,
        history_df=build_fighter_history_dataframe(_card()),
        fight_id=THE_FIGHT,
        feature_columns=FEATURE_SETS["base"],
        preufc_history=None,
    )
    assert list(row) == FEATURE_SETS["base"]


def test_the_method_row_never_carries_the_phase4_columns():
    _row, legacy_method, *_ = _todays_call(A, B, THE_FIGHT)
    _row, method_row, *_ = _served(A, B, fight_id=THE_FIGHT)

    assert method_row == legacy_method


def test_block_without_a_history_to_read_is_an_error():
    """api.predict and the service always hand one over; a silent all-None block
    would be a degraded prediction nobody knows about."""
    card = _card()
    with pytest.raises(ValueError, match="pre-UFC"):
        api._build_feature_row(
            card,
            EMPTY_RANKINGS,
            A,
            B,
            PHYSICAL,
            history_df=build_fighter_history_dataframe(card),
            fight_id=THE_FIGHT,
            feature_columns=FEATURE_SETS["preufc"],
            preufc_history=None,
        )


def test_unavailable_history_serves_the_whole_block_as_unknown():
    row, *_ = _served(
        A, B, fight_id=THE_FIGHT, preufc_history=api.UNAVAILABLE_PREUFC_HISTORY
    )

    assert all(row[column] is None for column in PREUFC_COLUMNS + PREUFC_DIFF_COLUMNS)
    # The UFC counts do not come from ESPN: still there.
    assert (row["ufc_prev_fights_red"], row["ufc_prev_fights_blue"]) == (2, 2)


def test_needs_preufc_history_only_for_the_block():
    assert not api.needs_preufc_history(FEATURE_SETS["legacy"])
    assert not api.needs_preufc_history(FEATURE_SETS["base"])
    assert api.needs_preufc_history(FEATURE_SETS["preufc"])
    assert api.needs_preufc_history(FEATURE_SETS["preufc_diff"])
    assert api.needs_preufc_history(["age_diff", "espn_win_rate_diff"])


def test_preufc_history_from_frames_indexes_once_and_counts():
    history = api.PreUfcHistory.from_frames(_espn(), [A, B, D])

    assert history.rows == len(ESPN_ROWS)
    assert history.known_fighter_ids == frozenset({A, B, D})
    assert list(history.by_fighter[A]["id"]) == [101, 102, 103, 104, 105]
    assert api.UNAVAILABLE_PREUFC_HISTORY.known_fighter_ids == frozenset()
    assert api.UNAVAILABLE_PREUFC_HISTORY.rows == 0


# ------------------------------------------- load_preufc_history: one consistent read

SWEPT = 9  # swept for the first time by the Tuesday cron WHILE the service loads


class _SweepRaceDatabase:
    """fight_history_espn and fighters as Postgres shows them. The Tuesday cron
    commits SWEPT's first sweep (his rows plus espn_history_checked_at, in one
    commit) right after the load's FIRST SELECT, whichever that is."""

    def __init__(self):
        self.rows = list(ESPN_ROWS)
        self.known = set(KNOWN)
        self.selects = 0

    def committed(self):
        return list(self.rows), set(self.known)

    def after_select(self):
        self.selects += 1
        if self.selects == 1:
            self.rows.append(
                (901, SWEPT, date(2024, 2, 2), "win", "KO/TKO", False, "8")
            )
            self.known.add(SWEPT)


class _RaceConnection:
    """A psycopg2 connection over _SweepRaceDatabase. READ COMMITTED by default:
    each SELECT sees the latest commit. ``SET TRANSACTION ... REPEATABLE READ``
    (honoured only inside a transaction, as with autocommit off) must come before
    any query, and then every SELECT sees the snapshot of the first one."""

    def __init__(self, database: _SweepRaceDatabase, honours_set_transaction: bool):
        self.database = database
        self.honours_set_transaction = honours_set_transaction
        self.statements: list[str] = []
        self.queries = 0
        self.isolation = "read committed"
        self.read_only = False
        self.snapshot = None

    def cursor(self, cursor_factory=None):
        return _RaceCursor(self, as_dicts=cursor_factory is not None)

    def rollback(self):
        pass

    def close(self):
        pass


class _RaceCursor:
    def __init__(self, connection: _RaceConnection, as_dicts: bool):
        self.connection = connection
        self.as_dicts = as_dicts
        self._rows: list = []

    def execute(self, sql, params=None):
        connection = self.connection
        normalized = " ".join(sql.split())
        connection.statements.append(normalized)
        if normalized.upper().startswith("SET TRANSACTION"):
            if connection.queries:
                raise AssertionError(
                    "SET TRANSACTION ISOLATION LEVEL must be called before any query"
                )
            if connection.honours_set_transaction:
                upper = normalized.upper()
                if "REPEATABLE READ" in upper:
                    connection.isolation = "repeatable read"
                connection.read_only = "READ ONLY" in upper
            self._rows = []
            return
        connection.queries += 1
        if connection.isolation == "repeatable read":
            if connection.snapshot is None:
                connection.snapshot = connection.database.committed()
            rows, known = connection.snapshot
        else:
            rows, known = connection.database.committed()
        if sql == ESPN_KNOWN_FIGHTERS_SQL:
            result = [{"id": f} if self.as_dicts else (f,) for f in sorted(known)]
        elif sql == ESPN_HISTORY_SQL:
            ordered = sorted(rows, key=lambda row: (row[1], row[2], row[0]))
            result = [
                dict(zip(ESPN_HISTORY_COLUMNS, row)) if self.as_dicts else row
                for row in ordered
            ]
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")
        connection.database.after_select()
        self._rows = result

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _connect_to(monkeypatch, connection: _RaceConnection) -> None:
    """load_preufc_history imports connect from src.scrapers.db when called."""

    @contextmanager
    def fake_connect(database_url):
        assert database_url == STUB_URL
        yield connection

    monkeypatch.setattr(scrapers_db, "connect", fake_connect)


@pytest.mark.parametrize(
    "honours_set_transaction", [True, False], ids=["repeatable-read", "autocommit"]
)
def test_a_sweep_committed_mid_load_never_makes_a_known_fighter_without_rows(
    monkeypatch, honours_set_transaction
):
    """The Tuesday cron commits a fighter's rows and his sweep stamp together. A
    load that read the rows BEFORE that commit and the known ids AFTER it would
    serve him as known with no prior fight (has_history 0, the 35.8 % penalty the
    unknown-history rule exists to avoid) for a whole TTL. Read in one snapshot,
    or at least the known ids first, the same race gives "don't know" (None)."""
    database = _SweepRaceDatabase()
    _connect_to(monkeypatch, _RaceConnection(database, honours_set_transaction))

    history = api.load_preufc_history(STUB_URL)

    assert database.selects == 2  # the sweep did land between the two reads
    assert SWEPT not in history.known_fighter_ids or SWEPT in history.by_fighter
    block = build_preufc_block(
        SWEPT, A, FIGHT_DAY, history.by_fighter, history.known_fighter_ids
    )
    assert block["espn_has_history_red"] is None
    assert block["espn_prev_fights_red"] is None


def test_load_preufc_history_reads_the_known_ids_first_in_one_read_only_snapshot(
    monkeypatch,
):
    connection = _RaceConnection(_SweepRaceDatabase(), honours_set_transaction=True)
    _connect_to(monkeypatch, connection)

    history = api.load_preufc_history(STUB_URL)

    assert connection.statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        " ".join(ESPN_KNOWN_FIGHTERS_SQL.split()),
        " ".join(ESPN_HISTORY_SQL.split()),
    ]
    assert connection.isolation == "repeatable read"
    assert connection.read_only is True
    # One snapshot: what was committed when the first SELECT ran.
    assert history.known_fighter_ids == KNOWN
    assert history.rows == len(ESPN_ROWS)


# ------------------------------------------------------------ api.predict end to end


def _synthetic_bundle(feature_set: str, nan_policy: str, calibrated: bool) -> dict:
    """A tiny winner bundle on `feature_set`. The label leans on the pre-UFC win
    rates and the UFC counts, so the block really reaches the probability."""
    columns = list(FEATURE_SETS[feature_set])
    rng = np.random.default_rng(20261005)
    n_rows = 1500
    frame = pd.DataFrame({column: rng.normal(size=n_rows) for column in columns})
    for column in columns:
        if not column.startswith("ufc_prev_fights"):
            frame.loc[rng.random(n_rows) < 0.15, column] = np.nan
    signal = np.zeros(n_rows)
    for column in columns:
        weight = 0.0
        if "espn_win_rate" in column:
            weight = 3.0
        elif "ufc_prev" in column:
            weight = 0.8
        if column.endswith("_blue"):
            weight = -weight
        signal += weight * np.nan_to_num(frame[column].to_numpy())
    labels = (signal + rng.normal(scale=0.5, size=n_rows) > 0).astype(int)
    imputer = (
        NanPassthrough() if nan_policy == "native" else SimpleImputer(strategy="median")
    )
    matrix = imputer.fit(frame).transform(frame)
    model = XGBClassifier(n_estimators=40, max_depth=3, random_state=0)
    model.fit(matrix, labels)
    bundle = {
        "feature_columns": columns,
        "imputer": imputer,
        "model": model,
        "trained_at": "2026-10-05",
    }
    if feature_set != "legacy":
        bundle["feature_set"] = feature_set
        bundle["nan_policy"] = nan_policy
    if calibrated:
        calibrator = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
        bundle["calibrator"] = calibrator.fit(matrix, labels)
    return bundle


BUNDLE_KINDS = {
    "legacy": ("legacy", "median", True),
    "preufc-native": ("preufc", "native", False),
    "preufc-median-calibrated": ("preufc", "median", True),
    "preufc_diff-native-calibrated": ("preufc_diff", "native", True),
}


@pytest.fixture(scope="module")
def bundles() -> dict[str, dict]:
    return {name: _synthetic_bundle(*spec) for name, spec in BUNDLE_KINDS.items()}


def _profiles(_database_url, fighter_ids):
    return {
        fighter_id: api.FighterPredictionProfile(
            id=fighter_id,
            name=f"Fighter {fighter_id}",
            nickname=None,
            headshot_url=None,
            wins=5,
            losses=1,
            draws=0,
            height_cm=None,
            reach_cm=None,
            stance=None,
            latest_weight_class=None,
            aggregate_stats={},
        )
        for fighter_id in fighter_ids
    }


@pytest.fixture
def offline_api(monkeypatch):
    """api.predict without Neon: settings, physicals and profiles stubbed."""
    monkeypatch.setattr(
        api, "get_settings", lambda: SimpleNamespace(database_url=STUB_URL)
    )
    monkeypatch.setattr(
        api,
        "_load_fighter_physical",
        lambda _url, ids: {fighter_id: PHYSICAL[fighter_id] for fighter_id in ids},
    )
    monkeypatch.setattr(api, "_load_fighter_profiles", _profiles)


def _predict(bundle: dict, red: int, blue: int, fight_id=None, **kwargs) -> dict:
    card = _card()
    return api.predict(
        red,
        blue,
        bundle=bundle,
        fights_df=card,
        rankings_df=EMPTY_RANKINGS,
        history_df=build_fighter_history_dataframe(card),
        fight_id=fight_id,
        **kwargs,
    )


def test_predict_with_a_legacy_bundle_never_loads_espn(
    offline_api, bundles, monkeypatch
):
    def boom(*_args, **_kwargs):  # pragma: no cover - must be unreachable
        raise AssertionError("a legacy bundle must not read fight_history_espn")

    monkeypatch.setattr(api, "load_preufc_history", boom)

    result = _predict(bundles["legacy"], A, B, fight_id=THE_FIGHT)

    assert list(result["featureValues"]) == FEATURE_COLUMNS
    today, *_ = _todays_call(A, B, THE_FIGHT)
    assert result["featureValues"] == today


def test_predict_loads_the_history_itself_when_not_given(
    offline_api, bundles, monkeypatch
):
    """The CLI and the shadow (api.predict(..., bundle=B42)) get no history from a
    service: api.predict reads it, once, like it reads the fights."""
    urls: list[str] = []

    def load(database_url):
        urls.append(database_url)
        return _preufc_history()

    monkeypatch.setattr(api, "load_preufc_history", load)

    result = _predict(bundles["preufc-native"], A, B, fight_id=THE_FIGHT)

    assert urls == [STUB_URL]
    expected = _training_composition(A, B, FIGHT_DAY)
    for column in UFC_COUNT_COLUMNS + PREUFC_COLUMNS:
        assert _same(result["featureValues"][column], expected[column]), column


SYMMETRY_MATCHUPS = [(A, B, THE_FIGHT), (D, U, DEBUT), (A, Y, None), (B, D, None)]


@pytest.mark.parametrize(
    "kind",
    ["preufc-native", "preufc-median-calibrated", "preufc_diff-native-calibrated"],
)
@pytest.mark.parametrize(
    ("red", "blue", "fight_id"),
    SYMMETRY_MATCHUPS,
    ids=["fight", "debut", "today", "pending"],
)
def test_block_bundle_is_exactly_corner_symmetric(
    offline_api, bundles, kind, red, blue, fight_id
):
    """Rows BUILT for (A, B) and for (B, A) with the real builders, not swap(row)."""
    bundle = bundles[kind]
    history = _preufc_history()

    forward = _predict(bundle, red, blue, fight_id, preufc_history=history)
    backward = _predict(bundle, blue, red, fight_id, preufc_history=history)

    assert abs(forward["redProbability"] - (1.0 - backward["redProbability"])) < 1e-9
    assert set(forward["featureContributions"]) == set(backward["featureContributions"])
    for name, contribution in forward["featureContributions"].items():
        assert backward["featureContributions"][name] == pytest.approx(
            -contribution, abs=1e-9
        ), name


def test_the_merged_pre_ufc_factor_comes_out(offline_api, bundles):
    result = _predict(
        bundles["preufc-native"], A, B, THE_FIGHT, preufc_history=_preufc_history()
    )

    contributions = result["featureContributions"]
    assert set(contributions) == set(FEATURE_COLUMNS) | set(CORNER_PAIR_BASES)
    names = [factor["name"] for factor in result["topFeatures"]] + list(contributions)
    assert not any(name.endswith(("_red", "_blue")) for name in names)
    # The label leans on it: it leads, as one factor, with the raw red-minus-blue
    # difference as its value (A 2/3, B 2/2 before FIGHT_DAY).
    top = result["topFeatures"][0]
    assert top["name"] == "espn_win_rate"
    assert top["value"] == pytest.approx(2 / 3 - 1.0)
    assert top["direction"] == "blue"


# --------------------------------------------------------------------- the service


def _fresh_cache() -> dict:
    return {
        "bundle": None,
        "fights_df": None,
        "rankings_df": None,
        "history_df": None,
        "preufc_history": None,
        "loaded_at": 0.0,
    }


@pytest.fixture
def svc(monkeypatch, offline_api):
    """The REAL app, the REAL _get_dataframes and the REAL api.predict over the
    synthetic card; only the loaders that would open a socket are stubbed."""
    monkeypatch.setattr(service, "_cache", _fresh_cache())
    monkeypatch.setattr(service, "_preufc_state", dict(service._PREUFC_STATE_AT_START))
    monkeypatch.setattr(
        service, "get_settings", lambda: SimpleNamespace(database_url=STUB_URL)
    )
    monkeypatch.setattr(service, "load_base_dataframe", lambda _url: _card())
    monkeypatch.setattr(service, "load_rankings_dataframe", lambda _url: EMPTY_RANKINGS)
    monkeypatch.setattr(service, "_existing_fighter_ids", lambda ids: set(ids))
    for name in service.API_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PREDICTION_ENV", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)

    state = SimpleNamespace(loads=[], fail_with=None)

    def load(database_url):
        state.loads.append(database_url)
        if state.fail_with is not None:
            raise state.fail_with
        return _preufc_history()

    monkeypatch.setattr(service, "load_preufc_history", load)
    state.use = lambda bundle: monkeypatch.setattr(
        service, "_get_bundle", lambda: bundle
    )
    state.client = TestClient(service.app, raise_server_exceptions=False)
    return state


def _health(svc) -> dict:
    response = svc.client.get("/health")
    assert response.status_code == 200, response.text
    return response.json()


THE_FIGHT_REQUEST = {"red": A, "blue": B, "fightId": THE_FIGHT}


def test_service_with_a_legacy_bundle_reads_no_espn(svc, bundles):
    svc.use(bundles["legacy"])

    response = svc.client.post("/predict", json=THE_FIGHT_REQUEST)

    assert response.status_code == 200, response.text
    assert svc.loads == []
    assert service._cache["preufc_history"] is None
    today, *_ = _todays_call(A, B, THE_FIGHT)
    assert response.json()["featureValues"] == today
    assert _health(svc)["preUfc"] == {
        "needed": False,
        "state": "not_needed",
        "rows": None,
        "knownFighters": None,
        "loadedAt": None,
        "degradedPredictions": 0,
    }


def test_service_loads_espn_with_the_fights_once_per_ttl(svc, bundles):
    svc.use(bundles["preufc-native"])

    fights_df, _rankings, history_df = service._get_dataframes()
    service._get_dataframes()
    service._get_dataframes()

    assert svc.loads == [STUB_URL]
    # Same population as training: build_fighter_history_dataframe over
    # load_base_dataframe, the frame build_training_dataset starts from.
    pd.testing.assert_frame_equal(history_df, build_fighter_history_dataframe(_card()))
    assert service._cache["preufc_history"].rows == len(ESPN_ROWS)

    service._cache["loaded_at"] -= service.DATA_TTL_SECONDS + 1
    service._get_dataframes()

    assert svc.loads == [STUB_URL, STUB_URL]


def test_service_serves_the_training_composition(svc, bundles):
    svc.use(bundles["preufc-native"])

    response = svc.client.post("/predict", json=THE_FIGHT_REQUEST)

    assert response.status_code == 200, response.text
    values = response.json()["featureValues"]
    assert list(values) == FEATURE_SETS["preufc"]
    expected = _training_composition(A, B, FIGHT_DAY)
    for column in UFC_COUNT_COLUMNS + PREUFC_COLUMNS:
        assert _same(values[column], expected[column]), column
    pre_ufc = _health(svc)["preUfc"]
    assert pre_ufc["needed"] is True
    assert pre_ufc["state"] == "ok"
    assert pre_ufc["rows"] == len(ESPN_ROWS)
    assert pre_ufc["knownFighters"] == len(KNOWN)
    assert isinstance(pre_ufc["loadedAt"], str) and pre_ufc["loadedAt"].startswith("20")
    assert pre_ufc["degradedPredictions"] == 0


def test_espn_failure_degrades_to_unknown_and_is_counted(svc, bundles, caplog):
    svc.use(bundles["preufc-native"])
    svc.fail_with = RuntimeError("permission denied for table fight_history_espn")

    with caplog.at_level(logging.ERROR, logger="prediction.service"):
        response = svc.client.post("/predict", json=THE_FIGHT_REQUEST)

    assert response.status_code == 200, response.text
    body = response.json()
    assert math.isfinite(body["redProbability"])
    values = body["featureValues"]
    assert all(values[column] is None for column in PREUFC_COLUMNS)
    assert (values["ufc_prev_fights_red"], values["ufc_prev_fights_blue"]) == (2, 2)
    assert any(
        record.levelno == logging.ERROR and "fight_history_espn" in record.getMessage()
        for record in caplog.records
    )
    pre_ufc = _health(svc)["preUfc"]
    assert pre_ufc["state"] == "unavailable"
    assert pre_ufc["rows"] is None
    assert pre_ufc["knownFighters"] is None
    assert pre_ufc["degradedPredictions"] == 1

    # Same cache, same TTL: no retry per request, and every one is counted.
    again = svc.client.post("/predict", json={"red": B, "blue": A})
    assert again.status_code == 200, again.text
    assert svc.loads == [STUB_URL]
    assert _health(svc)["preUfc"]["degradedPredictions"] == 2


def test_service_never_lets_api_predict_read_espn_per_request(
    svc, bundles, monkeypatch
):
    """Whatever the cache holds, /predict hands api.predict a history (never None):
    otherwise api.predict would read fight_history_espn from Neon on EVERY request.
    Here the frames come from a stub, so no refresh ever filled the cache."""
    svc.use(bundles["preufc-native"])
    card = _card()
    monkeypatch.setattr(
        service,
        "_get_dataframes",
        lambda: (card, EMPTY_RANKINGS, build_fighter_history_dataframe(card)),
    )

    def boom(*_args, **_kwargs):  # pragma: no cover - must be unreachable
        raise AssertionError("api.predict must not read fight_history_espn here")

    monkeypatch.setattr(api, "load_preufc_history", boom)

    response = svc.client.post("/predict", json=THE_FIGHT_REQUEST)

    assert response.status_code == 200, response.text
    values = response.json()["featureValues"]
    assert all(values[column] is None for column in PREUFC_COLUMNS)


def test_health_before_the_first_prediction_says_not_loaded(svc, bundles):
    svc.use(bundles["preufc-native"])

    pre_ufc = _health(svc)["preUfc"]

    assert pre_ufc == {
        "needed": True,
        "state": "not_loaded",
        "rows": None,
        "knownFighters": None,
        "loadedAt": None,
        "degradedPredictions": 0,
    }
    assert svc.loads == []


def test_health_answers_from_memory_only(svc, bundles, monkeypatch):
    """The keep-alive polls /health every 10 minutes: anything that reaches Neon
    there repeats the 18-ago quota outage. Every loader raises here."""
    svc.use(bundles["preufc-native"])
    service._preufc_state.update(
        state="ok",
        rows=35772,
        known_fighters=2650,
        loaded_at="2026-10-05T10:00:00+00:00",
        degraded_predictions=3,
    )

    def boom(*_args, **_kwargs):  # pragma: no cover - must be unreachable
        raise AssertionError("/health must not reach the database or the loaders")

    for name in (
        "load_base_dataframe",
        "load_rankings_dataframe",
        "load_preufc_history",
        "build_fighter_history_dataframe",
        "_get_dataframes",
        "_db_ping",
        "connect",
        "get_settings",
    ):
        monkeypatch.setattr(service, name, boom)
    for name in (
        "load_preufc_history",
        "load_espn_history_dataframe",
        "load_espn_known_fighter_ids",
    ):
        monkeypatch.setattr(api, name, boom)

    body = _health(svc)

    assert body["status"] == "ok"
    assert body["db"] == "skipped"
    assert body["preUfc"] == {
        "needed": True,
        "state": "ok",
        "rows": 35772,
        "knownFighters": 2650,
        "loadedAt": "2026-10-05T10:00:00+00:00",
        "degradedPredictions": 3,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3742f89aa0c1b2d3e4f5", "3742f89"), ("abc", "abc"), ("", None), (None, None)],
)
def test_health_commit_is_render_git_commit_cut_to_seven(
    svc, bundles, monkeypatch, value, expected
):
    svc.use(bundles["legacy"])
    if value is None:
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("RENDER_GIT_COMMIT", value)

    assert _health(svc)["commit"] == expected


def test_health_describes_the_model_it_serves(svc, bundles):
    svc.use(bundles["preufc-native"])
    assert _health(svc)["model"] == {
        "trainedAt": "2026-10-05",
        "featureSet": "preufc",
        "nanPolicy": "native",
        "featureCount": 40,
    }

    # A bundle from before phase 4 records none of the keys: legacy / median.
    svc.use({"trained_at": "2026-06-27", "feature_columns": list(FEATURE_COLUMNS)})
    assert _health(svc)["model"] == {
        "trainedAt": "2026-06-27",
        "featureSet": "legacy",
        "nanPolicy": "median",
        "featureCount": 20,
    }
