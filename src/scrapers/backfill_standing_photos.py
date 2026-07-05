"""Backfill fighters.standing_body_url from ufc.com event bout views (round A).

Each bout on a ufc.com event detail page serves a standing full-body photo per
corner (Drupal style event_fight_card_upper_body_of_standing_athlete: PNG alpha,
~185x600). The daily upcoming-events scraper now captures it for upcoming cards;
this one-off pass sweeps the events ALREADY in our DB with source='ufc.com' —
upcoming AND completed, because their /event/<slug> pages stay live after the
event (e.g. /event/ufc-328) — newest first, so partial passes (--limit) cover
the freshest photos first.

Round B (deduced slugs): completed events ingested from ufcstats never carried a
ufc.com slug (source/source_id NULL), so the source='ufc.com' sweep skipped them
and their fighters kept no standing photo — the owner spotted it on the site.
For recently completed events without source='ufc.com' (--lookback-days, default
120) guess_ufc_slug derives candidate slugs from the event name/date ("UFC 328:
X vs Y" -> ufc-328; fight nights -> ufc-fight-night-april-25-2026). Candidates
are probed with the same fetch; NOTE unknown slugs do NOT 404 — ufc.com
302-redirects them to /search (200 after redirects), so a page that parses to
ZERO bouts is treated as a miss and the next candidate is tried. When a slug
resolves, the event row is stamped source='ufc.com'/source_id=<slug> ONLY if
both were NULL (guarded UPDATE, never clobbers another source's linkage nor the
unique (source, source_id) index), so future sweeps cover it directly.

Matching: bouts are keyed by data-fmid, which upcoming_events stored verbatim in
fights.source_id (fallback '<slug>#<order>'), so each parsed corner maps to the
fighter_red_id/fighter_blue_id already resolved in our fights table. When that
row is missing or the corner id is NULL, the central name matcher (same
espn+rankings chain the daily scraper uses) resolves the corner name. Deduced
events (ufcstats fights carry no ufc.com fmid) always resolve via the matcher.

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
    python -m src.scrapers.backfill_standing_photos --lookback-days 365  # wider slug deduction
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from datetime import date

from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .repositories.fighters import get_all_fighters, update_fighter_standing_photo
from .upcoming_events import (
    HOME_URL,
    SOURCE,
    ParsedBout,
    _get_soup,
    _make_matcher,
    _new_session,
    _parse_bouts,
)

LOGGER = logging.getLogger(__name__)

EVENT_URL_TEMPLATE = "https://www.ufc.com/event/{slug}"

# Deduced-slug window: how far back completed events without source='ufc.com'
# are considered. Wide enough for the weekly cron to never leave a gap.
DEFAULT_LOOKBACK_DAYS = 120

# Hardcoded (not strftime %B) so slugs stay English under any locale.
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

# fetch(url) -> parsed soup of the event detail page. Injected in tests.
FetchSoup = Callable[[str], BeautifulSoup]


def guess_ufc_slug(name: str, event_date: date | None) -> list[str]:
    """Candidate ufc.com /event/ slugs for an event named by another source.

    - Numbered cards ("UFC 328: X vs Y") -> ["ufc-328"].
    - Fight nights and every other unnumbered card (UFC Fight Night, UFC on
      ABC/ESPN, The Ultimate Fighter finales...) -> the date-based slug
      "ufc-fight-night-<month>-<day>-<year>". ufc.com serves the day as-is
      (HEAD /event/ufc-fight-night-april-25-2026 -> 200, verified live), so
      the unpadded day goes first and a zero-padded variant is kept as a
      second candidate for days < 10.
    Ordered most-likely first. Empty when nothing can be deduced (no date).
    """
    numbered = re.match(r"\s*UFC\s+(\d+)", name or "", re.IGNORECASE)
    if numbered:
        return [f"ufc-{numbered.group(1)}"]
    if event_date is None:
        return []
    month = _MONTHS[event_date.month - 1]
    candidates = [f"ufc-fight-night-{month}-{event_date.day}-{event_date.year}"]
    padded = f"ufc-fight-night-{month}-{event_date.day:02d}-{event_date.year}"
    if padded != candidates[0]:
        candidates.append(padded)
    return candidates


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


def _get_recent_events_without_source(
    connection, lookback_days: int
) -> list[tuple[int, str, date | None]]:
    """(event_id, name, event_date) of recently completed events NOT sourced
    from ufc.com (typically ufcstats imports, which leave source/source_id
    NULL), newest first. These are the deduced-slug candidates."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, event_date
            FROM events
            WHERE status = 'completed'
              AND (source IS NULL OR source <> %s)
              AND event_date >= CURRENT_DATE - %s
            ORDER BY event_date DESC, id DESC
            """,
            (SOURCE, lookback_days),
        )
        return [(int(row[0]), str(row[1]), row[2]) for row in cursor.fetchall()]


def _claim_event_source(connection, event_id: int, slug: str) -> bool:
    """Stamp source='ufc.com'/source_id=<slug> on the event so future sweeps
    target it directly. Guarded like repositories/events.py upserts: only rows
    with BOTH source and source_id NULL are touched (never clobbers another
    source's linkage), and NOT EXISTS keeps the partial unique index
    idx_events_source (source, source_id) safe when a ufc.com twin row already
    holds this slug. Returns True if the row was claimed."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE events
            SET source = %s, source_id = %s
            WHERE id = %s
              AND source IS NULL AND source_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM events twin
                  WHERE twin.source = %s AND twin.source_id = %s AND twin.id <> %s
              )
            """,
            (SOURCE, slug, event_id, SOURCE, slug, event_id),
        )
        return cursor.rowcount > 0


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


def _fetch_bouts(fetch: FetchSoup, slug: str) -> list[ParsedBout]:
    soup = fetch(EVENT_URL_TEMPLATE.format(slug=slug))
    return _parse_bouts(soup, slug)


def _apply_bouts(
    connection,
    match: Callable[[str], int | None],
    counts: Counter,
    event_id: int,
    bouts: list[ParsedBout],
    *,
    dry_run: bool,
) -> None:
    """Write each parsed corner's standing photo onto its fighter row."""
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
                # Bout not in fights (card changed / ufcstats-sourced event) or
                # corner unresolved at scrape time: central name matcher.
                fighter_id = match(name)
            if fighter_id is None:
                counts["unmatched"] += 1
                continue
            counts["matched"] += 1
            if not dry_run and update_fighter_standing_photo(connection, fighter_id, image_url):
                counts["updated"] += 1


