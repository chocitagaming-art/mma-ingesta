"""The frozen "metro": train / calibration / test are cut by FIXED DATES.

Why it exists. The split used to be positional (last 20% = test, the 16% before
it = calibration). Adding OLD rows - which is exactly what phase 4 does with the
debutant / pre-UFC rows - moved all three boundaries at once, so a before/after
comparison of two models was no longer measured on the same fights. With the
clean CSV of 30-sep-2026 the boundaries had already moved 84 days.

These tests go through ``src.prediction.train`` on purpose: that is the import
path every caller (train, calibrate, evaluate, train_method) uses.

Pure: synthetic frames, no database, no model.
"""

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.prediction import train

# The metro frozen on 2026-10-04 from the clean training CSV of 30-sep-2026.
# Written as literals here (not read from the module) so a change of the
# constants is a deliberate edit in two places.
CAL_START = date(2021, 3, 20)
TEST_START = date(2023, 10, 7)
TEST_END = date(2026, 9, 26)
MIN_TEST_ROWS = 500

LEFT_OUT_MESSAGE = "peleas posteriores al metro, fuera"


def _frame(dates, first_fight_id: int) -> pd.DataFrame:
    dates = pd.to_datetime(pd.Series(list(dates)))
    return pd.DataFrame(
        {
            "fight_id": np.arange(first_fight_id, first_fight_id + len(dates)),
            "event_date": dates.to_numpy(),
            "target": np.arange(len(dates)) % 2,
        }
    )


def _base_dataset(rows_per_date: int = 4) -> pd.DataFrame:
    """Weekly events (Saturdays, like the UFC calendar) from 2015 to TEST_END,
    several fights per date - enough for a test window above MIN_TEST_ROWS."""
    saturdays = pd.date_range("2015-01-03", TEST_END, freq="7D")
    dates = np.repeat(saturdays.to_numpy(), rows_per_date)
    return _frame(dates, first_fight_id=1)


def _sorted(dataset: pd.DataFrame) -> pd.DataFrame:
    # The same ordering load_dataset() applies before splitting.
    return dataset.sort_values(["event_date", "fight_id", "target"]).reset_index(drop=True)


def _ids(frame: pd.DataFrame) -> set[int]:
    return set(frame["fight_id"].tolist())


def _split_ids(dataset: pd.DataFrame) -> tuple[set[int], set[int], set[int]]:
    train_df, calibration_df, test_df = train.chronological_three_way_split(dataset)
    return _ids(train_df), _ids(calibration_df), _ids(test_df)


# --- The constants ------------------------------------------------------------


def test_the_metro_constants_are_pinned():
    """Re-freezing the metro is a deliberate, dated change: this test must be
    edited together with the constants."""
    from src.prediction import split

    assert split.CAL_START == CAL_START
    assert split.TEST_START == TEST_START
    assert split.TEST_END == TEST_END
    assert split.MIN_TEST_ROWS == MIN_TEST_ROWS


def test_every_caller_uses_the_one_frozen_split():
    from src.prediction import calibrate, evaluate, split, train_method

    for module in (train, calibrate, evaluate, train_method):
        assert module.chronological_three_way_split is split.chronological_three_way_split
    assert train.chronological_train_test_split is split.chronological_train_test_split


# --- What the freeze is for -----------------------------------------------------


def test_prepending_old_rows_does_not_move_calibration_or_test():
    base = _base_dataset()
    old = _frame(pd.date_range("1994-01-01", periods=500, freq="7D"), first_fight_id=100_000)
    assert old["event_date"].max() < pd.Timestamp("2015-01-03")

    train_before, cal_before, test_before = _split_ids(_sorted(base))
    train_after, cal_after, test_after = _split_ids(_sorted(pd.concat([old, base])))

    assert cal_after == cal_before
    assert test_after == test_before
    assert train_after == train_before | _ids(old)


def test_rows_after_test_end_join_no_partition_and_are_reported(caplog):
    base = _base_dataset()
    later = _frame(
        [TEST_END + timedelta(days=7)] * 20 + [TEST_END + timedelta(days=14)] * 10,
        first_fight_id=200_000,
    )

    train_before, cal_before, test_before = _split_ids(_sorted(base))
    with caplog.at_level(logging.INFO):
        train_after, cal_after, test_after = _split_ids(_sorted(pd.concat([base, later])))

    assert (train_after, cal_after, test_after) == (train_before, cal_before, test_before)
    assert f"30 {LEFT_OUT_MESSAGE}" in caplog.text


def test_nothing_is_reported_when_no_row_is_left_out(caplog):
    with caplog.at_level(logging.INFO):
        train.chronological_three_way_split(_sorted(_base_dataset()))
    assert LEFT_OUT_MESSAGE not in caplog.text


