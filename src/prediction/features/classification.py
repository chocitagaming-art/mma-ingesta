from __future__ import annotations

import pandas as pd


# MODEL FEATURE: the "ko" branch is load-bearing for the WINNER model
# (pct_wins_by_ko is its only method-derived feature). It must stay FIRST and
# byte-identical to the original substring check so no value can move in or out
# of the "ko" bucket. The submission/decision branches below only rescue the
# real ufcstats codes ("SUB - Armbar", "U-DEC"...) that used to fall through to
# None — they cannot steal anything the "ko" branch already caught. Parity is
# pinned by tests/test_method_features.py::test_ko_branch_parity_with_old_classifier.
def classify_win_method(method: str | None) -> str | None:
    # NULL methods arrive from the DB as float NaN (e.g. upcoming fights with no
    # result). `not NaN` is False, so guard on type to avoid AttributeError.
    if not isinstance(method, str) or not method:
        return None
    lowered = method.lower()
    if "ko" in lowered or "tko" in lowered:
        return "ko"
    # ufcstats codes ("SUB", "SUB - Armbar") + ESPN provisional ("Submission").
    if lowered.startswith("sub") or "submission" in lowered:
        return "submission"
    # ufcstats scorecards (U-DEC/S-DEC/M-DEC) + ESPN provisional ("Decision").
    if lowered.startswith(("u-dec", "s-dec", "m-dec")) or "decision" in lowered:
        return "decision"
    # CNC / DQ / Overturned / Other: not a clean KO-sub-decision outcome.
    return None


def classify_target(row: pd.Series) -> int | None:
    if pd.isna(row["winner_id"]):
        return None
    if row["winner_id"] == row["fighter_red_id"]:
        return 1
    if row["winner_id"] == row["fighter_blue_id"]:
        return 0
    return None
