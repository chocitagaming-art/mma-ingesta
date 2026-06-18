from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from src.scrapers.config import get_settings
from src.scrapers.db import connect, cursor


load_dotenv()

OUTPUT_CSV_PATH = Path("training_dataset.csv")
OUTPUT_TABLE_NAME = "fight_prediction_training_data"
SPOT_CHECK_COUNT = 3


@dataclass(frozen=True)
class FighterHistorySummary:
    total_prior_fights: int
    total_rounds_fought: int
    sig_strikes_landed_per_fight: float | None
    sig_strike_accuracy: float | None
    knockdowns_per_fight: float | None
    takedowns_landed_per_fight: float | None
    takedown_accuracy: float | None
    submission_attempts_per_fight: float | None
    control_time_seconds_per_fight: float | None
    win_streak: int
    wins_last_5: int
    pct_wins_by_ko: float | None
    pct_wins_by_submission: float | None
    pct_wins_by_decision: float | None
    days_since_last_fight: int | None
    ranking_position: int | None
    latest_prior_fight_date: date | None


@dataclass(frozen=True)
class DatasetBuildResult:
    dataset: pd.DataFrame
    spot_checks: list[dict[str, Any]]
    total_fights_seen: int
    excluded_no_target: int
    excluded_missing_history: int
    excluded_missing_stats: int


FEATURE_COLUMNS = [
    "height_cm_diff",
    "reach_cm_diff",
    "age_diff",
    "sig_strikes_landed_per_fight_diff",
    "sig_strike_accuracy_diff",
    "knockdowns_per_fight_diff",
    "takedowns_landed_per_fight_diff",
    "takedown_accuracy_diff",
    "submission_attempts_per_fight_diff",
    "control_time_seconds_per_fight_diff",
    "win_streak_diff",
    "wins_last_5_diff",
    "total_prior_fights_diff",
    "total_rounds_fought_diff",
    "pct_wins_by_ko_diff",
    "pct_wins_by_submission_diff",
    "pct_wins_by_decision_diff",
    "scheduled_rounds",
    "days_since_last_fight_diff",
    "ranking_position_diff",
]


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


def classify_win_method(method: str | None) -> str | None:
    if not method:
        return None
    lowered = method.lower()
    if "ko" in lowered or "tko" in lowered:
        return "ko"
    if "submission" in lowered:
        return "submission"
    if "decision" in lowered:
        return "decision"
    return None


def classify_target(row: pd.Series) -> int | None:
    if pd.isna(row["winner_id"]):
        return None
    if row["winner_id"] == row["fighter_red_id"]:
        return 1
    if row["winner_id"] == row["fighter_blue_id"]:
        return 0
    return None


def compute_age(birth_date: date | None, event_date: date) -> float | None:
    if birth_date is None or pd.isna(birth_date):
        return None
    return round((event_date - birth_date).days / 365.25, 4)


def safe_divide(numerator: float | int, denominator: float | int) -> float | None:
    if denominator in (0, None) or pd.isna(denominator):
        return None
    if numerator is None or pd.isna(numerator):
        return None
    return float(numerator) / float(denominator)


