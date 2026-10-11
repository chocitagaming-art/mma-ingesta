"""Phase 4: the winner CSV v2 carries the pre-UFC block.

Each row of training_dataset.csv is now fight_id, event_date, the 49
WINNER_FEATURE_COLUMNS in their order (20 legacy diffs, ufc_prev_fights per corner,
the 18 pre-UFC columns per corner, the 9 pre-UFC diffs) and target. The block is
build_preufc_block with cutoff = the date of the bout, over an explicit ESPN index and
an explicit set of fighters with KNOWN history (an espn_id and a finished sweep).

The generator reads both from a frozen snapshot (--espn-snapshot DIR) or, without
one, from the database in a single connection; the two must give the same CSV. It
prints the block's coverage per metro partition, never a metric.

Synthetic and in memory: the fake connection stands in for Postgres, nothing is
written outside tmp_path.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import date, timedelta

import pandas as pd
import pytest

import src.prediction.features.dataset_guard as dataset_guard
import src.prediction.features.output as output
from src.prediction import split
from src.prediction.features.dataset_guard import check_winner_dataset
from src.prediction.features.db import (
    ESPN_HISTORY_SQL,
    ESPN_KNOWN_FIGHTERS_SQL,
    index_espn_history,
)
from src.prediction.features.method_training import build_method_training_dataset
from src.prediction.features.preufc import (
    ESPN_HISTORY_COLUMNS,
    build_preufc_block,
    preufc_diff_values,
)
from src.prediction.features.preufc_snapshot import (
    ESPN_HISTORY_FILE,
    KNOWN_IDS_FILE,
    read_manifest,
    take_snapshot,
)
from src.prediction.features.training import build_training_dataset
from src.prediction.features.types import (
    FEATURE_COLUMNS,
    PREUFC_BASES,
    PREUFC_COLUMNS,
    PREUFC_DIFF_COLUMNS,
    UFC_COUNT_COLUMNS,
    WINNER_FEATURE_COLUMNS,
)
from src.prediction.split import (
    CAL_START,
    TEST_END,
    TEST_START,
    chronological_three_way_split,
)

CSV_V2_COLUMNS = ["fight_id", "event_date", *WINNER_FEATURE_COLUMNS, "target"]
SHARED_WITH_B2 = [
    "fight_id", "event_date", *FEATURE_COLUMNS, *UFC_COUNT_COLUMNS, "target"
]
STAT_KEYS = (
    "sig_strikes_landed",
    "sig_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "control_time_seconds",
    "knockdowns",
)
FIGHTERS = {
    1: (date(1990, 1, 1), 180.0, 185.0),
    2: (date(1991, 2, 2), 178.0, 181.0),
    3: (date(1989, 3, 3), 183.0, 188.0),
    4: (date(1992, 4, 4), 175.0, 177.0),
    5: (date(1993, 5, 5), 185.0, 190.0),
    6: (date(1994, 6, 6), 179.0, 182.0),
}
# Fighter 6 has ESPN rows but was never swept: unknown history.
KNOWN = {1, 2, 3, 4, 5}


def _fight(fight_id, day, red, blue, winner, method="U-DEC"):
    row = {
        "fight_id": fight_id,
        "event_date": day,
        "fighter_red_id": red,
        "fighter_blue_id": blue,
        "winner_id": winner,
        "method": method,
        "end_round": 3,
        "end_time": "5:00",
        "is_title_fight": False,
        "scheduled_rounds": 3,
        "weight_class": "Lightweight",
    }
    for side, fighter_id in (("red", red), ("blue", blue)):
        birth, height, reach = FIGHTERS[fighter_id]
        row[f"{side}_birth_date"] = birth
        row[f"{side}_height_cm"] = height
        row[f"{side}_reach_cm"] = reach
        for offset, key in enumerate(STAT_KEYS):
            row[f"{side}_{key}"] = 10 + 3 * offset + fight_id + fighter_id
    return row


def _fights() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _fight(1, date(2015, 3, 1), 1, 2, 1),
            _fight(2, date(2015, 3, 1), 3, 4, 4),
            _fight(3, date(2016, 5, 1), 1, 3, 3),
            _fight(4, date(2016, 5, 1), 5, 2, 5),
            _fight(5, date(2017, 1, 1), 6, 4, 6),
            _fight(6, date(2017, 1, 1), 1, 5, None, "M-DEC"),  # draw: no target
            _fight(7, date(2022, 1, 15), 3, 5, 5),  # calibration window
            _fight(8, date(2024, 1, 13), 2, 6, 2),  # test window
            _fight(9, date(2026, 9, 30), 4, 1, 1),  # after the metro's TEST_END
        ]
    )


def _espn_row(row_id, fighter_id, day, result="win", method="KO/TKO", title=False,
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


ESPN_ROWS = [
    _espn_row(1, 1, date(2013, 1, 1), "win", "KO/TKO"),
    _espn_row(2, 1, date(2014, 6, 1), "loss", "U-DEC"),
    # Same day as fight 1: must not inform it (strict cut), but counts for fight 3.
    _espn_row(3, 1, date(2015, 3, 1), "win", "SUB - Armbar", league="3321"),
    _espn_row(4, 3, date(2012, 1, 1), "win", "TKO - Doctor Stoppage"),
    _espn_row(5, 3, date(2016, 5, 1), "loss", "U-DEC"),  # same day as fight 3
    _espn_row(6, 4, date(2014, 1, 1), "win", "U-DEC", title=True),
    _espn_row(7, 5, date(2016, 4, 30), "win", "KO/TKO"),  # the day before fight 4
    _espn_row(8, 6, date(2016, 1, 1), "win", "KO/TKO"),  # unknown fighter
]


def _espn_frame() -> pd.DataFrame:
    return pd.DataFrame(ESPN_ROWS, columns=ESPN_HISTORY_COLUMNS)


@pytest.fixture(scope="module")
def built():
    return build_training_dataset(
        _fights(),
        pd.DataFrame(),
        espn_by_fighter=index_espn_history(_espn_frame()),
        known_fighter_ids=KNOWN,
    )


def _row(result, fight_id) -> pd.Series:
    return result.dataset.set_index("fight_id").loc[fight_id]


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


# --- the dataset ---------------------------------------------------------------------


def test_csv_v2_columns_order(built):
    assert list(built.dataset.columns) == CSV_V2_COLUMNS
    assert len(CSV_V2_COLUMNS) == 52
    assert sorted(built.dataset["fight_id"]) == [1, 2, 3, 4, 5, 7, 8, 9]


def test_the_espn_index_and_the_known_ids_are_explicit():
    with pytest.raises(TypeError):
        build_training_dataset(_fights(), pd.DataFrame())


def test_block_cutoff_is_fight_date(built):
    # Fight 1 (2015-03-01): fighter 1's DWCS win that same day does not count yet.
    first = _row(built, 1)
    assert first["espn_prev_fights_red"] == 2
    assert first["espn_streak_red"] == 0  # the last prior fight was a loss
    assert first["espn_win_rate_red"] == 0.5
    # Fight 3 (2016-05-01): now it does; fighter 3's loss that day does not.
    third = _row(built, 3)
    assert third["espn_prev_fights_red"] == 3
    assert third["espn_streak_red"] == 1
    assert third["espn_prev_fights_blue"] == 1
    assert third["espn_ko_rate_blue"] == 1.0  # 'TKO - Doctor Stoppage' is a KO
    # Fight 4: a row the day before the bout counts.
    fourth = _row(built, 4)
    assert fourth["espn_prev_fights_red"] == 1
    assert fourth["espn_days_since_last_red"] == 1


def test_known_without_rows_and_unknown_corners(built):
    # Fighter 2: swept, no ESPN rows -> has_history 0, counts 0, the rest NaN.
    fourth = _row(built, 4)
    assert fourth["espn_has_history_blue"] == 0
    assert fourth["espn_prev_fights_blue"] == 0
    assert fourth["espn_title_fights_blue"] == 0
    assert _missing(fourth["espn_win_rate_blue"])
    # Fighter 6: rows in the table but never swept -> his nine are NaN.
    fifth = _row(built, 5)
    assert all(_missing(fifth[f"{base}_red"]) for base in PREUFC_BASES)
    assert fifth["espn_title_fights_blue"] == 1
    assert all(_missing(fifth[column]) for column in PREUFC_DIFF_COLUMNS)
    assert fourth["espn_has_history_diff"] == 1
    assert fourth["espn_prev_fights_diff"] == 1
    assert _missing(fourth["espn_win_rate_diff"])


def test_every_row_carries_the_block_and_its_diffs(built):
    index = index_espn_history(_espn_frame())
    fights = _fights().set_index("fight_id")
    compared = 0
    for row in built.dataset.to_dict("records"):
        fight = fights.loc[row["fight_id"]]
        block = build_preufc_block(
            fight["fighter_red_id"], fight["fighter_blue_id"], row["event_date"],
            index, KNOWN,
        )
        expected = {**block, **preufc_diff_values(block)}
        for column in PREUFC_COLUMNS + PREUFC_DIFF_COLUMNS:
            got, want = row[column], expected[column]
            if _missing(want):
                assert _missing(got), (row["fight_id"], column)
            else:
                assert got == want, (row["fight_id"], column)
            compared += 1
    assert compared == 8 * 27


def test_the_block_moves_nothing_else(built):
    """The rows and the 22 columns B2 wrote (+ target) are the same with or without
    the block: the CSV v2 only adds columns."""
    without = build_training_dataset(
        _fights(), pd.DataFrame(), espn_by_fighter={}, known_fighter_ids=set()
    )
    pd.testing.assert_frame_equal(
        built.dataset[SHARED_WITH_B2], without.dataset[SHARED_WITH_B2]
    )
    assert without.dataset[PREUFC_COLUMNS].isna().all().all()


def test_dataset_guard_passes_with_the_new_columns(built):
    check_winner_dataset(built.dataset, today=date(2026, 10, 5))


def test_legacy_train_reads_only_the_20_columns_from_the_csv_v2(
    built, tmp_path, monkeypatch
):
    import src.prediction.train as train

    path = tmp_path / "training_dataset.csv"
    built.dataset.to_csv(path, index=False)
    monkeypatch.setattr(train, "DATASET_PATH", path)

    dataset = train.load_dataset()

    assert set(PREUFC_COLUMNS + PREUFC_DIFF_COLUMNS) <= set(dataset.columns)
    columns = train.get_available_feature_columns(dataset)
    assert columns and set(columns) <= set(FEATURE_COLUMNS)


def test_the_method_csv_takes_none_of_the_new_columns():
    result = build_method_training_dataset(_fights(), pd.DataFrame())
    added = set(UFC_COUNT_COLUMNS + PREUFC_COLUMNS + PREUFC_DIFF_COLUMNS)
    assert not added & set(result.dataset.columns)


# --- the generator -------------------------------------------------------------------


class _Settings:
    database_url = "postgresql://do-not-use"


@pytest.fixture
def generator(monkeypatch, tmp_path, fakedb):
    """output.main without a database: fights from _fights(), ESPN from a fake
    connection (database path) or from a snapshot taken off that same connection."""

    def respond(sql, params):
        if sql == ESPN_HISTORY_SQL:
            return sorted(
                ESPN_ROWS, key=lambda r: (r["fighter_id"], r["event_date"], r["id"])
            )
        if sql == ESPN_KNOWN_FIGHTERS_SQL:
            return [{"id": fighter_id} for fighter_id in sorted(KNOWN)]
        if "MAX(CREATED_AT)" in " ".join(sql.split()).upper():
            return [{"max_created_at": None, "max_updated_at": None}]
        raise AssertionError(f"unexpected SQL: {sql}")

    state = {"connections": []}

    @contextmanager
    def fake_connect(url):
        assert url == _Settings.database_url
        connection = fakedb.Connection(respond)
        state["connections"].append(connection)
        yield connection

    class _Today(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 5)

    monkeypatch.setattr(dataset_guard, "date", _Today)
    monkeypatch.setattr(output, "get_settings", lambda: _Settings())
    monkeypatch.setattr(output, "load_base_dataframe", lambda _url: _fights())
    monkeypatch.setattr(output, "load_rankings_dataframe", lambda _url: pd.DataFrame())
    monkeypatch.setattr(output, "connect", fake_connect)
    monkeypatch.setattr(
        output, "create_output_table", lambda *a, **k: pytest.fail("touched the DB")
    )
    state["snapshot"] = tmp_path / "snap"
    take_snapshot(fakedb.Connection(respond), state["snapshot"])
    state["csv"] = tmp_path / "training_dataset.csv"
    monkeypatch.setattr(output, "OUTPUT_CSV_PATH", state["csv"])
    state["fakedb"] = fakedb
    return state


def test_without_a_snapshot_espn_comes_from_the_database_in_one_connection(
    generator,
):
    output.main()

    assert len(generator["connections"]) == 1
    connection = generator["connections"][0]
    fakedb = generator["fakedb"]
    assert fakedb.executed_statements(connection) == [
        ESPN_HISTORY_SQL,
        ESPN_KNOWN_FIGHTERS_SQL,
    ]
    assert fakedb.mutating_statements(connection) == []
    written = pd.read_csv(generator["csv"])
    assert list(written.columns) == CSV_V2_COLUMNS


def test_espn_snapshot_option_skips_db_and_gives_the_same_csv(generator):
    output.main()
    from_database = generator["csv"].read_bytes()
    generator["csv"].unlink()
    generator["connections"].clear()

    output.main(espn_snapshot=generator["snapshot"])

    assert generator["connections"] == []  # no database connection for ESPN...
    assert generator["csv"].read_bytes() == from_database  # ...and the same bytes


def test_the_generator_prints_the_block_coverage_and_no_metric(generator, capsys):
    output.main(espn_snapshot=generator["snapshot"])

    out = capsys.readouterr().out
    lines = {line.split()[0]: line.split()[1:] for line in out.splitlines()
             if line.strip().split(" ")[0] in {"train", "calibracion", "test", "fuera"}}
    # corners | unknown | has_history=0 | with history, per partition.
    assert lines["train"] == ["10", "1", "2", "7"]
    assert lines["calibracion"] == ["2", "0", "0", "2"]
    assert lines["test"] == ["2", "1", "1", "0"]
    assert lines["fuera"] == ["2", "0", "0", "2"]
    assert "snapshot" in out
    lower = out.lower()
    for word in ("auc", "brier", "accuracy", "acierto", "log loss", "logloss"):
        assert word not in lower, word


def test_preufc_coverage_counts_corners_per_partition(built):
    coverage = output.preufc_coverage(built.dataset)

    assert list(coverage.index) == ["train", "calibracion", "test", "fuera"]
    assert list(coverage.columns) == [
        "corners", "unknown", "no_history", "with_history"
    ]
    assert coverage.loc["train"].tolist() == [10, 1, 2, 7]
    assert coverage.loc["test"].tolist() == [2, 1, 1, 0]
    assert int(coverage["corners"].sum()) == 2 * len(built.dataset)


# The three frontier dates of the frozen metro and the day on each side. The real
# CSV has fights on them, so a slipped comparator would move the printed coverage
# and the Annex A counts in silence.
BOUNDARY_LABELS = {
    CAL_START - timedelta(days=1): "train",
    CAL_START: "calibracion",
    TEST_START - timedelta(days=1): "calibracion",
    TEST_START: "test",
    TEST_END: "test",
    TEST_END + timedelta(days=1): "fuera",
}


@pytest.mark.parametrize(
    "as_text", [False, True], ids=["datetime.date", "iso-text-like-the-csv"]
)
def test_metro_partition_labels_the_frontier_dates(as_text):
    dates = pd.Series(
        [day.isoformat() if as_text else day for day in BOUNDARY_LABELS]
    )

    assert output.metro_partition(dates).tolist() == list(BOUNDARY_LABELS.values())


def test_metro_partition_is_the_split_of_split_py(monkeypatch):
    """The coverage table labels each row exactly as chronological_three_way_split
    partitions it, and 'fuera' is what the split leaves out."""
    monkeypatch.setattr(split, "MIN_TEST_ROWS", 1)
    days = list(BOUNDARY_LABELS) + [
        date(1999, 1, 1),
        date(2022, 6, 1),
        date(2025, 1, 1),
        date(2027, 1, 1),
    ]
    frame = pd.DataFrame(
        {"fight_id": range(len(days) * 2), "event_date": days + days[::-1]}
    )

    train, calibration, test = chronological_three_way_split(frame)
    labels = output.metro_partition(frame["event_date"])

    assert set(frame.index[labels == "train"]) == set(train.index)
    assert set(frame.index[labels == "calibracion"]) == set(calibration.index)
    assert set(frame.index[labels == "test"]) == set(test.index)
    split_rows = set(train.index) | set(calibration.index) | set(test.index)
    assert set(frame.index[labels == "fuera"]) == set(frame.index) - split_rows


def test_the_generator_log_names_both_snapshot_files_by_sha256(generator, capsys):
    """known_fighter_ids.csv decides every unknown-history corner: the log has to
    tie the CSV to it, not only to espn_history.csv."""
    output.main(espn_snapshot=generator["snapshot"])

    out = capsys.readouterr().out
    files = read_manifest(generator["snapshot"])["files"]
    for name in (ESPN_HISTORY_FILE, KNOWN_IDS_FILE):
        assert f"{name} sha256 {files[name]['sha256']}" in out, name


def test_cli_accepts_the_snapshot_option(tmp_path):
    args = output.parse_args(["--espn-snapshot", str(tmp_path)])
    assert args.espn_snapshot == tmp_path
    assert args.write_table is False

    defaults = output.parse_args([])
    assert defaults.espn_snapshot is None
    assert defaults.write_table is False
