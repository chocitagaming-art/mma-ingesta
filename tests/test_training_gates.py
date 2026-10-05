"""Phase 4: the winner CSV opens its two gates (debutants and accuracy None).

Until phase 4, build_training_dataset dropped a fight whenever either corner had
no UFC summary (a debutant, or prior UFC fights without fight_stats) and whenever
sig_strike_accuracy or takedown_accuracy came out None (zero attempts). That
threw away almost every fight with a debutant corner and a third of the
novices: exactly the population the pre-UFC block is meant to fix.

Now those fights ENTER: their history diffs stay NaN (the physical diffs are
computed as always) and two explicit per-corner counts, ufc_prev_fights_red /
ufc_prev_fights_blue, say how many prior UFC fights each corner had (0 for a
debutant, never NaN). What must NOT move:

* every row that already entered: same fight_ids, same 20 values, same target,
  byte for byte. The golden files were written by the PRE-phase-4 code
  (4dfeb86) on the synthetic league below; never regenerate them with newer
  code, that would defeat their purpose.
* the METHOD dataset, which keeps its own copy of the old exclusion policy.

All synthetic and in memory: no database, nothing written outside tmp_path.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import src.prediction.features.output as output
from src.prediction.features import fighter_history, training
from src.prediction.features.fighter_history import (
    build_fighter_history_dataframe,
    compute_fighter_history,
)
from src.prediction.features.method_training import build_method_training_dataset
from src.prediction.features.preufc import ESPN_HISTORY_COLUMNS
from src.prediction.features.training import build_training_dataset
from src.prediction.features.types import (
    FEATURE_COLUMNS,
    UFC_COUNT_COLUMNS,
    WINNER_FEATURE_COLUMNS,
    FighterHistorySummary,
)

FIXTURES = Path(__file__).parent / "fixtures"
WINNER_GOLDEN = FIXTURES / "training_gates_winner_golden.csv"
METHOD_GOLDEN = FIXTURES / "training_gates_method_golden.csv"

PHYSICAL_DIFFS = ["height_cm_diff", "reach_cm_diff", "age_diff"]
HISTORY_DIFFS = [column for column in FEATURE_COLUMNS if column not in PHYSICAL_DIFFS]
LEGACY_CSV_COLUMNS = ["fight_id", "event_date", *FEATURE_COLUMNS, "target"]

# What the pre-phase-4 code (4dfeb86) reported on _league(): 20 fights seen, one
# draw without target, and the rows each gate threw away.
OLD_EXCLUDED_NO_TARGET = 1
OLD_EXCLUDED_MISSING_HISTORY = 10
OLD_EXCLUDED_MISSING_STATS = 3

# fighter_id -> (birth_date, height_cm, reach_cm). Fighter 2 has no height and
# fighter 11 no birth date, so some ADMITTED rows carry a NaN physical diff too.
FIGHTERS = {
    1: (date(1985, 3, 1), 180.0, 185.0),
    2: (date(1986, 7, 9), None, 182.0),
    3: (date(1984, 1, 20), 178.0, 180.0),
    4: (date(1988, 11, 2), 183.0, 188.0),
    5: (date(1987, 5, 15), 175.0, 178.0),
    6: (date(1983, 9, 30), 185.0, 190.0),
    7: (date(1989, 2, 14), 177.0, 179.0),
    8: (date(1990, 6, 6), 181.0, 184.0),
    9: (date(1982, 12, 12), 179.0, 183.0),
    10: (date(1991, 4, 4), 176.0, 177.0),
    11: (None, 182.0, 186.0),
    12: (date(1992, 8, 8), 174.0, 176.0),
}
NEVER_ATTEMPTS_TAKEDOWNS = {7}
STAT_KEYS = (
    "sig_strikes_landed",
    "sig_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "control_time_seconds",
    "knockdowns",
)


def _corner_stats(fight_id: int, fighter_id: int) -> dict[str, int]:
    """Deterministic, distinct-ish per-corner stats (no RNG: stable forever).

    At least one takedown and 30 significant strikes are always attempted, so an
    accuracy is None only where a fight below asks for it."""
    sig_landed = 20 + (7 * fight_id + 3 * fighter_id) % 40
    td_landed = (fight_id + fighter_id) % 3
    td_attempted = td_landed + 1 + (2 * fight_id + fighter_id) % 3
    if fighter_id in NEVER_ATTEMPTS_TAKEDOWNS:
        td_landed = td_attempted = 0
    return {
        "sig_strikes_landed": sig_landed,
        "sig_strikes_attempted": sig_landed + 30 + (5 * fight_id + fighter_id) % 25,
        "takedowns_landed": td_landed,
        "takedowns_attempted": td_attempted,
        "submission_attempts": (fight_id * fighter_id) % 3,
        "control_time_seconds": 30 * ((fight_id + 2 * fighter_id) % 9),
        "knockdowns": 1 if (3 * fight_id + fighter_id) % 4 == 0 else 0,
    }


def _fight(
    fight_id: int,
    event_date: date,
    red_id: int,
    blue_id: int,
    winner_id: int | None,
    method: str,
    end_round: int = 3,
    end_time: str = "5:00",
    *,
    no_stats: bool = False,
    overrides: dict[str, int] | None = None,
) -> dict:
    row = {
        "fight_id": fight_id,
        "event_date": event_date,
        "fighter_red_id": red_id,
        "fighter_blue_id": blue_id,
        "winner_id": winner_id,
        "method": method,
        "end_round": end_round,
        "end_time": end_time,
        "is_title_fight": False,
        "scheduled_rounds": 3,
        "weight_class": "Lightweight",
    }
    for side, fighter_id in (("red", red_id), ("blue", blue_id)):
        birth, height, reach = FIGHTERS[fighter_id]
        row[f"{side}_birth_date"] = birth
        row[f"{side}_height_cm"] = height
        row[f"{side}_reach_cm"] = reach
        stats = _corner_stats(fight_id, fighter_id)
        for key in STAT_KEYS:
            row[f"{side}_{key}"] = None if no_stats else stats[key]
    row.update(overrides or {})
    return row


def _league() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(fights_df, rankings_df) shaped like load_base_dataframe / load_rankings.

    Covers every case the gates care about: pure debutant fights, a debutant
    against a veteran, a same-night tournament bout (strict cut), a draw (no
    target), a corner that never attempted a takedown, a corner whose only
    prior fight had zero significant strikes attempted, a fight without
    fight_stats (its corners later have prior fights but no summary), and plain
    veteran-vs-veteran fights, some with a missing physical attribute."""
    d = date
    fights = [
        _fight(1, d(2010, 1, 10), 1, 2, 1, "KO/TKO - Punches", 1, "2:31"),
        _fight(2, d(2010, 1, 10), 3, 4, 4, "SUB - Armbar", 2, "3:05"),
        # Same night (tournament final): fights 1 and 2 are NOT prior to it.
        _fight(3, d(2010, 1, 10), 1, 4, 1, "U-DEC"),
        _fight(4, d(2010, 6, 1), 2, 3, 3, "S-DEC"),
        _fight(5, d(2010, 6, 1), 5, 6, 5, "KO/TKO - Kick", 3, "1:10"),
        _fight(6, d(2011, 1, 15), 1, 5, 5, "SUB - Rear Naked Choke", 1, "4:44"),
        _fight(7, d(2011, 1, 15), 7, 2, 2, "U-DEC"),
        # Draw: no target, but it still counts as a prior fight for both.
        _fight(8, d(2011, 3, 1), 4, 6, None, "M-DEC"),
        # Fighter 7 never attempted a takedown: takedown_accuracy None.
        _fight(9, d(2011, 6, 1), 7, 3, 7, "KO/TKO - Elbows", 2, "0:45"),
        # Fighter 8 debuts without throwing a significant strike.
        _fight(
            10,
            d(2011, 6, 1),
            8,
            6,
            6,
            "SUB - Guillotine",
            1,
            "1:15",
            overrides={"red_sig_strikes_landed": 0, "red_sig_strikes_attempted": 0},
        ),
        # Fighter 8's only prior fight: sig_strike_accuracy None.
        _fight(11, d(2012, 1, 1), 8, 4, 4, "U-DEC"),
        # No fight_stats at all: 9 and 10 later have prior fights but no summary.
        _fight(12, d(2012, 1, 1), 9, 10, 9, "KO/TKO", 1, "3:00", no_stats=True),
        _fight(13, d(2012, 6, 1), 9, 1, 1, "U-DEC"),
        _fight(14, d(2012, 6, 1), 10, 5, 10, "SUB - Triangle", 2, "2:02"),
        _fight(15, d(2013, 1, 1), 3, 5, 3, "U-DEC"),
        _fight(16, d(2013, 1, 1), 2, 6, 6, "KO/TKO - Punches", 3, "4:10"),
        _fight(17, d(2013, 6, 1), 4, 1, 4, "S-DEC"),
        _fight(18, d(2013, 6, 1), 7, 8, 8, "U-DEC"),
        _fight(19, d(2014, 1, 1), 2, 3, 2, "SUB - Kimura", 2, "1:59"),
        _fight(20, d(2014, 1, 1), 11, 12, 12, "U-DEC"),
    ]
    rankings = pd.DataFrame(
        [
            {"fighter_id": 1, "division": "Lightweight", "rank_position": 5,
             "snapshot_date": d(2013, 1, 1)},
            {"fighter_id": 4, "division": "Lightweight", "rank_position": 3,
             "snapshot_date": d(2013, 1, 1)},
            {"fighter_id": 3, "division": "Lightweight", "rank_position": 8,
             "snapshot_date": d(2012, 12, 1)},
            {"fighter_id": 2, "division": "Lightweight", "rank_position": 11,
             "snapshot_date": d(2013, 12, 1)},
        ]
    )
    return pd.DataFrame(fights), rankings