def compute_fighter_history(
    fighter_id: int,
    current_event_date: date,
    history_df: pd.DataFrame,
    rankings_df: pd.DataFrame,
    weight_class: str | None,
) -> FighterHistorySummary | None:
    prior_history = history_df[
        (history_df["fighter_id"] == fighter_id)
        & (history_df["event_date"] < current_event_date)
    ].sort_values(["event_date", "fight_id"], ascending=[True, True])
    if prior_history.empty:
        return None
    stats_history = prior_history[
        prior_history[
            [
                "sig_strikes_landed",
                "sig_strikes_attempted",
                "takedowns_landed",
                "takedowns_attempted",
                "submission_attempts",
                "control_time_seconds",
                "knockdowns",
            ]
        ].notna().any(axis=1)
    ]
    if stats_history.empty:
        return None

    total_prior_fights = int(len(prior_history))
    total_rounds_fought = int(prior_history["rounds_fought"].fillna(0).sum())
    stats_fight_count = int(len(stats_history))
    sig_landed_total = stats_history["sig_strikes_landed"].fillna(0).sum()
    sig_attempted_total = stats_history["sig_strikes_attempted"].fillna(0).sum()
    knockdowns_total = stats_history["knockdowns"].fillna(0).sum()
    takedowns_landed_total = stats_history["takedowns_landed"].fillna(0).sum()
    takedowns_attempted_total = stats_history["takedowns_attempted"].fillna(0).sum()
    submission_attempts_total = stats_history["submission_attempts"].fillna(0).sum()
    control_time_total = stats_history["control_time_seconds"].fillna(0).sum()

    results = prior_history["result"].tolist()
    win_streak = 0
    for result in reversed(results):
        if result == "win":
            win_streak += 1
            continue
        break
    wins_last_5 = int(sum(1 for result in results[-5:] if result == "win"))

    prior_wins = prior_history[prior_history["result"] == "win"]
    ko_wins = int((prior_wins["win_method"] == "ko").sum())
    submission_wins = int((prior_wins["win_method"] == "submission").sum())
    decision_wins = int((prior_wins["win_method"] == "decision").sum())
    total_prior_wins = int(len(prior_wins))

    latest_prior_fight_date = prior_history["event_date"].max()
    days_since_last_fight = None
    if latest_prior_fight_date is not None and not pd.isna(latest_prior_fight_date):
        days_since_last_fight = int((current_event_date - latest_prior_fight_date).days)

    ranking_position = lookup_ranking_position(
        fighter_id=fighter_id,
        current_event_date=current_event_date,
        rankings_df=rankings_df,
        weight_class=weight_class,
    )

    return FighterHistorySummary(
        total_prior_fights=total_prior_fights,
        total_rounds_fought=total_rounds_fought,
        sig_strikes_landed_per_fight=float(sig_landed_total / stats_fight_count),
        sig_strike_accuracy=safe_divide(sig_landed_total, sig_attempted_total),
        knockdowns_per_fight=float(knockdowns_total / stats_fight_count),
        takedowns_landed_per_fight=float(takedowns_landed_total / stats_fight_count),
        takedown_accuracy=safe_divide(takedowns_landed_total, takedowns_attempted_total),
        submission_attempts_per_fight=float(submission_attempts_total / stats_fight_count),
        control_time_seconds_per_fight=float(control_time_total / stats_fight_count),
        win_streak=win_streak,
        wins_last_5=wins_last_5,
        pct_wins_by_ko=safe_divide(ko_wins, total_prior_wins),
        pct_wins_by_submission=safe_divide(submission_wins, total_prior_wins),
        pct_wins_by_decision=safe_divide(decision_wins, total_prior_wins),
        days_since_last_fight=days_since_last_fight,
        ranking_position=ranking_position,
        latest_prior_fight_date=latest_prior_fight_date,
    )


def lookup_ranking_position(
    fighter_id: int,
    current_event_date: date,
    rankings_df: pd.DataFrame,
    weight_class: str | None,
) -> int | None:
    if rankings_df.empty:
        return None
    fighter_rankings = rankings_df[
        (rankings_df["fighter_id"] == fighter_id)
        & (rankings_df["snapshot_date"] < current_event_date)
    ]
    if fighter_rankings.empty:
        return None
    if weight_class:
        division_matches = fighter_rankings[
            fighter_rankings["division"].fillna("").str.lower() == weight_class.lower()
        ]
        if not division_matches.empty:
            fighter_rankings = division_matches
    latest_snapshot_date = fighter_rankings["snapshot_date"].max()
    latest_rows = fighter_rankings[fighter_rankings["snapshot_date"] == latest_snapshot_date]
    if latest_rows.empty:
        return None
    return int(latest_rows.sort_values("rank_position").iloc[0]["rank_position"])


