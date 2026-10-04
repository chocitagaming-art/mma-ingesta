"""Pre-UFC block of the WINNER model (phase 4, Contender Series).

Nine variables per corner built from fight_history_espn (regional promotions,
Bellator and, since mma-ingesta 17cdd3d, the Contender Series as league '3321').
They are the winner model's only; the method model never sees them (see the
phase-4 block of types.py).

espn_features_for_fighter is a verbatim port of
docs/experiments/preufc-dwcs-2026-09-30/build_espn_block.py:82-140, the arm that won
the 30-sep experiment (con_dwcs: every row, no league filter). Do NOT "improve" it:
any change in the cut, the order, the win rule or the method search breaks parity
with the measured experiment. Its quirks are on purpose:
  - the cut is STRICT (event_date < date of the bout): a fight the same day cannot
    inform that day's prediction, and a NULL event_date never counts;
  - same-day rows are ordered stably by (event_date, id): the streak and the days
    of inactivity depended on row order before (~670 fighter-days with 2+ rows);
  - a win is a result that starts with 'W'; loss, draw, nc and NULL count in the
    denominator and break the streak;
  - ko_rate and sub_rate are KO/SUB WINS over ALL prior fights (not over wins),
    found by substring in the upper-cased method ('Technical Submission' counts,
    the misspelt 'Sumission' does not);
  - no prior rows: has_history, prev_fights and title_fights are 0 and the other
    six are None ("no history" -> "don't know" for the rates, not a zero).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date
from typing import Any

import pandas as pd

from .metrics import diff
from .types import CORNER_SIDES, PREUFC_BASES, PREUFC_DIFF_COLUMNS

# Columns of fight_history_espn the block reads (db.load_espn_history_dataframe).
# league_id is not used by the function: it rides along so a later audit can tell
# the DWCS rows apart without reloading.
ESPN_HISTORY_COLUMNS = [
    "id",
    "fighter_id",
    "event_date",
    "result",
    "method",
    "is_title_fight",
    "league_id",
]

# History of a fighter with no rows at all (known to ESPN, nothing scraped).
_EMPTY_HISTORY = pd.DataFrame(columns=ESPN_HISTORY_COLUMNS)


def _is_win(result: Any) -> bool:
    return isinstance(result, str) and result.strip().upper().startswith("W")


def espn_features_for_fighter(
    history: pd.DataFrame, fighter_id: int, corte: date
) -> dict[str, Any]:
    """The nine pre-UFC variables of a fighter using ONLY fights before `corte`.

    `corte` keeps the experiment's parameter name so its tests (which pass it by
    keyword) run unchanged here. `history` may hold every fighter: the function
    filters by fighter_id itself, exactly as the experiment did.
    """
    if history.empty:
        prior = history
    else:
        order = ["event_date", "id"] if "id" in history.columns else ["event_date"]
        prior = history[
            (history["fighter_id"] == fighter_id)
            & (pd.to_datetime(history["event_date"]).dt.date < corte)
        ].sort_values(order, kind="stable")

    n = len(prior)
    if n == 0:
        return {
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

    wins = prior["result"].apply(_is_win)
    methods = prior["method"].fillna("").str.upper()
    dates = pd.to_datetime(prior["event_date"]).dt.date

    streak = 0
    for won in reversed(list(wins)):
        if won:
            streak += 1
        else:
            break

    return {
        "espn_has_history": 1,
        "espn_prev_fights": n,
        "espn_win_rate": float(wins.mean()),
        "espn_ko_rate": float((wins & methods.str.contains("KO")).sum() / n),
        "espn_sub_rate": float((wins & methods.str.contains("SUB")).sum() / n),
        "espn_streak": streak,
        "espn_days_since_last": (corte - dates.iloc[-1]).days,
        "espn_years_pro": round((corte - dates.iloc[0]).days / 365.25, 2),
        "espn_title_fights": int(prior["is_title_fight"].fillna(False).sum()),
    }


def build_preufc_block(
    red_id: int,
    blue_id: int,
    cutoff: date,
    espn_by_fighter: Mapping[int, pd.DataFrame],
    known_fighter_ids: Collection[int],
) -> dict[str, Any]:
    """The 18 PREUFC_COLUMNS of a bout ({base}_red, {base}_blue per base, in order).

    `espn_by_fighter` is db.index_espn_history(...): a fighter missing from it has no
    rows. `cutoff` is the date of the bout (training) or of the matchup (serving).

    UNKNOWN HISTORY (deliberate deviation from the experiment, which had no such
    rule): a fighter NOT in `known_fighter_ids` (db.load_espn_known_fighter_ids: an
    espn_id and a finished sweep) gets None in all nine variables of his corner,
    has_history included. The experiment gave him has_history=0, the same as a
    swept fighter with no prior fights. Measured by the plan's critique: in train,
    debutants with no ESPN row won 35.8 % against 46.0 % for those with history, so
    a short-notice debutant the Tuesday cron has not swept yet would be punished at
    serving time. NaN says "don't know" instead. The same rule runs when training
    and when serving, so both see the same values (parity).
    """
    corners: dict[str, dict[str, Any]] = {}
    for side, fighter_id in zip(CORNER_SIDES, (red_id, blue_id)):
        if fighter_id in known_fighter_ids:
            history = espn_by_fighter.get(fighter_id, _EMPTY_HISTORY)
            corners[side] = espn_features_for_fighter(history, fighter_id, cutoff)
        else:
            corners[side] = dict.fromkeys(PREUFC_BASES)
    return {
        f"{base}_{side}": corners[side][base]
        for base in PREUFC_BASES
        for side in CORNER_SIDES
    }


def preufc_diff_values(block: Mapping[str, Any]) -> dict[str, float | None]:
    """The nine PREUFC_DIFF_COLUMNS (arm C, informative): red minus blue of each
    base, None when either corner is missing (same policy as metrics.diff)."""
    return {
        column: diff(block[f"{base}_red"], block[f"{base}_blue"])
        for base, column in zip(PREUFC_BASES, PREUFC_DIFF_COLUMNS)
    }
