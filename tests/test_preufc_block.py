"""Phase 4 pre-UFC block: the nine fight_history_espn variables per corner.

The function is a verbatim port of the 30-sep experiment (DWCS included), so these
tests pin the experiment's semantics, quirks included, plus the one deliberate change:
a fighter whose ESPN history was never swept is unknown (nine None), not "no history".
Pure: the loaders run against the in-memory fake connection, never a database.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

from src.prediction.features.db import (
    ESPN_HISTORY_SQL,
    ESPN_KNOWN_FIGHTERS_SQL,
    index_espn_history,
    load_espn_history_dataframe,
    load_espn_known_fighter_ids,
)
from src.prediction.features.preufc import (
    ESPN_HISTORY_COLUMNS,
    build_preufc_block,
    espn_features_for_fighter,
    preufc_diff_values,
)
from src.prediction.features.types import (
    PREUFC_BASES,
    PREUFC_COLUMNS,
    PREUFC_DIFF_COLUMNS,
)

FIGHT_DAY = date(2024, 6, 1)
NO_HISTORY = {
    "espn_has_history": 0,
    "espn_prev_fights": 0,
    "espn_win_rate": None,
    "espn_ko_rate": None,
    "espn_sub_rate": None,
    "espn_streak": None,
    "espn_days_since_last": None,
    "espn_years_pro": None,
    "espn_title_fights": 0,
}


def _row(row_id, fighter_id, day, result="win", method="KO/TKO", title=False,
         league="3359"):
    return {
        "id": row_id,
        "fighter_id": fighter_id,
        "event_date": day,
        "result": result,
        "method": method,
        "is_title_fight": title,
        "league_id": league,
    }


def _history(rows):
    return pd.DataFrame(rows, columns=ESPN_HISTORY_COLUMNS)


def _same(a, b) -> bool:
    if a is None or b is None:
        return a is b
    both_float = isinstance(a, float) and isinstance(b, float)
    if both_float and math.isnan(a) and math.isnan(b):
        return True
    return a == b


# --- espn_features_for_fighter: the experiment's semantics ---------------------------

def test_dwcs_rows_count():
    # League 3321 is the Contender Series: it is pre-UFC history and goes in.
    history = _history([
        _row(1, 7, date(2022, 3, 1), "win", "KO/TKO", league="3359"),
        _row(2, 7, date(2023, 9, 12), "win", "U-DEC", league="3321"),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_prev_fights"] == 2
    assert features["espn_streak"] == 2


def test_a_row_without_league_counts_too():
    history = _history([_row(1, 7, date(2022, 3, 1), league=None)])

    assert espn_features_for_fighter(history, 7, FIGHT_DAY)["espn_prev_fights"] == 1


def test_ko_rate_denominator_is_all_fights():
    # 1 KO win, 1 SUB win, 1 decision win and 1 KO LOSS: rates over the 4 fights.
    history = _history([
        _row(1, 7, date(2020, 1, 1), "win", "KO/TKO"),
        _row(2, 7, date(2021, 1, 1), "win", "SUB - Armbar"),
        _row(3, 7, date(2022, 1, 1), "win", "U-DEC"),
        _row(4, 7, date(2023, 1, 1), "loss", "KO/TKO"),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_ko_rate"] == 0.25
    assert features["espn_sub_rate"] == 0.25
    assert features["espn_win_rate"] == 0.75


def test_method_search_is_case_insensitive_substring_as_in_the_experiment():
    # 'Technical Submission' contains SUB; the misspelt 'Sumission' does not. Pinned
    # as it is: fixing the method text would break parity with the experiment.
    history = _history([
        _row(1, 7, date(2020, 1, 1), "win", "Technical Submission (Rear Naked Choke)"),
        _row(2, 7, date(2021, 1, 1), "win", "Sumission (Arm Triangle)"),
        _row(3, 7, date(2022, 1, 1), "win", "ko/tko"),
        _row(4, 7, date(2023, 1, 1), "win", None),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_sub_rate"] == 0.25
    assert features["espn_ko_rate"] == 0.25
    assert features["espn_win_rate"] == 1.0


def test_ko_is_found_anywhere_in_the_method_not_only_at_the_start():
    # 625 methods are free text from ESPN's displayName: a 'TKO' that does not
    # start with 'KO' is still a KO win (substring search, as in the experiment).
    history = _history([
        _row(1, 7, date(2021, 1, 1), "win", "TKO - Doctor Stoppage"),
        _row(2, 7, date(2022, 1, 1), "win", "U-DEC"),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_ko_rate"] == 0.5
    assert features["espn_sub_rate"] == 0.0


def test_only_a_result_starting_with_w_is_a_win_and_the_rest_break_the_streak():
    history = _history([
        _row(1, 7, date(2019, 1, 1), "loss"),
        _row(2, 7, date(2020, 1, 1), "draw"),
        _row(3, 7, date(2021, 1, 1), "nc"),
        _row(4, 7, date(2022, 1, 1), None),
        _row(5, 7, date(2023, 1, 1), " Win "),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_prev_fights"] == 5
    assert features["espn_win_rate"] == 0.2
    assert features["espn_streak"] == 1


def test_days_years_and_title_fights():
    history = _history([
        _row(1, 7, date(2020, 6, 1), title=True),
        _row(2, 7, date(2024, 5, 1), title=False),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_days_since_last"] == 31
    years = round((FIGHT_DAY - date(2020, 6, 1)).days / 365.25, 2)
    assert features["espn_years_pro"] == years
    assert features["espn_title_fights"] == 1


def test_a_row_on_the_fight_day_does_not_count():
    history = _history([
        _row(1, 7, date(2023, 1, 1), "win"),
        _row(2, 7, FIGHT_DAY, "loss"),
    ])

    features = espn_features_for_fighter(history, 7, FIGHT_DAY)

    assert features["espn_prev_fights"] == 1
    assert features["espn_streak"] == 1
    assert features["espn_days_since_last"] == (FIGHT_DAY - date(2023, 1, 1)).days


def test_null_event_date_excluded():
    history = _history([
        _row(1, 7, date(2023, 1, 1), "win"),
        _row(2, 7, None, "loss"),
    ])

    assert espn_features_for_fighter(history, 7, FIGHT_DAY)["espn_prev_fights"] == 1


def test_no_history_zeros_and_nones():
    history = _history([_row(1, 8, date(2023, 1, 1))])  # rows of someone else only

    assert espn_features_for_fighter(history, 7, FIGHT_DAY) == NO_HISTORY


# --- build_preufc_block: 18 keys and the "unknown history" rule ----------------------

def _indexed(rows):
    return index_espn_history(_history(rows))


def test_block_has_18_keys_in_order():
    block = build_preufc_block(1, 2, FIGHT_DAY, {}, known_fighter_ids={1, 2})

    assert list(block) == PREUFC_COLUMNS
    assert len(block) == 18


def test_known_fighter_without_rows_is_no_history_zeros_and_nones():
    block = build_preufc_block(1, 2, FIGHT_DAY, {}, known_fighter_ids={1, 2})

    for base, value in NO_HISTORY.items():
        assert block[f"{base}_red"] == value
        assert block[f"{base}_blue"] == value


def test_unknown_fighter_gets_nine_none_even_with_rows():
    espn = _indexed([
        _row(1, 1, date(2022, 1, 1)),
        _row(2, 2, date(2022, 1, 1)),
    ])

    block = build_preufc_block(1, 2, FIGHT_DAY, espn, known_fighter_ids={2})

    assert all(block[f"{base}_red"] is None for base in PREUFC_BASES)
    assert block["espn_has_history_blue"] == 1
    assert block["espn_prev_fights_blue"] == 1


def test_unknown_fighter_without_rows_is_not_a_zero():
    block = build_preufc_block(1, 2, FIGHT_DAY, {}, known_fighter_ids=set())

    assert all(value is None for value in block.values())


def test_block_equals_the_function_on_the_whole_table_for_each_corner():
    rows = [
        _row(5, 1, date(2019, 5, 1), "win", "KO/TKO"),
        _row(4, 1, date(2019, 5, 1), "loss", "U-DEC"),
        _row(3, 1, date(2021, 2, 1), "win", "SUB", title=True, league="3321"),
        _row(9, 2, date(2018, 1, 1), "draw", "Draw"),
        _row(8, 2, FIGHT_DAY, "win", "KO/TKO"),
    ]
    table = _history(rows)

    block = build_preufc_block(1, 2, FIGHT_DAY, index_espn_history(table), {1, 2})

    for side, fighter_id in (("red", 1), ("blue", 2)):
        expected = espn_features_for_fighter(table, fighter_id, FIGHT_DAY)
        for base in PREUFC_BASES:
            assert _same(block[f"{base}_{side}"], expected[base]), (side, base)


def test_block_ignores_rows_on_or_after_the_fight_day():
    espn = _indexed([
        _row(1, 1, date(2023, 1, 1), "win"),
        _row(2, 1, FIGHT_DAY, "loss"),
        _row(3, 1, date(2025, 1, 1), "loss"),
    ])

    block = build_preufc_block(1, 2, FIGHT_DAY, espn, {1, 2})

    assert block["espn_prev_fights_red"] == 1
    assert block["espn_win_rate_red"] == 1.0


# --- preufc_diff_values (arm C, informative) ------------------------------------------

def test_diff_is_red_minus_blue_in_the_contract_order():
    espn = _indexed([
        _row(1, 1, date(2020, 1, 1), "win"),
        _row(2, 1, date(2021, 1, 1), "win"),
        _row(3, 2, date(2022, 1, 1), "loss"),
    ])
    block = build_preufc_block(1, 2, FIGHT_DAY, espn, {1, 2})

    diffs = preufc_diff_values(block)

    assert list(diffs) == PREUFC_DIFF_COLUMNS
    assert diffs["espn_prev_fights_diff"] == 1
    assert diffs["espn_win_rate_diff"] == 1.0
    assert diffs["espn_has_history_diff"] == 0


def test_diff_none_if_one_side_missing():
    espn = _indexed([_row(1, 1, date(2020, 1, 1), "win")])

    # Blue is known with no rows: its six "unknown" variables are None.
    block = build_preufc_block(1, 2, FIGHT_DAY, espn, {1, 2})
    diffs = preufc_diff_values(block)
    assert diffs["espn_win_rate_diff"] is None
    assert diffs["espn_prev_fights_diff"] == 1

    # Red unknown: all nine diffs are None.
    unknown = preufc_diff_values(build_preufc_block(1, 2, FIGHT_DAY, espn, {2}))
    assert all(value is None for value in unknown.values())


# --- loaders and index ----------------------------------------------------------------

def test_loader_sql_has_no_league_filter():
    sql = " ".join(ESPN_HISTORY_SQL.split()).upper()

    assert "FROM FIGHT_HISTORY_ESPN" in sql
    assert "WHERE" not in sql
    assert "3321" not in sql
    for column in ESPN_HISTORY_COLUMNS:
        assert column.upper() in sql


def test_loader_returns_dwcs_rows_and_runs_only_the_unfiltered_select(fakedb):
    rows = [
        _row(1, 7, date(2022, 3, 1), league="3359"),
        _row(2, 7, date(2023, 9, 12), league="3321"),
        _row(3, 8, date(2021, 1, 1), league=None),
    ]
    conn = fakedb.Connection(lambda sql, params: rows)

    loaded = load_espn_history_dataframe(conn)

    assert list(loaded.columns) == ESPN_HISTORY_COLUMNS
    assert sorted(loaded["id"]) == [1, 2, 3]
    assert (loaded["league_id"] == "3321").sum() == 1
    assert fakedb.executed_statements(conn) == [ESPN_HISTORY_SQL]
    assert fakedb.mutating_statements(conn) == []


def test_loader_on_an_empty_table_keeps_the_columns(fakedb):
    conn = fakedb.Connection(lambda sql, params: [])

    loaded = load_espn_history_dataframe(conn)

    assert loaded.empty
    assert list(loaded.columns) == ESPN_HISTORY_COLUMNS
    assert index_espn_history(loaded) == {}


def test_known_fighter_ids_need_espn_id_and_a_finished_sweep(fakedb):
    conn = fakedb.Connection(lambda sql, params: [{"id": 3}, {"id": 11}])

    known = load_espn_known_fighter_ids(conn)

    # The whole statement, normalized: BOTH conditions are required. An OR would
    # count a linked but never-swept fighter as known and switch the rule off.
    sql = " ".join(ESPN_KNOWN_FIGHTERS_SQL.split()).upper()
    assert sql == (
        "SELECT ID FROM FIGHTERS "
        "WHERE ESPN_ID IS NOT NULL AND ESPN_HISTORY_CHECKED_AT IS NOT NULL"
    )
    assert " OR " not in sql
    assert known == {3, 11}
    assert all(type(fighter_id) is int for fighter_id in known)
    assert fakedb.mutating_statements(conn) == []


def test_index_groups_by_fighter_and_sorts_stably_by_date_then_id():
    # Deliberately out of order: id 12 before 10 on the same day, and the oldest
    # row (2017) last. An index that does not sort would keep [12, 10, 11].
    rows = [
        _row(12, 1, date(2019, 5, 1)),
        _row(10, 1, date(2019, 5, 1)),
        _row(3, 2, date(2018, 1, 1)),
        _row(11, 1, date(2017, 1, 1)),
    ]

    for ordering in (rows, list(reversed(rows))):
        index = index_espn_history(_history(ordering))

        assert set(index) == {1, 2}
        assert all(type(fighter_id) is int for fighter_id in index)
        assert list(index[1]["id"]) == [11, 10, 12]
        assert list(index[2]["id"]) == [3]
        assert list(index[1].index) == [0, 1, 2]


def test_index_does_not_modify_its_input():
    table = _history([_row(2, 1, date(2019, 5, 1)), _row(1, 1, date(2019, 5, 1))])
    before = table.copy()

    index_espn_history(table)

    pd.testing.assert_frame_equal(table, before)
