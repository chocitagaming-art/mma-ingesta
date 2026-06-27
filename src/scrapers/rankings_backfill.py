"""Backfill historical UFC rankings snapshots from Wayback Machine into the
``rankings`` table (#12).

The model's ``ranking_position_diff`` feature is 100% NaN in training because the
``rankings`` table only holds recent snapshots (all *after* the training/test
fights), so ``lookup_ranking_position`` never finds a snapshot dated before a past
fight. Wayback has ~monthly captures of ufc.com/rankings going back years; this
reads them and inserts each as a dated snapshot.

Only the HTML *source* (web.archive.org) and the *snapshot date* (from the capture
timestamp) differ from the live scraper: the page structure is identical, so we
reuse rankings.py's parser, name matcher and the rankings repository verbatim.

Usage::

    python -m src.scrapers.rankings_backfill --from 2023 --to 2025 --dry-run
    python -m src.scrapers.rankings_backfill --from 2023 --to 2025
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter
from datetime import date

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .rankings import BROWSER_HEADERS, _build_records, _parse_rankings
from .repositories.fighters import get_all_fighters
from .repositories.rankings import (
    delete_rankings_for_snapshot,
    ensure_ufc_promotion,
    insert_ranking,
)

LOGGER = logging.getLogger(__name__)

WAYBACK_TARGET = "https://www.ufc.com/rankings"
CDX_URL = "http://web.archive.org/cdx/search/cdx"


def _ts_to_date(timestamp: str) -> date:
    """Wayback 14-digit timestamp 'YYYYMMDDhhmmss' -> the capture date."""
    return date(int(timestamp[0:4]), int(timestamp[4:6]), int(timestamp[6:8]))


def monthly_snapshots(
    from_year: int, to_year: int, *, timeout: int = 90, retries: int = 4
) -> list[str]:
    """~One capture timestamp per month for ufc.com/rankings (CDX, collapse:6)."""
    params = {
        "url": "ufc.com/rankings",
        "output": "json",
        "from": f"{from_year}0101",
        "to": f"{to_year}1231",
        "collapse": "timestamp:6",
        "filter": "statuscode:200",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(CDX_URL, params=params, timeout=timeout)
            if response.status_code == 200:
                rows = response.json()
                return [row[1] for row in rows[1:]]  # row 0 is the column header
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except requests.RequestException as exc:  # noqa: PERF203
            last_error = exc
        time.sleep(3 * (attempt + 1))  # CDX is flaky / rate-limited
    raise RuntimeError(f"CDX query failed after {retries} attempts: {last_error}")


def fetch_wayback(timestamp: str, *, timeout: int = 30, retries: int = 4) -> str:
    # 'id_' = the raw archived page (no Wayback banner / link rewriting).
    url = f"http://web.archive.org/web/{timestamp}id_/{WAYBACK_TARGET}"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
            if response.status_code == 200:
                response.encoding = "utf-8"  # captures carry no charset header
                return response.text
            last_error = RuntimeError(f"HTTP {response.status_code}")
        except requests.RequestException as exc:  # noqa: PERF203
            last_error = exc
        time.sleep(2**attempt)  # backoff for Wayback's rate limiting
    raise RuntimeError(f"Wayback fetch failed for {timestamp}: {last_error}")


def run(from_year: int, to_year: int, *, dry_run: bool = False, delay: float = 2.0) -> dict:
    settings = get_settings()
    timestamps = monthly_snapshots(from_year, to_year)
    LOGGER.info(
        "CDX returned %s monthly snapshots for %s-%s", len(timestamps), from_year, to_year
    )

    totals: Counter = Counter()
    with connect(settings.database_url) as connection:
        promotion_id = ensure_ufc_promotion(connection)
        connection.commit()
        fighters = get_all_fighters(connection)

        for timestamp in timestamps:
            snapshot = _ts_to_date(timestamp)
            try:
                html = fetch_wayback(timestamp)
            except RuntimeError as exc:
                LOGGER.warning("skip %s: %s", snapshot, exc)
                totals["fetch_failed"] += 1
                continue

            counts: Counter = Counter()
            divisions = _parse_rankings(html, counts)
            records = _build_records(divisions, promotion_id, snapshot, fighters, counts)
            matched = counts["matched"] + counts["matched_folded"]
            print(
                f"{snapshot}: {len(records)} rows, matched={matched}, "
                f"unmatched={counts['unmatched']}, divisions={counts['divisions']}"
            )
            totals["snapshots"] += 1
            totals["rows"] += len(records)
            totals["matched"] += matched
            totals["unmatched"] += counts["unmatched"]

            if not dry_run and records:
                delete_rankings_for_snapshot(connection, promotion_id, snapshot)
                for record in records:
                    insert_ranking(connection, record)
                connection.commit()

            time.sleep(delay)  # be a good Wayback citizen

    print(
        f"\nTOTAL: {totals['snapshots']} snapshots, {totals['rows']} rows, "
        f"matched={totals['matched']}, unmatched={totals['unmatched']}, "
        f"fetch_failed={totals['fetch_failed']}"
    )
    return dict(totals)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_year", type=int, default=2023)
    parser.add_argument("--to", dest="to_year", type=int, default=2025)
    parser.add_argument("--dry-run", action="store_true", help="parse + match, write nothing")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between Wayback fetches")
    args = parser.parse_args()
    run(args.from_year, args.to_year, dry_run=args.dry_run, delay=args.delay)


if __name__ == "__main__":
    main()