def test_rows_inside_the_test_window_are_judged_in_test():
    """Phase 4 debutant rows dated inside the window must be scored, not dropped."""
    base = _base_dataset()
    debutants = _frame(
        [date(2024, 3, 16)] * 25 + [date(2025, 11, 1)] * 25, first_fight_id=300_000
    )

    train_before, cal_before, test_before = _split_ids(_sorted(base))
    train_after, cal_after, test_after = _split_ids(_sorted(pd.concat([base, debutants])))

    assert train_after == train_before
    assert cal_after == cal_before
    assert test_after == test_before | _ids(debutants)


@pytest.mark.parametrize(
    ("event_date", "partition"),
    [
        (CAL_START - timedelta(days=1), "train"),
        (CAL_START, "calibration"),
        (TEST_START - timedelta(days=1), "calibration"),
        (TEST_START, "test"),
        (TEST_END, "test"),
        (TEST_END + timedelta(days=1), None),
    ],
)
def test_boundary_dates_land_in_the_documented_partition(event_date, partition):
    probe = _frame([event_date], first_fight_id=400_000)
    ids = dict(
        zip(
            ("train", "calibration", "test"),
            _split_ids(_sorted(pd.concat([_base_dataset(), probe]))),
            strict=True,
        )
    )
    landed = [name for name, members in ids.items() if 400_000 in members]
    assert landed == ([partition] if partition else [])


def test_the_partitions_are_disjoint_and_keep_the_chronological_order():
    train_df, calibration_df, test_df = train.chronological_three_way_split(
        _sorted(_base_dataset())
    )
    assert train_df["event_date"].max() < pd.Timestamp(CAL_START)
    assert calibration_df["event_date"].min() >= pd.Timestamp(CAL_START)
    assert calibration_df["event_date"].max() < pd.Timestamp(TEST_START)
    assert test_df["event_date"].min() >= pd.Timestamp(TEST_START)
    assert test_df["event_date"].max() <= pd.Timestamp(TEST_END)
    # build_time_series_folds cuts the train partition positionally: it must
    # come out in the same chronological order it went in.
    assert train_df["event_date"].is_monotonic_increasing


def test_the_two_way_split_shares_the_test_and_merges_train_with_calibration():
    dataset = _sorted(_base_dataset())
    train_ids, cal_ids, test_ids = _split_ids(dataset)
    two_way_train, two_way_test = train.chronological_train_test_split(dataset)

    assert _ids(two_way_test) == test_ids
    assert _ids(two_way_train) == train_ids | cal_ids


def test_test_split_index_counts_the_rows_before_the_test():
    """Kept for the archived experiment scripts that still import it."""
    dataset = _sorted(_base_dataset())
    train_ids, cal_ids, _ = _split_ids(dataset)
    assert train._test_split_index(dataset) == len(train_ids) + len(cal_ids)


def test_event_dates_given_as_text_are_understood():
    dataset = _sorted(_base_dataset())
    as_text = dataset.assign(event_date=dataset["event_date"].dt.strftime("%Y-%m-%d"))
    assert _split_ids(as_text) == _split_ids(dataset)


# --- The guard: an unexpected CSV must fail loudly ------------------------------


@pytest.mark.parametrize(
    "split_function",
    ["chronological_three_way_split", "chronological_train_test_split"],
)
def test_a_test_window_below_the_minimum_raises(split_function):
    # One fight per Saturday: ~157 rows in the test window.
    thin = _sorted(_base_dataset(rows_per_date=1))
    with pytest.raises(RuntimeError, match="el test tiene"):
        getattr(train, split_function)(thin)


@pytest.mark.parametrize(
    ("missing_from", "missing_to", "partition"),
    [
        (None, CAL_START, "train"),
        (CAL_START, TEST_START, "calibracion"),
    ],
)
def test_an_empty_partition_raises(missing_from, missing_to, partition):
    dataset = _sorted(_base_dataset())
    dates = dataset["event_date"]
    gone = dates < pd.Timestamp(missing_to)
    if missing_from is not None:
        gone &= dates >= pd.Timestamp(missing_from)
    with pytest.raises(RuntimeError, match=f"particion {partition} sale vacia"):
        train.chronological_three_way_split(dataset[~gone].reset_index(drop=True))


def test_a_missing_event_date_raises_instead_of_silently_dropping_the_row():
    dataset = _sorted(_base_dataset())
    dataset.loc[10, "event_date"] = pd.NaT
    with pytest.raises(RuntimeError, match="event_date"):
        train.chronological_three_way_split(dataset)