def _csv_text(frame: pd.DataFrame) -> str:
    return frame.to_csv(index=False, lineterminator="\n")


def _golden_text(path: Path) -> str:
    # core.autocrlf may hand the fixture back with CRLF on Windows.
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _build(fights: pd.DataFrame, rankings: pd.DataFrame):
    # No ESPN rows and no fighter with known history: the gates do not depend on
    # the pre-UFC block (tests/test_training_csv_v2.py covers it).
    return build_training_dataset(
        fights, rankings, espn_by_fighter={}, known_fighter_ids=set()
    )


@pytest.fixture(scope="module")
def built():
    fights, rankings = _league()
    return _build(fights, rankings)


def _row(result, fight_id: int) -> pd.Series:
    by_id = result.dataset.set_index("fight_id")
    return by_id.loc[fight_id]


# ----------------------------------------------------------- gate 1: no summary


def test_debutant_fight_enters_with_nan_history_diffs(built):
    # Fight 1: both corners debut. Fight 7: fighter 7 debuts against a veteran.
    for fight_id in (1, 7):
        row = _row(built, fight_id)
        assert row[HISTORY_DIFFS].isna().all(), f"fight {fight_id}"
    both_new = _row(built, 1)
    assert both_new["ufc_prev_fights_red"] == 0
    assert both_new["ufc_prev_fights_blue"] == 0
    # Physical diffs are computed as always (fighter 2 has no height).
    assert both_new["reach_cm_diff"] == pytest.approx(185.0 - 182.0)
    assert both_new["age_diff"] == pytest.approx(
        ((date(2010, 1, 10) - date(1985, 3, 1)).days
         - (date(2010, 1, 10) - date(1986, 7, 9)).days) / 365.25,
        abs=1e-3,
    )
    assert pd.isna(both_new["height_cm_diff"])

    one_new = _row(built, 7)
    assert one_new["ufc_prev_fights_red"] == 0  # fighter 7, debut
    assert one_new["ufc_prev_fights_blue"] == 2  # fighter 2: fights 1 and 4
    assert one_new["target"] == 0


