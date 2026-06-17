from __future__ import annotations

import logging
from collections import Counter
from string import ascii_lowercase

from .config import get_settings
from .db import connect
from .espn import scrape_and_enrich
from .http import UfcStatsClient
from .logging_config import configure_logging
from .models import FightRecord
from .parsers.events import parse_events_index
from .parsers.fighters import parse_fighter_detail, parse_fighter_index
from .parsers.fights import build_fight_stats_record, parse_event_fights, parse_fight_stats
from .repositories.events import get_event_id, upsert_event
from .repositories.fighters import get_fighter_id_by_source, upsert_fighter
from .repositories.fights import upsert_fight, upsert_fight_stats


LOGGER = logging.getLogger(__name__)
FIGHTERS_URL_TEMPLATE = "http://ufcstats.com/statistics/fighters?char={letter}&page=all"
EVENTS_URL = "http://ufcstats.com/statistics/events/completed?page=all"
FIGHTER_DETAIL_URL_TEMPLATE = "http://ufcstats.com/fighter-details/{fighter_id}"


def scrape_all(
    fighter_letters: str | None = None,
    max_fighters_per_letter: int | None = None,
    max_events: int | None = None,
    max_fights_per_event: int | None = None,
    skip_events: bool = False,
    skip_fighters: bool = False,
) -> Counter:
    settings = get_settings()
    client = UfcStatsClient(settings)
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        letters = fighter_letters or ascii_lowercase
        fighter_id_by_source: dict[str, int] = {}
        if not skip_fighters:
            fighter_id_by_source = scrape_fighters(
                connection=connection,
                client=client,
                settings=settings,
                counts=counts,
                fighter_letters=letters,
                max_fighters_per_letter=max_fighters_per_letter,
            )

        if skip_events:
            return counts

        scrape_events(
            connection=connection,
            client=client,
            settings=settings,
            counts=counts,
            fighter_id_by_source=fighter_id_by_source,
            max_events=max_events,
            max_fights_per_event=max_fights_per_event,
        )
    return counts


def scrape_events(
    connection,
    client: UfcStatsClient,
    settings,
    counts: Counter,
    fighter_id_by_source: dict[str, int] | None = None,
    max_events: int | None = None,
    max_fights_per_event: int | None = None,
) -> Counter:
    fighter_id_by_source = fighter_id_by_source or {}
    LOGGER.info("Scraping events index %s", EVENTS_URL)
    events_page = client.fetch(EVENTS_URL)
    parsed_events = parse_events_index(events_page.soup, settings)
    historical_event_records = [
        event_record
        for event_record in parsed_events
        if event_record.event.event_date and event_record.event.event_date.year <= 2025
    ]
    event_records = historical_event_records or parsed_events
    if max_events is not None:
        event_records = event_records[:max_events]
    for event_record in event_records:
        try:
            existing_event_id = get_event_id(connection, event_record.event)
            if existing_event_id is not None:
                counts["events_skipped_existing"] += 1
                continue
            LOGGER.info("Scraping event %s", event_record.detail_url)
            event_id = upsert_event(connection, event_record.event)
            counts["events"] += 1
            event_page = client.fetch(event_record.detail_url)
            fights = parse_event_fights(event_page.soup, settings)
            if max_fights_per_event is not None:
                fights = fights[:max_fights_per_event]
            for parsed_fight in fights:
                LOGGER.info(
                    "Processing fight %s vs %s (%s)",
                    parsed_fight.red_name,
                    parsed_fight.blue_name,
                    parsed_fight.detail_url,
                )
                _ensure_fighter_present(
                    connection=connection,
                    client=client,
                    settings=settings,
                    counts=counts,
                    fighter_id_by_source=fighter_id_by_source,
                    source_id=parsed_fight.red_source_id,
                )
                _ensure_fighter_present(
                    connection=connection,
                    client=client,
                    settings=settings,
                    counts=counts,
                    fighter_id_by_source=fighter_id_by_source,
                    source_id=parsed_fight.blue_source_id,
                )
                red_id = _resolve_fighter_id(connection, fighter_id_by_source, settings.source_name, parsed_fight.red_source_id)
                blue_id = _resolve_fighter_id(connection, fighter_id_by_source, settings.source_name, parsed_fight.blue_source_id)
                if red_id is None or blue_id is None:
                    counts["fight_skipped_missing_fighter"] += 1
                    continue
                winner_id = None
                if parsed_fight.winner_corner == "red":
                    winner_id = red_id
                elif parsed_fight.winner_corner == "blue":
                    winner_id = blue_id
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
                fight_id = upsert_fight(connection, fight)
                counts["fights"] += 1
                connection.commit()
                try:
                    fight_page = client.fetch(parsed_fight.detail_url)
                    parsed_stats = parse_fight_stats(fight_page.soup)
                    for fighter_stats in parsed_stats:
                        _ensure_fighter_present(
                            connection=connection,
                            client=client,
                            settings=settings,
                            counts=counts,
                            fighter_id_by_source=fighter_id_by_source,
                            source_id=fighter_stats.fighter_source_id,
                        )
                        fighter_id = _resolve_fighter_id(
                            connection,
                            fighter_id_by_source,
                            settings.source_name,
                            fighter_stats.fighter_source_id,
                        )
                        if fighter_id is None:
                            counts["fight_stats_skipped_missing_fighter"] += 1
                            continue
                        upsert_fight_stats(connection, build_fight_stats_record(fight_id, fighter_id, fighter_stats))
                        counts["fight_stats"] += 1
                    connection.commit()
                except Exception as exc:
                    connection.rollback()
                    counts["fight_stats_errors"] += 1
                    LOGGER.exception("Failed to scrape fight stats %s: %s", parsed_fight.detail_url, exc)
        except Exception as exc:
            connection.rollback()
            counts["event_errors"] += 1
            LOGGER.exception("Failed to scrape event %s: %s", event_record.detail_url, exc)
    return counts


