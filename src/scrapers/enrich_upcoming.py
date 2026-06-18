"""ESPN enrichment scoped to fighters in UPCOMING events.

Only the ~176 ranked fighters were ever enriched, so fighters who appear only in
upcoming-event cards (e.g. Ion Cutelaba, Navajo Stirling) render as initials with
no photo / flag / physicals. This reuses the exact machinery of enrich_ranked
(ESPN search -> athlete detail -> COALESCE update) but over the upcoming-event
gap set, filling ONLY the NULL columns (existing data is never overwritten).

Usage:
    python -m src.scrapers.enrich_upcoming --dry-run   # report coverage, no writes
    python -m src.scrapers.enrich_upcoming             # enrich (writes to DB)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter

from .config import get_settings
from .db import connect
from .enrich_ranked import REQUEST_DELAY_SECONDS, _build_session, _resolve_athlete
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_enrichment

LOGGER = logging.getLogger(__name__)


def _get_upcoming_gap_fighters(connection) -> list[tuple[int, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f
              ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (
                  f.headshot_url IS NULL
                  OR f.nationality IS NULL
                  OR f.height_cm IS NULL
                  OR f.reach_cm IS NULL
              )
            ORDER BY f.name
            """
        )
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def enrich_upcoming_fighters(dry_run: bool = False) -> Counter:
    settings = get_settings()
    session = _build_session(settings)
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        gaps = _get_upcoming_gap_fighters(connection)
        counts["gap_fighters"] = len(gaps)
        LOGGER.info("Upcoming-event gap fighters: %d", len(gaps))
        for fighter_id, name in gaps:
            try:
                athlete = _resolve_athlete(session, name)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:  # noqa: BLE001 - network/parse issues are non-fatal
                counts["errors"] += 1
                LOGGER.warning("Lookup failed for %r: %s", name, exc)
                continue
            if athlete is None:
                counts["unresolved"] += 1
                LOGGER.info("No ESPN match for %r", name)
                continue
            counts["resolved"] += 1
            if athlete.headshot_url:
                counts["has_photo"] += 1
            if dry_run:
                continue
            updated = update_fighter_enrichment(
                connection,
                fighter_id,
                nickname=athlete.nickname,
                headshot_url=athlete.headshot_url,
                nationality=athlete.nationality,
                birth_date=athlete.birth_date,
                height_cm=athlete.height_cm,
                reach_cm=athlete.reach_cm,
                weight_grams=athlete.weight_grams,
                stance=athlete.stance,
            )
            connection.commit()
            if updated:
                counts["updated"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="ESPN enrichment for upcoming-event fighters.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    args = parser.parse_args()
    configure_logging()
    counts = enrich_upcoming_fighters(dry_run=args.dry_run)
    keys = ["gap_fighters", "resolved", "unresolved", "errors", "has_photo", "updated"]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))


if __name__ == "__main__":
    main()