def test_same_night_tournament_bout_counts_no_prior_fight(built):
    # Fight 3 is the same night as fights 1 and 2: strict cut, both debut.
    row = _row(built, 3)
    assert row["ufc_prev_fights_red"] == 0
    assert row["ufc_prev_fights_blue"] == 0
    assert row[HISTORY_DIFFS].isna().all()


def test_prior_fights_without_stats_enter_with_their_ufc_count(built):
    # Fighter 9's only prior fight (12) has no fight_stats: compute_fighter_history
    # returns None, yet the corner is not a debutant.
    row = _row(built, 13)
    assert row[HISTORY_DIFFS].isna().all()
    assert row["ufc_prev_fights_red"] == 1
    assert row["ufc_prev_fights_blue"] == 3  # fighter 1: fights 1, 3 and 6


# ------------------------------------------------- gate 2: accuracy None -> NaN


def test_zero_takedown_attempts_row_kept_with_nan(built):
    # Fighter 7 never attempted a takedown: only takedown_accuracy_diff is NaN.
    row = _row(built, 9)
    assert pd.isna(row["takedown_accuracy_diff"])
    others = [c for c in FEATURE_COLUMNS if c not in {
        "takedown_accuracy_diff", "ranking_position_diff", "pct_wins_by_ko_diff",
        "avg_opponent_prior_win_rate_diff",
    }]
    assert row[others].notna().all(), row[others][row[others].isna()]
    assert row["ufc_prev_fights_red"] == 1
    assert row["ufc_prev_fights_blue"] == 2  # fighter 3: fights 2 and 4


