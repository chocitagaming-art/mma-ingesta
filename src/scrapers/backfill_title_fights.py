"""One-off backfill: mark UFC title fights (fights.is_title_fight) from ufcstats.

The results scraper historically never captured the title flag for FOUGHT bouts
— only the ufc.com UPCOMING scraper set it, and those are different rows keyed by
a different (source, source_id) — so every ufcstats fight sat at the migration-010
default FALSE (0 of ~8.7k marked). ufcstats flags a title bout with a belt icon
in the card's weight-class cell, which ``parse_event_fights`` now reads
(``is_title_fight``).

This sweep walks the completed events index ONCE, fetches each event page, and
UPDATEs each already-stored fight's ``is_title_fight`` by (source, source_id) —
matching the card row's ``data-link`` to the fight we already store. It NEVER
inserts a fight and touches NO other column. Idempotent and resumable: only rows
whose flag actually changes are written, so a re-run (or the forward scraper,
which now sets the flag on import) leaves everything untouched.

Cost: ~30 index pages + one fetch per completed event (~700-800), rate-limited by
UfcStatsClient — a one-off run of roughly a quarter of an hour, far cheaper than
re-fetching all ~8.7k individual fight-detail pages.

Usage:
    python -m src.scrapers.backfill_title_fights --dry-run
    python -m src.scrapers.backfill_title_fights --limit 50
    python -m src.scrapers.backfill_title_fights
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .logging_config import configure_logging
from .parsers.events import parse_events_index
from .parsers.fights import parse_event_fights
from .repositories.fights import set_fight_title_flag, title_flag_would_change

LOGGER = logging.getLogger(__name__)

EVENTS_INDEX_URL = "http://ufcstats.com/statistics/events/completed?page={page}"
# The completed index is paginated newest-first (~25 events/page); this cap is a
# safety net against a pathological index — a few decades of UFC events fit well
# inside it.
DEFAULT_MAX_INDEX_PAGES = 100


def _collect_event_urls(client: UfcStatsClient, settings, max_pages: int) -> list[str]:
    """Every completed-event detail_url on the ufcstats index, newest-first."""
    urls: list[str] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        index_page = client.fetch(EVENTS_INDEX_URL.format(page=page))
        records = parse_events_index(index_page.soup, settings)
        if not records:
            break  # ran past the last page
        for record in records:
            if record.detail_url and record.detail_url not in seen:
                seen.add(record.detail_url)
                urls.append(record.detail_url)
    return urls


def sweep_title_fights(
    connection,
    client: UfcStatsClient,
    settings,
    dry_run: bool = False,
    limit: int | None = None,
    max_pages: int = DEFAULT_MAX_INDEX_PAGES,
) -> Counter:
    """Scan completed events (given an open connection + client) and flag titles.

    Each event is its own transaction: on success it commits, on error it rolls
    back, is counted, and the sweep moves on — one unfetchable/unparseable event
    never aborts the rest.
    """
    counts: Counter = Counter()
    event_urls = _collect_event_urls(client, settings, max_pages)
    if limit is not None:
        event_urls = event_urls[:limit]
    counts["events_total"] = len(event_urls)
    LOGGER.info("Completed events to scan: %d", len(event_urls))
    for detail_url in event_urls:
        try:
            event_page = client.fetch(detail_url)
            fights = parse_event_fights(event_page.soup, settings)
            for parsed_fight in fights:
                counts["fights_seen"] += 1
                if parsed_fight.is_title_fight:
                    counts["title_fights_on_card"] += 1
                changed = (
                    title_flag_would_change(
                        connection, settings.source_name,
                        parsed_fight.source_id, parsed_fight.is_title_fight,
                    )
                    if dry_run
                    else set_fight_title_flag(
                        connection, settings.source_name,
                        parsed_fight.source_id, parsed_fight.is_title_fight,
                    )
                )
                if changed:
                    counts["rows_changed"] += 1
                    counts[
                        "rows_set_true" if parsed_fight.is_title_fight else "rows_set_false"
                    ] += 1
            if not dry_run:
                connection.commit()
            counts["events_done"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate the failure, keep sweeping
            if not dry_run:
                connection.rollback()
            counts["event_errors"] += 1
            LOGGER.warning("Event %s failed, skipping: %s", detail_url, exc)
    return counts


def backfill_title_fights(
    dry_run: bool = False,
    limit: int | None = None,
    max_pages: int = DEFAULT_MAX_INDEX_PAGES,
) -> Counter:
    """Sweep every completed ufcstats event, flagging its title bouts in the DB."""
    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        return sweep_title_fights(
            connection, client, settings, dry_run=dry_run, limit=limit, max_pages=max_pages
        )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Backfill fights.is_title_fight for fought UFC bouts from ufcstats belt icons."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan + report how many rows would change, without writing.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most this many completed events (newest-first).",
    )
    args = parser.parse_args()

    totals = backfill_title_fights(dry_run=args.dry_run, limit=args.limit)
    keys = [
        "events_total", "events_done", "event_errors", "fights_seen",
        "title_fights_on_card", "rows_changed", "rows_set_true", "rows_set_false",
    ]
    print(json.dumps({k: totals.get(k, 0) for k in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