def scrape_fighters(
    connection,
    client: UfcStatsClient,
    settings,
    counts: Counter,
    fighter_letters: str = ascii_lowercase,
    max_fighters_per_letter: int | None = None,
) -> dict[str, int]:
    fighter_id_by_source: dict[str, int] = {}
    for letter in fighter_letters:
        url = FIGHTERS_URL_TEMPLATE.format(letter=letter)
        LOGGER.info("Scraping fighter index %s", url)
        fighter_index = client.fetch(url)
        fighter_urls = parse_fighter_index(fighter_index.soup)
        if max_fighters_per_letter is not None:
            fighter_urls = fighter_urls[:max_fighters_per_letter]
        for fighter_url in fighter_urls:
            try:
                fighter_page = client.fetch(fighter_url)
                fighter = parse_fighter_detail(fighter_page.soup, fighter_url, settings)
                fighter_id = upsert_fighter(connection, fighter)
                fighter_id_by_source[fighter.source_id] = fighter_id
                counts["fighters"] += 1
                connection.commit()
            except Exception as exc:
                connection.rollback()
                counts["fighter_errors"] += 1
                LOGGER.exception("Failed to scrape fighter %s: %s", fighter_url, exc)
    return fighter_id_by_source


def _resolve_fighter_id(
    connection,
    fighter_id_by_source: dict[str, int],
    source: str,
    source_id: str | None,
) -> int | None:
    if not source_id:
        return None
    fighter_id = fighter_id_by_source.get(source_id)
    if fighter_id is not None:
        return fighter_id
    fighter_id = get_fighter_id_by_source(connection, source, source_id)
    if fighter_id is not None:
        fighter_id_by_source[source_id] = fighter_id
    return fighter_id


def _ensure_fighter_present(
    connection,
    client: UfcStatsClient,
    settings,
    counts: Counter,
    fighter_id_by_source: dict[str, int],
    source_id: str | None,
) -> int | None:
    fighter_id = _resolve_fighter_id(connection, fighter_id_by_source, settings.source_name, source_id)
    if fighter_id is not None or not source_id:
        return fighter_id
    fighter_url = f"http://ufcstats.com{source_id}" if source_id.startswith("/") else FIGHTER_DETAIL_URL_TEMPLATE.format(fighter_id=source_id)
    try:
        fighter_page = client.fetch(fighter_url)
        fighter = parse_fighter_detail(fighter_page.soup, fighter_url, settings)
        fighter_id = upsert_fighter(connection, fighter)
        fighter_id_by_source[fighter.source_id] = fighter_id
        counts["fighters_backfilled"] += 1
        connection.commit()
        return fighter_id
    except Exception as exc:
        connection.rollback()
        counts["fighter_backfill_errors"] += 1
        LOGGER.exception("Failed to backfill fighter %s: %s", fighter_url, exc)
        return None


def main() -> None:
    configure_logging()
    counts = scrape_all()
    LOGGER.info("Scrape complete: %s", dict(counts))


def enrich_from_espn() -> None:
    configure_logging()
    counts = scrape_and_enrich()
    LOGGER.info("ESPN enrichment complete: %s", dict(counts))


if __name__ == "__main__":
    main()