def test_zero_sig_strike_attempts_row_kept_with_nan(built):
    # Fighter 8's only prior fight had 0 significant strikes attempted.
    row = _row(built, 11)
    assert pd.isna(row["sig_strike_accuracy_diff"])
    # The rest of the striking line still flows: 0 landed against fighter 4's
    # average over fights 2, 3 and 8 (the draw has stats too).
    blue_landed = [_corner_stats(k, 4)["sig_strikes_landed"] for k in (2, 3, 8)]
    assert row["sig_strikes_landed_per_fight_diff"] == pytest.approx(
        0.0 - sum(blue_landed) / 3
    )
    assert row["ufc_prev_fights_red"] == 1
    assert row["ufc_prev_fights_blue"] == 3


# ---------------------------------------------------------------- spot checks


def test_spot_checks_skip_rows_without_both_summaries(built):
    # The first fights of the league are debutants: a leak check on them compares
    # no date at all. The checks come from the first rows where BOTH corners have
    # a UFC summary (fight 9 counts: its takedown accuracy None is not a summary
    # missing), so the log shows real prior dates to compare.
    assert [check["fight_id"] for check in built.spot_checks] == [4, 6, 9]
    for check in built.spot_checks:
        assert check["red_latest_prior_fight_date"] is not None
        assert check["blue_latest_prior_fight_date"] is not None
        assert check["used_only_prior_data"] is True
    first = built.spot_checks[0]
    assert (first["red_prior_fights"], first["blue_prior_fights"]) == (1, 1)


def test_spot_checks_keep_reading_the_summary_when_it_exists():
    fights, rankings = _league()
    # Fights 1 and 2 seed the history (debutants: no check); fight 4 is checked.
    result = _build(fights[fights["fight_id"].isin([1, 2, 4])], rankings)
    assert [c["fight_id"] for c in result.spot_checks] == [4]
    check = result.spot_checks[0]
    assert check["red_prior_fights"] == 1
    assert check["blue_prior_fights"] == 1
    assert check["red_latest_prior_fight_date"] == "2010-01-10"
    assert check["used_only_prior_data"] is True


def _summary(total_prior_fights: int, latest: date) -> FighterHistorySummary:
    return FighterHistorySummary(
        total_prior_fights=total_prior_fights,
        total_rounds_fought=3 * total_prior_fights,
        sig_strikes_landed_per_fight=30.0,
        sig_strike_accuracy=0.5,
        knockdowns_per_fight=0.0,
        takedowns_landed_per_fight=1.0,
        takedown_accuracy=0.4,
        submission_attempts_per_fight=0.0,
        control_time_seconds_per_fight=60.0,
        win_streak=1,
        wins_last_5=1,
        pct_wins_by_ko=None,
        pct_wins_by_submission=None,
        pct_wins_by_decision=None,
        days_since_last_fight=100,
        ranking_position=None,
        sig_strikes_absorbed_per_fight=20.0,
        sig_strike_defense=0.6,
        takedowns_absorbed_per_fight=0.5,
        takedown_defense=0.7,
        avg_opponent_prior_win_rate=None,
        latest_prior_fight_date=latest,
    )


