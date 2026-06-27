from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from .types import DEFAULT_SCHEDULED_ROUNDS


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


def diff(red_value: Any, blue_value: Any) -> float | None:
    if red_value is None or blue_value is None:
        return None
    if pd.isna(red_value) or pd.isna(blue_value):
        return None
    return float(red_value) - float(blue_value)


def _coerce_scheduled_rounds(value: Any) -> int:
    """Normalise a fights.scheduled_rounds cell to a positive int.

    Falls back to the default when the value is missing/NaN/non-positive so a
    dirty row never poisons the feature."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return DEFAULT_SCHEDULED_ROUNDS
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULED_ROUNDS
    return rounds if rounds > 0 else DEFAULT_SCHEDULED_ROUNDS