def build_fighter_history_dataframe(fights_df: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in fights_df.to_dict("records"):
        win_method = classify_win_method(row["method"])
        red_result = "win" if row["winner_id"] == row["fighter_red_id"] else "loss" if row["winner_id"] == row["fighter_blue_id"] else "other"
        blue_result = "win" if row["winner_id"] == row["fighter_blue_id"] else "loss" if row["winner_id"] == row["fighter_red_id"] else "other"
        records.append(
            {
                "fight_id": row["fight_id"],
                "event_date": row["event_date"],
                "fighter_id": row["fighter_red_id"],
                "result": red_result,
                "win_method": win_method if red_result == "win" else None,
                "rounds_fought": row["end_round"],
                "sig_strikes_landed": row["red_sig_strikes_landed"],
                "sig_strikes_attempted": row["red_sig_strikes_attempted"],
                "takedowns_landed": row["red_takedowns_landed"],
                "takedowns_attempted": row["red_takedowns_attempted"],
                "submission_attempts": row["red_submission_attempts"],
                "control_time_seconds": row["red_control_time_seconds"],
                "knockdowns": row["red_knockdowns"],
            }
        )
        records.append(
            {
                "fight_id": row["fight_id"],
                "event_date": row["event_date"],
                "fighter_id": row["fighter_blue_id"],
                "result": blue_result,
                "win_method": win_method if blue_result == "win" else None,
                "rounds_fought": row["end_round"],
                "sig_strikes_landed": row["blue_sig_strikes_landed"],
                "sig_strikes_attempted": row["blue_sig_strikes_attempted"],
                "takedowns_landed": row["blue_takedowns_landed"],
                "takedowns_attempted": row["blue_takedowns_attempted"],
                "submission_attempts": row["blue_submission_attempts"],
                "control_time_seconds": row["blue_control_time_seconds"],
                "knockdowns": row["blue_knockdowns"],
            }
        )
    return pd.DataFrame.from_records(records)


def build_training_dataset(fights_df: pd.DataFrame, rankings_df: pd.DataFrame) -> DatasetBuildResult:
    history_df = build_fighter_history_dataframe(fights_df)
    dataset_rows: list[dict[str, Any]] = []
    spot_checks: list[dict[str, Any]] = []
    excluded_no_target = 0
    excluded_missing_history = 0
    excluded_missing_stats = 0

    for row in fights_df.to_dict("records"):
        target = classify_target(pd.Series(row))
        if target is None:
            excluded_no_target += 1
            continue
        red_history = compute_fighter_history(
            fighter_id=row["fighter_red_id"],
            current_event_date=row["event_date"],
            history_df=history_df,
            rankings_df=rankings_df,
            weight_class=row["weight_class"],
        )
        blue_history = compute_fighter_history(
            fighter_id=row["fighter_blue_id"],
            current_event_date=row["event_date"],
            history_df=history_df,
            rankings_df=rankings_df,
            weight_class=row["weight_class"],
        )
        if red_history is None or blue_history is None:
            excluded_missing_history += 1
            continue
        if any(
            value is None
            for value in (
                red_history.sig_strikes_landed_per_fight,
                red_history.sig_strike_accuracy,
                red_history.knockdowns_per_fight,
                red_history.takedowns_landed_per_fight,
                red_history.takedown_accuracy,
                red_history.submission_attempts_per_fight,
                red_history.control_time_seconds_per_fight,
                blue_history.sig_strikes_landed_per_fight,
                blue_history.sig_strike_accuracy,
                blue_history.knockdowns_per_fight,
                blue_history.takedowns_landed_per_fight,
                blue_history.takedown_accuracy,
                blue_history.submission_attempts_per_fight,
                blue_history.control_time_seconds_per_fight,
            )
        ):
            excluded_missing_stats += 1
            continue

        red_age = compute_age(row["red_birth_date"], row["event_date"])
        blue_age = compute_age(row["blue_birth_date"], row["event_date"])

        base_row = {
            "fight_id": row["fight_id"],
            "event_date": row["event_date"],
            "height_cm_diff": diff(row["red_height_cm"], row["blue_height_cm"]),
            "reach_cm_diff": diff(row["red_reach_cm"], row["blue_reach_cm"]),
            "age_diff": diff(red_age, blue_age),
            "sig_strikes_landed_per_fight_diff": diff(
                red_history.sig_strikes_landed_per_fight,
                blue_history.sig_strikes_landed_per_fight,
            ),
            "sig_strike_accuracy_diff": diff(
                red_history.sig_strike_accuracy,
                blue_history.sig_strike_accuracy,
            ),
            "knockdowns_per_fight_diff": diff(
                red_history.knockdowns_per_fight,
                blue_history.knockdowns_per_fight,
            ),
            "takedowns_landed_per_fight_diff": diff(
                red_history.takedowns_landed_per_fight,
                blue_history.takedowns_landed_per_fight,
            ),
            "takedown_accuracy_diff": diff(
                red_history.takedown_accuracy,
                blue_history.takedown_accuracy,
            ),
            "submission_attempts_per_fight_diff": diff(
                red_history.submission_attempts_per_fight,
                blue_history.submission_attempts_per_fight,
            ),
            "control_time_seconds_per_fight_diff": diff(
                red_history.control_time_seconds_per_fight,
                blue_history.control_time_seconds_per_fight,
            ),
            "win_streak_diff": diff(red_history.win_streak, blue_history.win_streak),
            "wins_last_5_diff": diff(red_history.wins_last_5, blue_history.wins_last_5),
            "total_prior_fights_diff": diff(
                red_history.total_prior_fights,
                blue_history.total_prior_fights,
            ),
            "total_rounds_fought_diff": diff(
                red_history.total_rounds_fought,
                blue_history.total_rounds_fought,
            ),
            "pct_wins_by_ko_diff": diff(
                red_history.pct_wins_by_ko,
                blue_history.pct_wins_by_ko,
            ),
            "pct_wins_by_submission_diff": diff(
                red_history.pct_wins_by_submission,
                blue_history.pct_wins_by_submission,
            ),
            "pct_wins_by_decision_diff": diff(
                red_history.pct_wins_by_decision,
                blue_history.pct_wins_by_decision,
            ),
            "scheduled_rounds": row["scheduled_rounds"],
            "days_since_last_fight_diff": diff(
                red_history.days_since_last_fight,
                blue_history.days_since_last_fight,
            ),
            "ranking_position_diff": diff(
                red_history.ranking_position,
                blue_history.ranking_position,
            ),
        }
        # Raw red-blue diffs (NOT oriented by target). Orienting by target
        # canonicalizes every row to winner-loser diffs, which makes the label
        # unlearnable and mismatches inference (api.py uses raw red-blue diffs).
        dataset_rows.append(
            {
                **base_row,
                "target": target,
            }
        )

        if len(spot_checks) < SPOT_CHECK_COUNT:
            spot_checks.append(
                {
                    "fight_id": row["fight_id"],
                    "event_date": row["event_date"].isoformat(),
                    "red_prior_fights": red_history.total_prior_fights,
                    "blue_prior_fights": blue_history.total_prior_fights,
                    "red_latest_prior_fight_date": red_history.latest_prior_fight_date.isoformat()
                    if red_history.latest_prior_fight_date
                    else None,
                    "blue_latest_prior_fight_date": blue_history.latest_prior_fight_date.isoformat()
                    if blue_history.latest_prior_fight_date
                    else None,
                    "used_only_prior_data": (
                        (red_history.latest_prior_fight_date is None or red_history.latest_prior_fight_date < row["event_date"])
                        and (blue_history.latest_prior_fight_date is None or blue_history.latest_prior_fight_date < row["event_date"])
                    ),
                }
            )

    dataset = pd.DataFrame.from_records(dataset_rows)
    if dataset.empty:
        return DatasetBuildResult(
            dataset=dataset,
            spot_checks=spot_checks,
            total_fights_seen=len(fights_df),
            excluded_no_target=excluded_no_target,
            excluded_missing_history=excluded_missing_history,
            excluded_missing_stats=excluded_missing_stats,
        )
    dataset["event_date"] = pd.to_datetime(dataset["event_date"]).dt.date
    return DatasetBuildResult(
        dataset=dataset.sort_values(["event_date", "fight_id"]).reset_index(drop=True),
        spot_checks=spot_checks,
        total_fights_seen=len(fights_df),
        excluded_no_target=excluded_no_target,
        excluded_missing_history=excluded_missing_history,
        excluded_missing_stats=excluded_missing_stats,
    )


def diff(red_value: Any, blue_value: Any) -> float | None:
    if red_value is None or blue_value is None:
        return None
    if pd.isna(red_value) or pd.isna(blue_value):
        return None
    return float(red_value) - float(blue_value)


def create_output_table(database_url: str, dataset: pd.DataFrame) -> None:
    column_definitions = []
    for column in dataset.columns:
        if column == "fight_id":
            column_definitions.append(f"{column} INTEGER")
        elif column == "event_date":
            column_definitions.append(f"{column} DATE NOT NULL")
        elif column == "target":
            column_definitions.append(f"{column} INTEGER NOT NULL")
        else:
            column_definitions.append(f"{column} DOUBLE PRECISION")
    create_table_sql = f"""
        DROP TABLE IF EXISTS {OUTPUT_TABLE_NAME};
        CREATE TABLE {OUTPUT_TABLE_NAME} (
            {", ".join(column_definitions)}
        );
    """
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(create_table_sql)
            insert_columns = list(dataset.columns)
            placeholders = ", ".join(["%s"] * len(insert_columns))
            insert_sql = f"""
                INSERT INTO {OUTPUT_TABLE_NAME} ({", ".join(insert_columns)})
                VALUES ({placeholders})
            """
            rows = []
            for record in dataset.replace({np.nan: None}).to_dict("records"):
                rows.append(tuple(record[column] for column in insert_columns))
            cursor.executemany(insert_sql, rows)
        connection.commit()


def print_summary(result: DatasetBuildResult) -> None:
    dataset = result.dataset
    feature_columns = [column for column in dataset.columns if column not in {"fight_id", "event_date", "target"}]
    class_balance = (
        dataset["target"].value_counts(normalize=True).sort_index().to_dict()
        if "target" in dataset.columns
        else {}
    )
    print(f"Total fights seen: {result.total_fights_seen}")
    print(f"Total samples: {len(dataset)}")
    print(f"Feature count: {len(feature_columns)}")
    print(f"Class balance: {class_balance}")
    print(
        "Exclusions:",
        {
            "no_target": result.excluded_no_target,
            "missing_history": result.excluded_missing_history,
            "missing_stats": result.excluded_missing_stats,
        },
    )
    print("Spot checks:")
    for spot_check in result.spot_checks:
        print(spot_check)


def main() -> None:
    settings = get_settings()
    fights_df = load_base_dataframe(settings.database_url)
    rankings_df = load_rankings_dataframe(settings.database_url)
    result = build_training_dataset(fights_df, rankings_df)
    dataset = result.dataset
    if dataset.empty:
        print_summary(result)
        raise RuntimeError("No eligible training samples were generated.")
    dataset.to_csv(OUTPUT_CSV_PATH, index=False)
    create_output_table(settings.database_url, dataset)
    print_summary(result)




if __name__ == "__main__":
    main()