@pytest.mark.parametrize("same_day_side", ["red", "blue"])
def test_spot_check_flags_a_summary_dated_on_the_fight_day(same_day_side):
    """The leak flag: a summary whose latest prior fight is the fight's own day
    used data that is not prior. Asymmetric counts pin which corner is which."""
    event_date = date(2012, 6, 1)
    earlier = date(2012, 1, 1)
    red = _summary(2, event_date if same_day_side == "red" else earlier)
    blue = _summary(5, event_date if same_day_side == "blue" else earlier)

    check = training._spot_check(
        {"fight_id": 77, "event_date": event_date}, red, blue, 2, 5
    )

    assert check["red_prior_fights"] == 2
    assert check["blue_prior_fights"] == 5
    assert check["used_only_prior_data"] is False
    assert check[f"{same_day_side}_latest_prior_fight_date"] == "2012-06-01"
    other = "blue" if same_day_side == "red" else "red"
    assert check[f"{other}_latest_prior_fight_date"] == "2012-01-01"


# ------------------------------------------------------ rows that already entered


def test_previously_admitted_rows_identical(built):
    golden = pd.read_csv(WINNER_GOLDEN)
    admitted = built.dataset[built.dataset["fight_id"].isin(golden["fight_id"])]
    assert admitted["fight_id"].tolist() == golden["fight_id"].tolist()
    # Same 20 values, same target, same date: byte for byte against the CSV the
    # pre-phase-4 code wrote.
    assert _csv_text(admitted[LEGACY_CSV_COLUMNS]) == _golden_text(WINNER_GOLDEN)


def test_inclusion_counters_mirror_the_old_exclusions(built):
    golden = pd.read_csv(WINNER_GOLDEN)
    assert built.total_fights_seen == 20
    assert built.excluded_no_target == OLD_EXCLUDED_NO_TARGET
    # Nothing is excluded for history or stats any more...
    assert built.excluded_missing_history == 0
    assert built.excluded_missing_stats == 0
    # ...and each old exclusion now shows up as an inclusion, one for one.
    assert built.included_no_ufc_history == OLD_EXCLUDED_MISSING_HISTORY
    assert built.included_nan_stats == OLD_EXCLUDED_MISSING_STATS
    assert len(built.dataset) == len(golden) + (
        OLD_EXCLUDED_MISSING_HISTORY + OLD_EXCLUDED_MISSING_STATS
    )
    assert 8 not in built.dataset["fight_id"].tolist()  # the draw


# ------------------------------------------------------- the two new columns


def test_winner_csv_column_order_and_ufc_counts_never_nan(built):
    dataset = built.dataset
    # The CSV v2 order: the 20 legacy diffs and the UFC counts lead the 49.
    assert WINNER_FEATURE_COLUMNS[:22] == [*FEATURE_COLUMNS, *UFC_COUNT_COLUMNS]
    assert list(dataset.columns) == [
        "fight_id", "event_date", *WINNER_FEATURE_COLUMNS, "target"
    ]
    for column in UFC_COUNT_COLUMNS:
        assert dataset[column].notna().all()
        assert pd.api.types.is_integer_dtype(dataset[column])
        assert (dataset[column] >= 0).all()
    assert dataset["event_date"].tolist() == sorted(dataset["event_date"].tolist())


def test_count_prior_ufc_fights_strict_cutoff():
    fights, _ = _league()
    history = build_fighter_history_dataframe(fights)
    count = fighter_history.count_prior_ufc_fights

    # Fighter 1 fights twice on 2010-01-10, then on 2011-01-15 and 2012-06-01.
    assert count(history, 1, date(2010, 1, 10)) == 0  # same day: not prior
    assert count(history, 1, date(2010, 1, 11)) == 2
    assert count(history, 1, date(2011, 1, 15)) == 2
    assert count(history, 1, date(2011, 1, 16)) == 3
    # The draw (fight 8) and the fight without stats (12) still count.
    assert count(history, 6, date(2011, 6, 1)) == 2
    assert count(history, 9, date(2012, 6, 1)) == 1
    # A debutant, an unknown fighter and an empty history are an explicit 0.
    assert count(history, 11, date(2014, 1, 1)) == 0
    assert count(history, 999, date(2030, 1, 1)) == 0
    assert count(pd.DataFrame(), 1, date(2030, 1, 1)) == 0
    assert isinstance(count(history, 1, date(2030, 1, 1)), int)


