"""The frozen "metro": the train / calibration-holdout / test split by FIXED DATES.

Why
---
The split used to be POSITIONAL: test = the last 20% of the rows, calibration =
the 16% before it, train = the rest. Adding OLD rows - exactly what phase 4 does
with the debutant / pre-UFC rows - shifted all three boundaries at once, so two
models were no longer measured on the same fights and a before/after comparison
stopped meaning anything. It had already happened: with the clean training CSV
of 30-sep-2026 the positional boundaries moved 84 days against the published
model. A ruler that stretches when you add data is not a ruler.

So the boundaries are now DATES, defined here and nowhere else, and every caller
(``train.py``, ``calibrate.py``, ``evaluate.py``, ``train_method.py``) goes
through the functions below::

    train        event_date <  CAL_START
    calibration  CAL_START  <= event_date < TEST_START
    test         TEST_START <= event_date <= TEST_END
    (none)       event_date >  TEST_END   -> left out, and logged

Consequences, all wanted:
- old rows (dated before CAL_START) only grow train; calibration and test keep
  exactly the same fights;
- rows dated INSIDE the test window join the test: phase 4 debutant fights must
  be judged, not dropped;
- rows after TEST_END join NO partition - not even train - and the split logs
  how many were left out.

How the constants were derived (2026-10-04)
-------------------------------------------
From the clean ``training_dataset.csv`` of 30-sep-2026 (5,412 rows, 1995-07-14
to 2026-09-26), reproducing the positional split it replaced (train 3,463 /
calibration 866 / test 1,083) as closely as possible at DATE granularity. Both
positional cuts fell inside one event date, so each whole date went to the side
that moves fewer rows (for the test boundary, the side that keeps the test
closest to 1,083): train 3,465 (+2), calibration 865 (-1), test 1,082 (-1).
TEST_END is the last event date in that CSV. Each start date is the first event
date of its partition.

Re-freezing
-----------
Moving these dates is a deliberate, dated change - never a side effect of a new
CSV. Change the constants, the pinned values in ``tests/test_metro_congelado.py``,
and record why and when in ``docs/DECISIONS.md``. Fights after TEST_END are not
trained on until that happens.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

# The metro, frozen on 2026-10-04. See the module docstring before touching them.
CAL_START = date(2021, 3, 20)
TEST_START = date(2023, 10, 7)
TEST_END = date(2026, 9, 26)

# The frozen test window holds ~1,080 fights. Far fewer means the CSV is not the
# one this metro was frozen on (or not a training CSV at all).
MIN_TEST_ROWS = 500

PARTITION_NAMES = ("train", "calibracion", "test")


def _event_dates(dataset: pd.DataFrame) -> pd.Series:
    event_dates = pd.to_datetime(dataset["event_date"])
    missing = int(event_dates.isna().sum())
    if missing:
        raise RuntimeError(
            f"Metro congelado: {missing} filas sin event_date. Ninguna particion "
            "las cogeria y se perderian en silencio: el CSV no es el esperado."
        )
    return event_dates


def _partition_masks(dataset: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Boolean masks (train, calibration, test) over ``dataset``, validated.

    Logs the rows that fall after TEST_END (they belong to no partition) and
    raises when a partition is empty or the test is below MIN_TEST_ROWS."""
    event_dates = _event_dates(dataset)
    cal_start = pd.Timestamp(CAL_START)
    test_start = pd.Timestamp(TEST_START)
    test_end = pd.Timestamp(TEST_END)

    train_mask = event_dates < cal_start
    calibration_mask = (event_dates >= cal_start) & (event_dates < test_start)
    test_mask = (event_dates >= test_start) & (event_dates <= test_end)

    left_out = int((event_dates > test_end).sum())
    if left_out:
        logger.warning(
            "%d peleas posteriores al metro, fuera (event_date > %s): no entran "
            "ni en train, ni en calibracion, ni en test.",
            left_out,
            TEST_END.isoformat(),
        )

    windows = (
        f"antes del {CAL_START.isoformat()}",
        f"del {CAL_START.isoformat()} al {TEST_START.isoformat()} (excluido)",
        f"del {TEST_START.isoformat()} al {TEST_END.isoformat()}",
    )
    for name, mask, window in zip(
        PARTITION_NAMES, (train_mask, calibration_mask, test_mask), windows, strict=True
    ):
        if not mask.any():
            raise RuntimeError(
                f"Metro congelado: la particion {name} sale vacia (no hay peleas "
                f"{window}). El CSV no es el esperado."
            )
    test_rows = int(test_mask.sum())
    if test_rows < MIN_TEST_ROWS:
        raise RuntimeError(
            f"Metro congelado: el test tiene {test_rows} peleas, menos de "
            f"{MIN_TEST_ROWS} ({windows[2]}). El CSV no es el esperado."
        )
    return train_mask, calibration_mask, test_mask


def chronological_three_way_split(
    dataset: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split into (train, calibration_holdout, test) by the frozen dates.

    The base model trains on ``train`` only; ``calibrate.py`` fits the calibrator
    on ``calibration_holdout`` (rows the base never saw); ``evaluate.py`` scores
    ``test``. Rows keep their input order and index (no shuffle): callers pass
    the dataset sorted by event_date, as ``load_dataset`` does, and
    ``build_time_series_folds`` relies on that order inside train."""
    train_mask, calibration_mask, test_mask = _partition_masks(dataset)
    return (
        dataset.loc[train_mask].copy(),
        dataset.loc[calibration_mask].copy(),
        dataset.loc[test_mask].copy(),
    )


def chronological_train_test_split(dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Two-way variant: (train + calibration, test), with the SAME test as
    ``chronological_three_way_split`` so test metrics stay comparable."""
    train_mask, calibration_mask, test_mask = _partition_masks(dataset)
    return (
        dataset.loc[train_mask | calibration_mask].copy(),
        dataset.loc[test_mask].copy(),
    )


def _test_split_index(dataset: pd.DataFrame) -> int:
    """Positional index of the first test row in a dataset sorted by event_date
    (= the rows dated before TEST_START). Kept for the archived experiment
    scripts that still import it; new code should use the split functions."""
    return int((_event_dates(dataset) < pd.Timestamp(TEST_START)).sum())
