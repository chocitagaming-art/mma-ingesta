from datetime import date

from src.scrapers.rankings_backfill import _ts_to_date


def test_ts_to_date_parses_wayback_timestamp():
    assert _ts_to_date("20240602183510") == date(2024, 6, 2)
    assert _ts_to_date("20231003125440") == date(2023, 10, 3)
