from __future__ import annotations

import pandas as pd


# MODEL FEATURE: DO NOT MODIFY
def classify_win_method(method: str | None) -> str | None:
    # NULL methods arrive from the DB as float NaN (e.g. upcoming fights with no
    # result). `not NaN` is False, so guard on type to avoid AttributeError.
    if not isinstance(method, str) or not method:
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
