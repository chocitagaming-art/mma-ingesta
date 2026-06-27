from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .classification import classify_win_method
from .metrics import safe_divide
from .types import FighterHistorySummary


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

    # Defensive signal (#25): a fighter "absorbs" exactly the opponent's landed
    # strikes/takedowns in each past bout (fight_stats stores both corners).
    # Aggregated over the same prior stats fights as the offensive metrics, so
    # the per-fight denominator (stats_fight_count) matches and stays leak-free.
    opp_sig_landed_total = stats_history["opp_sig_strikes_landed"].fillna(0).sum()
    opp_sig_attempted_total = stats_history["opp_sig_strikes_attempted"].fillna(0).sum()
    opp_td_landed_total = stats_history["opp_takedowns_landed"].fillna(0).sum()
    opp_td_attempted_total = stats_history["opp_takedowns_attempted"].fillna(0).sum()
    opp_sig_accuracy = safe_divide(opp_sig_landed_total, opp_sig_attempted_total)
    opp_td_accuracy = safe_divide(opp_td_landed_total, opp_td_attempted_total)
    # Striking/takedown defense = fraction of the opponents' attempts avoided.
    sig_strike_defense = None if opp_sig_accuracy is None else 1.0 - opp_sig_accuracy
    takedown_defense = None if opp_td_accuracy is None else 1.0 - opp_td_accuracy

    # Opponent quality / strength-of-schedule (#25): mean win-rate of the
    # fighter's PAST opponents, each measured as that opponent's career win-rate
    # *going into* the shared bout (opp_prior_win_rate, computed leak-free in
    # build_fighter_history_dataframe). NaN for debutant opponents is skipped by
    # Series.mean; None when every past opponent was a debutant.
    opp_win_rate_series = prior_history["opp_prior_win_rate"]
    avg_opponent_prior_win_rate = (
        float(opp_win_rate_series.mean()) if opp_win_rate_series.notna().any() else None
    )

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
        sig_strikes_absorbed_per_fight=float(opp_sig_landed_total / stats_fight_count),
        sig_strike_defense=sig_strike_defense,
        takedowns_absorbed_per_fight=float(opp_td_landed_total / stats_fight_count),
        takedown_defense=takedown_defense,
        avg_opponent_prior_win_rate=avg_opponent_prior_win_rate,
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
                # Opponent (blue) for this fighter's defensive metrics + SoS.
                "opponent_id": row["fighter_blue_id"],
                "opp_sig_strikes_landed": row["blue_sig_strikes_landed"],
                "opp_sig_strikes_attempted": row["blue_sig_strikes_attempted"],
                "opp_takedowns_landed": row["blue_takedowns_landed"],
                "opp_takedowns_attempted": row["blue_takedowns_attempted"],
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
                # Opponent (red) for this fighter's defensive metrics + SoS.
                "opponent_id": row["fighter_red_id"],
                "opp_sig_strikes_landed": row["red_sig_strikes_landed"],
                "opp_sig_strikes_attempted": row["red_sig_strikes_attempted"],
                "opp_takedowns_landed": row["red_takedowns_landed"],
                "opp_takedowns_attempted": row["red_takedowns_attempted"],
            }
        )
    history = pd.DataFrame.from_records(records)
    return _attach_opponent_prior_win_rate(history)


def _attach_opponent_prior_win_rate(history: pd.DataFrame) -> pd.DataFrame:
    """Add ``opp_prior_win_rate`` (leak-free strength-of-schedule signal).

    For every (fighter, bout) row we first compute the fighter's own career
    win-rate *going into* that bout (wins / fights, both counted strictly before
    the bout). Then, for each row, we look up the OPPONENT's prior win-rate for
    that same bout via a self-merge on (fight_id, opponent_id). Because the value
    only depends on each opponent's results before the shared bout - and that
    bout predates the prediction target - the resulting per-row signal carries no
    leakage when later averaged career-to-date in compute_fighter_history."""
    if history.empty:
        history["prior_win_rate"] = pd.Series(dtype="float64")
        history["opp_prior_win_rate"] = pd.Series(dtype="float64")
        return history
    history = history.sort_values(["fighter_id", "event_date", "fight_id"]).reset_index(drop=True)
    is_win = (history["result"] == "win").astype(int)
    grouped = is_win.groupby(history["fighter_id"])
    prior_wins = grouped.cumsum() - is_win
    prior_count = history.groupby("fighter_id").cumcount()
    history["prior_win_rate"] = np.where(prior_count > 0, prior_wins / prior_count.replace(0, np.nan), np.nan)
    opponent_lookup = history[["fight_id", "fighter_id", "prior_win_rate"]].rename(
        columns={"fighter_id": "opponent_id", "prior_win_rate": "opp_prior_win_rate"}
    )
    history = history.merge(opponent_lookup, on=["fight_id", "opponent_id"], how="left")
    return history
