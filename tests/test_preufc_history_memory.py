"""Phase 4, service memory: the pre-UFC history kept in RAM between refreshes.

B6 measured the steady RSS of the service going up +42 MiB on Windows with the
pre-UFC data loaded (the plan's guide was 30 MB), although only ~17 MiB were live.
Two levers cut it WITHOUT changing a single value, and these tests pin that:

* db.load_espn_history_dataframe reads plain tuples instead of RealDictCursor rows:
  the frame is the same (rows, order, dtypes and Python types, None apart from ''),
  but ~36k throwaway dicts per refresh are never built;
* CompactEspnIndex holds ONE frame sorted stably by fighter plus each fighter's
  offsets, instead of one DataFrame per fighter (~2,600 of them). Each lookup gives
  espn_features_for_fighter exactly the rows index_espn_history gives, in the same
  order, so the pre-UFC block is identical (the training CSV keeps using
  index_espn_history; serving uses the compact one).

Synthetic and in memory: no database.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
import pytest
from psycopg2.extras import RealDictCursor, RealDictRow

from src.prediction import api
from src.prediction.features.db import (
    ESPN_HISTORY_SQL,
    CompactEspnIndex,
    index_espn_history,
    load_espn_history_dataframe,
)
from src.prediction.features.preufc import (
    ESPN_HISTORY_COLUMNS,
    build_preufc_block,
    espn_features_for_fighter,
)

# Every quirk the frame must keep: a NULL date, a NULL method next to an EMPTY one,
# a NULL league, a NULL result, a NULL title flag, the DWCS league, accents, two
# rows of one fighter on the same date.
QUIRKY_ROWS = [
    (4, 7, date(2022, 3, 1), "win", "KO/TKO", True, "3359"),
    (31, 7, date(2023, 9, 12), "win", "U-DEC", False, "3321"),
    (9, 2, date(2019, 5, 1), None, "", False, "3359"),
    (10, 2, date(2019, 5, 1), "nc", 'Sumisión, "técnica"', None, "3301"),
    (12, 2, None, "loss", None, False, None),
]


class _CursorFactoryConnection:
    """psycopg2-like: plain cursors hand back tuples, RealDictCursor hands back
    RealDictRow. Records the factory each cursor was opened with."""

    def __init__(self, rows):
        self.rows = rows
        self.factories: list = []

    def cursor(self, cursor_factory=None):
        self.factories.append(cursor_factory)
        return _Cursor(self.rows, as_dicts=cursor_factory is RealDictCursor)


class _Cursor:
    def __init__(self, rows, as_dicts):
        self.rows = rows
        self.as_dicts = as_dicts
        self._result: list = []

    def execute(self, sql, params=None):
        assert sql == ESPN_HISTORY_SQL
        self._result = [
            RealDictRow(zip(ESPN_HISTORY_COLUMNS, row)) if self.as_dicts else tuple(row)
            for row in self.rows
        ]

    def fetchall(self):
        return list(self._result)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _frame_as_realdictcursor_built_it(rows) -> pd.DataFrame:
    """What load_espn_history_dataframe returned with a RealDictCursor."""
    return pd.DataFrame(
        [RealDictRow(zip(ESPN_HISTORY_COLUMNS, row)) for row in rows],
        columns=ESPN_HISTORY_COLUMNS,
    )


def _assert_same_frame(got: pd.DataFrame, want: pd.DataFrame) -> None:
    pd.testing.assert_frame_equal(got, want, check_exact=True)
    assert got.dtypes.to_dict() == want.dtypes.to_dict()
    for got_row, want_row in zip(got.to_dict("records"), want.to_dict("records")):
        for column in ESPN_HISTORY_COLUMNS:
            assert type(got_row[column]) is type(want_row[column]), column
            if pd.isna(want_row[column]):
                assert pd.isna(got_row[column]), column
            else:
                assert got_row[column] == want_row[column], column


# ------------------------------------------------------------------- tuple cursor


@pytest.mark.parametrize("rows", [QUIRKY_ROWS, []], ids=["quirky", "empty-table"])
def test_the_espn_frame_is_built_from_plain_tuples_and_is_the_same_frame(rows):
    connection = _CursorFactoryConnection(rows)

    frame = load_espn_history_dataframe(connection)

    assert connection.factories == [None]  # a plain cursor: tuples, no dict per row
    _assert_same_frame(frame, _frame_as_realdictcursor_built_it(rows))
    assert list(frame.columns) == ESPN_HISTORY_COLUMNS
    if rows:
        by_id = frame.set_index("id")
        assert by_id.loc[9, "method"] == ""
        assert pd.isna(by_id.loc[12, "method"])
        assert by_id.loc[12, "event_date"] is None


def test_the_tuple_frame_equals_the_dict_frame_on_a_large_random_table():
    rows = _random_rows(seed=5, n_rows=4_000, n_fighters=300)
    connection = _CursorFactoryConnection(rows)

    _assert_same_frame(
        load_espn_history_dataframe(connection), _frame_as_realdictcursor_built_it(rows)
    )


# ------------------------------------------------------------------ compact index


def _random_rows(seed: int, n_rows: int, n_fighters: int) -> list[tuple]:
    """Shuffled rows with plenty of same-date ties, some NULL dates and, on purpose,
    some repeated ids, so the stable order is the only tie-break left."""
    rng = random.Random(seed)
    fighters = rng.sample(range(1, 10 * n_fighters), n_fighters)
    days = [date(2015, 1, 1) + timedelta(days=rng.randint(0, 3_000)) for _ in range(40)]
    rows = []
    for index in range(n_rows):
        row_id = rng.randint(1, n_rows // 2)  # repeats on purpose
        rows.append(
            (
                row_id,
                rng.choice(fighters),
                None if rng.random() < 0.02 else rng.choice(days),  # many ties
                rng.choice(["win", "loss", "draw", "nc", None, "Win"]),
                rng.choice(["KO/TKO", "Submission", "Decision", "", None, "TKO"]),
                rng.choice([True, False, False, None]),
                rng.choice(["3321", "8", "3359", None]),
            )
        )
    rng.shuffle(rows)
    return rows


def _frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=ESPN_HISTORY_COLUMNS)


@pytest.mark.parametrize(
    "rows",
    [QUIRKY_ROWS, _random_rows(seed=1, n_rows=3_000, n_fighters=150)],
    ids=["quirky", "random-with-ties"],
)
def test_compact_index_gives_each_fighter_the_rows_of_index_espn_history(rows):
    history = _frame(rows)
    before = history.copy()

    compact = CompactEspnIndex(history)
    reference = index_espn_history(history)

    pd.testing.assert_frame_equal(history, before)  # the input is not modified
    assert len(compact) == len(reference)
    assert set(compact) == set(reference)
    for fighter_id, expected in reference.items():
        assert fighter_id in compact
        _assert_same_frame(compact[fighter_id], expected)
    missing = max(reference) + 1
    assert missing not in compact
    sentinel = object()
    assert compact.get(missing, sentinel) is sentinel
    with pytest.raises(KeyError):
        compact[missing]


def test_same_date_rows_keep_the_stable_order():
    history = _frame(
        [
            (30, 1, date(2020, 1, 1), "loss", "Decision", False, "8"),
            (10, 1, date(2020, 1, 1), "win", "KO/TKO", False, "8"),
            (20, 1, date(2019, 1, 1), "win", "Submission", False, "8"),
            (10, 1, date(2020, 1, 1), "draw", "Decision", False, "8"),  # same date AND id
            (5, 1, None, "win", "KO", False, "8"),
        ]
    )

    rows = CompactEspnIndex(history)[1]

    assert list(rows["id"]) == [20, 10, 10, 30, 5]
    assert list(rows["result"]) == ["win", "win", "draw", "loss", "win"]
    _assert_same_frame(rows, index_espn_history(history)[1])


def test_the_pre_ufc_block_is_identical_with_either_index():
    rows = _random_rows(seed=2, n_rows=2_500, n_fighters=120)
    history = _frame(rows)
    compact = CompactEspnIndex(history)
    reference = index_espn_history(history)
    fighters = sorted(reference)
    known = set(fighters[::2]) | {10**6}  # half known, plus one known with no rows
    cutoffs = sorted({row[2] for row in rows if row[2] is not None})[::7] + [
        date(2030, 1, 1)
    ]

    for fighter_id in fighters[:40] + [10**6]:
        for cutoff in cutoffs:
            assert espn_features_for_fighter(
                compact.get(fighter_id, reference.get(fighter_id, history.iloc[0:0])),
                fighter_id,
                cutoff,
            ) == espn_features_for_fighter(
                reference.get(fighter_id, history.iloc[0:0]), fighter_id, cutoff
            )
    for red, blue in zip(fighters, fighters[1:] + [10**6]):
        for cutoff in cutoffs[::3]:
            assert build_preufc_block(red, blue, cutoff, compact, known) == (
                build_preufc_block(red, blue, cutoff, reference, known)
            )


def test_an_empty_table_gives_an_empty_index():
    compact = CompactEspnIndex(_frame([]))

    assert len(compact) == 0
    assert list(compact) == []
    assert compact.get(1) is None
    block = build_preufc_block(1, 2, date(2026, 1, 1), compact, {1})
    assert block["espn_has_history_red"] == 0
    assert block["espn_has_history_blue"] is None


def test_the_service_history_keeps_the_compact_index():
    """PreUfcHistory is what the service keeps between refreshes: one frame plus
    offsets, never ~2,600 DataFrames."""
    history = api.PreUfcHistory.from_frames(_frame(QUIRKY_ROWS), [7, 2])

    assert isinstance(history.by_fighter, CompactEspnIndex)
    assert list(history.by_fighter[2]["id"]) == [9, 10, 12]
    assert history.rows == len(QUIRKY_ROWS)