def test_count_prior_ufc_fights_matches_total_prior_fights():
    fights, rankings = _league()
    history = build_fighter_history_dataframe(fights)
    cutoffs = sorted(set(fights["event_date"])) + [date(2030, 1, 1)]
    compared = 0
    for fighter_id in FIGHTERS:
        for cutoff in cutoffs:
            summary = compute_fighter_history(
                fighter_id, cutoff, history, rankings, "Lightweight"
            )
            if summary is None:
                continue
            assert fighter_history.count_prior_ufc_fights(
                history, fighter_id, cutoff
            ) == summary.total_prior_fights, (fighter_id, cutoff)
            compared += 1
    assert compared > 30  # the comparison really ran over the league


# ----------------------------------------------------------- method untouched


def test_method_dataset_unchanged():
    fights, rankings = _league()
    result = build_method_training_dataset(fights, rankings)
    # The method model keeps the OLD exclusion policy: debutants and accuracy
    # None stay out (the draw, fight 8, is a decision and enters).
    assert result.excluded_no_method == 0
    assert result.excluded_missing_history == OLD_EXCLUDED_MISSING_HISTORY
    assert result.excluded_missing_stats == OLD_EXCLUDED_MISSING_STATS
    assert 8 in result.dataset["fight_id"].tolist()
    assert not any(c in result.dataset.columns for c in UFC_COUNT_COLUMNS)
    assert _csv_text(result.dataset) == _golden_text(METHOD_GOLDEN)


def test_method_pipeline_does_not_import_the_winner_gates():
    import src.prediction.features.method_training as method_training

    assert not hasattr(method_training, "build_training_dataset")
    assert not hasattr(method_training, "count_prior_ufc_fights")


# ------------------------------------------------- downstream readers accept it


def test_output_main_writes_the_new_columns_and_logs_the_inclusions(
    monkeypatch, tmp_path, capsys
):
    fights, rankings = _league()

    class _Settings:
        database_url = "postgresql://do-not-use"

    monkeypatch.setattr(output, "get_settings", lambda: _Settings())
    monkeypatch.setattr(output, "load_base_dataframe", lambda _url: fights)
    monkeypatch.setattr(output, "load_rankings_dataframe", lambda _url: rankings)
    monkeypatch.setattr(
        output,
        "load_espn_inputs",
        lambda _url, _snapshot: (pd.DataFrame(columns=ESPN_HISTORY_COLUMNS), set()),
    )
    monkeypatch.setattr(output, "OUTPUT_CSV_PATH", tmp_path / "training_dataset.csv")
    monkeypatch.setattr(
        output, "create_output_table", lambda *a, **k: pytest.fail("touched the DB")
    )

    output.main()  # dataset_guard runs before writing

    written = pd.read_csv(tmp_path / "training_dataset.csv")
    assert list(written.columns) == [
        "fight_id", "event_date", *WINNER_FEATURE_COLUMNS, "target"
    ]
    assert written[UFC_COUNT_COLUMNS].notna().all().all()
    log = capsys.readouterr().out
    assert "'no_ufc_history': 10" in log
    assert "'nan_stats': 3" in log
    assert "'missing_history': 0" in log


def test_legacy_train_loader_ignores_the_extra_columns(monkeypatch, tmp_path, built):
    import src.prediction.train as train

    path = tmp_path / "training_dataset.csv"
    built.dataset.to_csv(path, index=False)
    monkeypatch.setattr(train, "DATASET_PATH", path)

    dataset = train.load_dataset()
    # The CSV really carries the extra columns and the debutant rows...
    assert set(UFC_COUNT_COLUMNS) <= set(dataset.columns)
    assert dataset["fight_id"].isin([1, 7, 9]).sum() == 3
    # ...and the legacy selection still reads only the 20 of the 27-jun bundle.
    columns = train.get_available_feature_columns(dataset)
    assert set(columns) <= set(FEATURE_COLUMNS)
    assert not set(columns) & set(UFC_COUNT_COLUMNS)
    prepared = train.prepare_features(dataset, dataset, columns)
    assert prepared.x_train.shape == (len(dataset), len(columns))