def backfill(
    connection,
    fetch: FetchSoup,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Counter:
    counts: Counter = Counter()
    targets = _get_target_events(connection, limit=limit)
    total = len(targets)
    counts["events"] = total
    match = _make_matcher(get_all_fighters(connection))
    LOGGER.info("ufc.com events to sweep for standing photos: %d", total)

    for idx, (event_id, slug) in enumerate(targets, 1):
        try:
            bouts = _fetch_bouts(fetch, slug)
        except Exception as exc:
            counts["fetch_errors"] += 1
            LOGGER.warning("Failed to fetch/parse event %s (id=%d): %s", slug, event_id, exc)
            continue
        _apply_bouts(connection, match, counts, event_id, bouts, dry_run=dry_run)
        if not dry_run:
            connection.commit()
        LOGGER.info(
            "Progress %d/%d (%s) — images=%d matched=%d updated=%d unmatched=%d errors=%d",
            idx, total, slug, counts["images_found"], counts["matched"],
            counts["updated"], counts["unmatched"], counts["fetch_errors"],
        )

    # Round B: recently completed events with no ufc.com slug (ufcstats imports)
    # — deduce candidate slugs from name/date and probe each one.
    deduced = _get_recent_events_without_source(connection, lookback_days)
    LOGGER.info(
        "completed events without source='ufc.com' in the last %d days: %d",
        lookback_days, len(deduced),
    )
    for event_id, name, event_date in deduced:
        resolved: tuple[str, list[ParsedBout]] | None = None
        for slug in guess_ufc_slug(name, event_date):
            try:
                bouts = _fetch_bouts(fetch, slug)
            except Exception as exc:
                # 404/HTTP error on a guessed slug is an expected miss, not a
                # fetch_error: try the next candidate.
                LOGGER.debug("Candidate slug %s missed for event %d: %s", slug, event_id, exc)
                continue
            if not bouts:
                # Unknown slugs do NOT 404: ufc.com 302-redirects to /search,
                # which parses to zero bouts -> miss, try the next candidate.
                continue
            resolved = (slug, bouts)
            break
        if resolved is None:
            counts["deduced_unresolved"] += 1
            LOGGER.info("No ufc.com slug candidate worked for event %d (%s)", event_id, name)
            continue
        slug, bouts = resolved
        counts["events"] += 1
        counts["deduced_resolved"] += 1
        _apply_bouts(connection, match, counts, event_id, bouts, dry_run=dry_run)
        if not dry_run:
            if _claim_event_source(connection, event_id, slug):
                counts["source_claimed"] += 1
            connection.commit()
        LOGGER.info(
            "Deduced %s -> event %d (%s) — images=%d matched=%d updated=%d unmatched=%d",
            slug, event_id, name, counts["images_found"], counts["matched"],
            counts["updated"], counts["unmatched"],
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
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help="Also deduce ufc.com slugs for completed events without "
             "source='ufc.com' in this window (default %(default)s).",
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
        counts = backfill(
            connection, fetch,
            dry_run=args.dry_run, limit=args.limit, lookback_days=args.lookback_days,
        )

    keys = [
        "events", "fetch_errors", "images_found", "matched", "unmatched", "updated",
        "deduced_resolved", "deduced_unresolved", "source_claimed",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
