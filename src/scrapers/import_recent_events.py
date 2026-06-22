"""Import completed ufcstats events that the historical importer missed.

main.py used to drop every 2026+ event (hardcoded year filter, now fixed), so
completed current-year events never landed in the DB with their fights +
fight_stats. backfill_results.py only fills winner/method onto pre-existing
ufc.com bouts (no stats, and only for events still in the DB) — it cannot create
the missing events. This does.

For every completed ufcstats event NOT already in the DB (matched by name+date,
ANY source -> never duplicates an existing ufc.com or ufcstats event), it imports:
  - the event (source-less row, like the historical ufcstats events),
  - each fight with its winner read from the fight's OWN detail page (the event
    page only flags THAT a bout had a winner, not which fighter),
  - the full fight_stats incl. the #45 head/body/leg + distance/clinch/ground
    breakdown (via the round-summing parser).

Idempotent: existing events are skipped; fights/stats upsert ON CONFLICT. The
year-filter fix means new completed events also flow in through main.py going
forward; this is the one-shot catch-up for the backlog.

Usage (writes to the DB):
    python -m src.scrapers.import_recent_events --dry-run
    python -m src.scrapers.import_recent_events
    python -m src.scrapers.import_recent_events --pages 3 --limit 25
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from datetime import date

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .logging_config import configure_logging
from .main import _ensure_fighter_present, _resolve_fighter_id
from .models import FightRecord
from .parsers.events import parse_events_index
from .parsers.fights import build_fight_stats_record, parse_event_fights, parse_fight_stats
from .repositories.events import find_existing_event_id, upsert_event
from .repositories.fights import upsert_fight, upsert_fight_stats
from .utils import clean_text, source_id_from_url

LOGGER = logging.getLogger(__name__)
EVENTS_URL = "http://ufcstats.com/statistics/events/completed?page={page}"


def _winner_source_id(soup) -> str | None:
    """The winning fighter's ufcstats source_id, or None (draw / NC / unparseable)."""
    for person in soup.select(".b-fight-details__person"):
        status_el = person.select_one(".b-fight-details__person-status")
        status = (clean_text(status_el.get_text(" ", strip=True)) or "").lower() if status_el else ""
        link = person.select_one(".b-fight-details__person-name a[href*='/fighter-details/']")
        if status == "w" and link and link.get("href"):
            return source_id_from_url(link.get("href"))
    return None


def import_recent_events(
    connection,
    client: UfcStatsClient,
    settings,
    pages: int = 3,
    limit: int | None = None,
    dry_run: bool = False,
) -> Counter:
    counts: Counter = Counter()
    today = date.today()
    fighter_id_by_source: dict[str, int] = {}

    # Collect candidate events from the most-recent index pages.
    candidates = []
    seen_urls: set[str] = set()
    for page in range(1, pages + 1):
        index = client.fetch(EVENTS_URL.format(page=page))
        for record in parse_events_index(index.soup, settings):
            ev = record.event
            if record.detail_url in seen_urls:
                continue
            seen_urls.add(record.detail_url)
            if not ev.event_date or ev.event_date > today:
                continue  # upcoming / not yet happened
            if find_existing_event_id(connection, ev) is not None:
                counts["events_already_present"] += 1
                continue
            candidates.append(record)

    candidates.sort(key=lambda r: r.event.event_date)
    if limit is not None:
        candidates = candidates[:limit]
    LOGGER.info("Completed events missing from DB: %d", len(candidates))

    for record in candidates:
        try:
            LOGGER.info("Importing %s (%s)", record.event.name, record.event.event_date)
            event_page = client.fetch(record.detail_url)
            fights = parse_event_fights(event_page.soup, settings)
            event_id = upsert_event(connection, record.event) if not dry_run else -1
            for parsed_fight in fights:
                # _ensure_fighter_present writes (upsert + commit), so in --dry-run we
                # only RESOLVE existing fighters; unknown ones leave the fight skipped.
                if not dry_run:
                    _ensure_fighter_present(
                        connection, client, settings, counts, fighter_id_by_source, parsed_fight.red_source_id
                    )
                    _ensure_fighter_present(
                        connection, client, settings, counts, fighter_id_by_source, parsed_fight.blue_source_id
                    )
                red_id = _resolve_fighter_id(connection, fighter_id_by_source, settings.source_name, parsed_fight.red_source_id)
                blue_id = _resolve_fighter_id(connection, fighter_id_by_source, settings.source_name, parsed_fight.blue_source_id)
                if red_id is None or blue_id is None:
                    counts["fight_skipped_missing_fighter"] += 1
                    continue
                fight_page = client.fetch(parsed_fight.detail_url)
                winner_src = _winner_source_id(fight_page.soup)
                winner_id = None
                if winner_src == parsed_fight.red_source_id:
                    winner_id = red_id
                elif winner_src == parsed_fight.blue_source_id:
                    winner_id = blue_id
                elif winner_src is not None:
                    # A decided fight whose winner maps to neither corner -> don't
                    # silently store NULL (looks like a draw); surface it instead.
                    counts["winner_unmatched"] += 1
                    LOGGER.warning(
                        "winner %s matched neither corner on fight %s", winner_src, parsed_fight.detail_url
                    )
                fight = FightRecord(
                    event_id=event_id,
                    fighter_red_id=red_id,
                    fighter_blue_id=blue_id,
                    weight_class=parsed_fight.weight_class,
                    weight_grams=None,
                    scheduled_rounds=parsed_fight.scheduled_rounds,
                    winner_id=winner_id,
                    method=parsed_fight.method,
                    end_round=parsed_fight.end_round,
                    end_time=parsed_fight.end_time,
                    odds_red=None,
                    odds_blue=None,
                    source=settings.source_name,
                    source_id=parsed_fight.source_id,
                )
                if not dry_run:
                    fight_id = upsert_fight(connection, fight)
                    for fighter_stats in parse_fight_stats(fight_page.soup):
                        fighter_id = _resolve_fighter_id(
                            connection, fighter_id_by_source, settings.source_name, fighter_stats.fighter_source_id
                        )
                        if fighter_id is None:
                            counts["stats_unresolved_fighter"] += 1
                            continue
                        upsert_fight_stats(connection, build_fight_stats_record(fight_id, fighter_id, fighter_stats))
                        counts["stats_rows"] += 1
                counts["fights"] += 1
            if not dry_run:
                connection.commit()
            counts["events"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate per-event failures
            connection.rollback()
            counts["event_errors"] += 1
            LOGGER.exception("Failed to import %s: %s", record.detail_url, exc)
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Import completed events missed by the ufcstats historical importer.")
    parser.add_argument("--pages", type=int, default=3, help="How many index pages (most recent first) to scan.")
    parser.add_argument("--limit", type=int, default=None, help="Import at most this many events.")
    parser.add_argument("--dry-run", action="store_true", help="Scan + report, no writes.")
    args = parser.parse_args()
    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        counts = import_recent_events(
            connection, client, settings, pages=args.pages, limit=args.limit, dry_run=args.dry_run
        )
    LOGGER.info("Import complete: %s", dict(counts))


if __name__ == "__main__":
    main()
