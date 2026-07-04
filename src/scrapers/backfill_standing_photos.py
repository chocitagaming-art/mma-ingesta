"""Backfill fighters.standing_body_url from ufc.com event bout views (round A).

Each bout on a ufc.com event detail page serves a standing full-body photo per
corner (Drupal style event_fight_card_upper_body_of_standing_athlete: PNG alpha,
~185x600). The daily upcoming-events scraper now captures it for upcoming cards;
this one-off pass sweeps the events ALREADY in our DB with source='ufc.com' —
upcoming AND completed, because their /event/<slug> pages stay live after the
event (e.g. /event/ufc-328) — newest first, so partial passes (--limit) cover
the freshest photos first.

Matching: bouts are keyed by data-fmid, which upcoming_events stored verbatim in
fights.source_id (fallback '<slug>#<order>'), so each parsed corner maps to the
fighter_red_id/fighter_blue_id already resolved in our fights table. When that
row is missing or the corner id is NULL, the central name matcher (same
espn+rankings chain the daily scraper uses) resolves the corner name.

WRITE POLICY — the NEW value WINS (UFC refreshes the photo after every fight,
so the freshest scrape is authoritative), via update_fighter_standing_photo,
which never writes NULL/empty over a stored value. Since targets are walked
newest-first, a fighter on several events keeps the photo of the most recent
one. Re-dispatching is always safe. --dry-run previews without writing.

Fetching reuses the upcoming-events session (browser UA, warm-up request) and
per-request delay.

Usage:
    python -m src.scrapers.backfill_standing_photos --dry-run --limit 3  # preview
    python -m src.scrapers.backfill_standing_photos --limit 20           # partial pass
    python -m src.scrapers.backfill_standing_photos                      # all ufc.com events
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from collections.abc import Callable

from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .repositories.fighters import get_all_fighters, update_fighter_standing_photo
from .upcoming_events import (
    HOME_URL,
    SOURCE,
    _get_soup,
    _make_matcher,
    _new_session,
    _parse_bouts,
)

LOGGER = logging.getLogger(__name__)

EVENT_URL_TEMPLATE = "https://www.ufc.com/event/{slug}"

# fetch(url) -> parsed soup of the event detail page. Injected in tests.
FetchSoup = Callable[[str], BeautifulSoup]


def _get_target_events(connection, limit: int | None = None) -> list[tuple[int, str]]:
    """(event_id, slug) of every ufc.com-sourced event, newest first.

    Completed events are included on purpose: ufc.com keeps their pages live and
    still serves the bout view with the standing photos.
    """
    sql = """
        SELECT id, source_id
        FROM events
        WHERE source = %s
        ORDER BY event_date DESC NULLS LAST, id DESC
    """
    params: list = [SOURCE]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def _get_event_corner_ids(
    connection, event_id: int
) -> dict[str, tuple[int | None, int | None]]:
    """This event's fights keyed by bout source_id (the ufc.com data-fmid, or
    the '<slug>#<order>' fallback) -> (fighter_red_id, fighter_blue_id)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_id, fighter_red_id, fighter_blue_id
            FROM fights
            WHERE event_id = %s AND source = %s
            """,
            (event_id, SOURCE),
        )
        return {
            str(row[0]): (
                int(row[1]) if row[1] is not None else None,
                int(row[2]) if row[2] is not None else None,
            )
            for row in cursor.fetchall()
        }


def backfill(
    connection,
    fetch: FetchSoup,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> Counter:
    counts: Counter = Counter()
    targets = _get_target_events(connection, limit=limit)
    total = len(targets)
    counts["events"] = total
    match = _make_matcher(get_all_fighters(connection))
    LOGGER.info("ufc.com events to sweep for standing photos: %d", total)

    for idx, (event_id, slug) in enumerate(targets, 1):
        try:
            soup = fetch(EVENT_URL_TEMPLATE.format(slug=slug))
            bouts = _parse_bouts(soup, slug)
        except Exception as exc:
            counts["fetch_errors"] += 1
            LOGGER.warning("Failed to fetch/parse event %s (id=%d): %s", slug, event_id, exc)
            continue
        corner_ids = _get_event_corner_ids(connection, event_id)
        for bout in bouts:
            red_id, blue_id = corner_ids.get(bout.fmid, (None, None))
            for fighter_id, name, image_url in (
                (red_id, bout.red_name, bout.red_image_url),
                (blue_id, bout.blue_name, bout.blue_image_url),
            ):
                if not image_url:
                    continue
                counts["images_found"] += 1
                if fighter_id is None:
                    # Bout not in fights (card changed) or corner unresolved at
                    # scrape time: fall back to the central name matcher.
                    fighter_id = match(name)
                if fighter_id is None:
                    counts["unmatched"] += 1
                    continue
                counts["matched"] += 1
                if not dry_run and update_fighter_standing_photo(connection, fighter_id, image_url):
                    counts["updated"] += 1
        if not dry_run:
            connection.commit()
        LOGGER.info(
            "Progress %d/%d (%s) — images=%d matched=%d updated=%d unmatched=%d errors=%d",
            idx, total, slug, counts["images_found"], counts["matched"],
            counts["updated"], counts["unmatched"], counts["fetch_errors"],
        )
    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill fighters.standing_body_url from ufc.com event bout views."
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse + match but do not write.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most this many events (newest first; re-runnable).",
    )
    args = parser.parse_args()
    configure_logging()

    settings = get_settings()
    session = _new_session()
    # Same warm-up as the daily scraper (cookies before the event pages).
    session.get(HOME_URL, timeout=settings.request_timeout_seconds)

    def fetch(url: str) -> BeautifulSoup:
        return _get_soup(session, url, settings)

    with connect(settings.database_url) as connection:
        counts = backfill(connection, fetch, dry_run=args.dry_run, limit=args.limit)

    keys = ["events", "fetch_errors", "images_found", "matched", "unmatched", "updated"]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
