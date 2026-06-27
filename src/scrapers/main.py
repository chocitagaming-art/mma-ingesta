from __future__ import annotations

import logging
from collections import Counter
from datetime import date
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
from .repositories.events import find_existing_event_id, upsert_event
from .repositories.fighters import get_fighter_id_by_source, upsert_fighter
from .repositories.fights import (
    get_fight_corner_assignment,
    list_fights_for_winner_repair,
    swap_fight_corners,
    update_fight_winner,
    upsert_fight,
    upsert_fight_stats,
)
from .utils import clean_text, source_id_from_url


LOGGER = logging.getLogger(__name__)
FIGHTERS_URL_TEMPLATE = "http://ufcstats.com/statistics/fighters?char={letter}&page=all"
EVENTS_URL = "http://ufcstats.com/statistics/events/completed"
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


def repair_fight_winners() -> Counter:
    settings = get_settings()
    client = UfcStatsClient(settings)
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        fights = list_fights_for_winner_repair(connection)
        for fight_id, source_id in fights:
            detail_url = f"http://ufcstats.com{source_id}" if source_id.startswith("/") else f"http://ufcstats.com/fight-details/{source_id}"
            try:
                LOGGER.info("Repairing fight winner from %s", detail_url)
                fight_page = client.fetch(detail_url)
                fighter_links = fight_page.soup.select(".b-fight-details__person-name a[href*='/fighter-details/']")
                if len(fighter_links) < 2:
                    counts["repair_skipped_unparsed"] += 1
                    continue
                page_red_source_id = source_id_from_url(fighter_links[0].get("href")) if fighter_links[0].get("href") else None
                page_blue_source_id = source_id_from_url(fighter_links[1].get("href")) if fighter_links[1].get("href") else None
                winner_corner = None
                statuses = [
                    clean_text(node.get_text(" ", strip=True)).lower()
                    for node in fight_page.soup.select(".b-fight-details__person-status")
                    if clean_text(node.get_text(" ", strip=True))
                ]
                if len(statuses) >= 2:
                    if statuses[0] == "w" and statuses[1] == "l":
                        winner_corner = "red"
                    elif statuses[0] == "l" and statuses[1] == "w":
                        winner_corner = "blue"
                page_red_id = _resolve_fighter_id(connection, {}, settings.source_name, page_red_source_id)
                page_blue_id = _resolve_fighter_id(connection, {}, settings.source_name, page_blue_source_id)
                stored_red_id, stored_blue_id = get_fight_corner_assignment(connection, fight_id)
                if stored_red_id == page_blue_id and stored_blue_id == page_red_id:
                    swap_fight_corners(connection, fight_id)
                    counts["repair_swapped_corners"] += 1
                    stored_red_id, stored_blue_id = get_fight_corner_assignment(connection, fight_id)
                elif stored_red_id != page_red_id or stored_blue_id != page_blue_id:
                    counts["repair_skipped_mismatch"] += 1
                    connection.rollback()
                    continue
                if winner_corner is None:
                    # Statuses were empty / non-parseable / a draw: there is no
                    # confirmed winner. Calling update_fight_winner with None here
                    # would NULL an already-stored victory, so skip the update.
                    # (Any corner swap above is legitimate and is kept.)
                    counts["repair_skipped_no_winner"] += 1
                    connection.commit()
                    continue
                winner_id = stored_red_id if winner_corner == "red" else stored_blue_id
                update_fight_winner(connection, fight_id, winner_id)
                connection.commit()
                counts["repair_updated"] += 1
            except Exception as exc:
                connection.rollback()
                counts["repair_errors"] += 1
                LOGGER.exception("Failed to repair fight %s: %s", detail_url, exc)
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
    today = date.today()
    event_records = []
    for events_page in client.fetch_all_pages(EVENTS_URL):
        parsed_events = parse_events_index(events_page.soup, settings)
        # Keep events that have already happened. The completed index lists the
        # next upcoming event(s) at the top with a FUTURE date and no stats yet;
        # exclude only those. (Previously a hardcoded year<=2025 filter silently
        # dropped every 2026+ event -> all current-year fights went missing.)
        completed_event_records = [
            event_record
            for event_record in parsed_events
            if event_record.event.event_date and event_record.event.event_date <= today
        ]
        event_records.extend(completed_event_records or parsed_events)
    deduped_event_records = []
    seen_event_urls: set[str] = set()
    for event_record in event_records:
        if event_record.detail_url in seen_event_urls:
            continue
        seen_event_urls.add(event_record.detail_url)
        deduped_event_records.append(event_record)
    event_records = deduped_event_records
    if max_events is not None:
        event_records = event_records[:max_events]
    for event_record in event_records:
        try:
            existing_event_id = find_existing_event_id(connection, event_record.event)
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