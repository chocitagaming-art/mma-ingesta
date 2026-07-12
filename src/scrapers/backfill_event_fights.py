"""Re-import ALL fights of an EXISTING completed event from ufcstats.

The importers skip events already present in the DB (main.py's
events_skipped_existing, import_recent_events' events_already_present), so an
event row that landed with an incomplete card — e.g. event 315 "UFC Fight
Night: Imavov vs. Borralho" (2025-09-06), inserted by the historical importer
with only 2 of its ~12 fights — can never be repaired by re-running them. This
script targets ONE existing event by DB id and re-ingests its full ufcstats
card onto that same row:

  1. Load the event (name + event_date) from the DB; it must exist, be
     'completed', and have no fights keyed under another source: against e.g.
     a ufc.com card (source='ufc.com', source_id=fmid) the ON CONFLICT below
     would never fire and every bout would be INSERTED again as a duplicate.
  2. Locate the matching ufcstats event on the completed index: ALL same-date
     candidates are collected across pages (the index is paginated newest-first
     and a same-date pair can straddle a page boundary), then the best
     DISTINCTIVE name-token overlap wins — a date alone is ambiguous on
     double-event dates (real precedent: 2014-06-28) and matching the wrong
     event would re-point ITS fights onto this event_id.
  3. parse_event_fights on the event page; per fight, ensure/resolve both
     fighters (WITHOUT intermediate commits — see _ensure_fighter_present_no_commit
     — so the final commit stays all-or-nothing), read the winner AND the real
     scheduled rounds ("Time format: 5 Rnd ...", which the event page can only
     infer) from the fight's OWN detail page and upsert the FightRecord with
     source='ufcstats' + source_id=<fight-details path>. The UNIQUE
     (source, source_id) + ON CONFLICT DO UPDATE means already-present fights
     are UPDATED and the missing ones INSERTED — never duplicated, and never a
     new event row.
  4. fight_stats are parsed from the same fight page and upserted
     (parse_fight_stats + upsert_fight_stats, like import_recent_events; the
     per-round breakdown has its own backfill in backfill_round_stats.py).

Usage:
    python -m src.scrapers.backfill_event_fights --event-id 315 --dry-run
    python -m src.scrapers.backfill_event_fights --event-id 315
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import date

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .import_recent_events import _winner_source_id
from .logging_config import configure_logging
from .main import FIGHTER_DETAIL_URL_TEMPLATE, _resolve_fighter_id
from .matching import alnum_name, strip_accents
from .models import FightRecord
from .parsers.events import parse_events_index
from .parsers.fighters import parse_fighter_detail
from .parsers.fights import (
    build_fight_stats_record,
    parse_event_fights,
    parse_fight_stats,
    parse_scheduled_rounds,
)
from .repositories.fighters import upsert_fighter
from .repositories.fights import upsert_fight, upsert_fight_stats

LOGGER = logging.getLogger(__name__)

EVENTS_INDEX_URL = "http://ufcstats.com/statistics/events/completed?page={page}"
# The completed index is newest-first; walking stops as soon as a page's dates
# are already older than the target, so this cap is only a safety net against
# a pathological index (~25 events per page covers decades well before it).
DEFAULT_MAX_INDEX_PAGES = 100

# An event with FEWER than this many fights counts as an incomplete card worth
# re-importing (event 315 landed with 2 of ~12). The re-import is idempotent
# (upsert on (source, source_id)), so the threshold only bounds which events the
# --all sweep bothers to fetch; a card that is already complete just updates in
# place if it slips through. Override with --max-fights.
DEFAULT_MAX_FIGHTS = 6


def _get_event(connection, event_id: int) -> tuple[str, date]:
    """(name, event_date) of a completed DB event, or raise ValueError."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, event_date, status FROM events WHERE id = %s",
            (event_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Event {event_id} not found in the DB.")
    name, event_date, status = str(row[0]), row[1], row[2]
    if status != "completed":
        raise ValueError(
            f"Event {event_id} ({name}) has status {status!r}; only 'completed' "
            "events have a ufcstats results page to backfill from."
        )
    if event_date is None:
        raise ValueError(
            f"Event {event_id} ({name}) has no event_date; cannot match it on ufcstats."
        )
    return name, event_date


def _assert_no_foreign_source_fights(connection, event_id: int, source: str) -> None:
    """Raise when the event already has fights keyed under ANOTHER source.

    upsert_fight keys on UNIQUE (source, source_id). Against an event whose
    card was ingested from ufc.com (source='ufc.com', source_id=fmid — and
    upcoming_events flips those events to 'completed' once their date passes,
    so they DO pass the status guard) the ON CONFLICT would never fire and
    this backfill would INSERT a complete parallel card, duplicating every
    bout. Those events are backfill_results' job; failing here also makes
    --dry-run surface the problem instead of reporting clean-looking upserts.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT source FROM fights WHERE event_id = %s",
            (event_id,),
        )
        sources = sorted(str(row[0]) for row in cursor.fetchall())
    foreign = [s for s in sources if s != source]
    if foreign:
        raise ValueError(
            f"Event {event_id} already has fights keyed under source(s) {foreign}; "
            f"re-importing from {source!r} would duplicate its card because "
            "fights upsert on (source, source_id) and rows from another source "
            "never conflict-update. Use backfill_results for ufc.com-keyed events."
        )


# Tokens shared by virtually every UFC event name ("UFC Fight Night: X vs. Y"):
# an overlap on these alone says nothing about identity, so only the remaining
# tokens (fighter surnames, event numbers) count as a name match.
_GENERIC_EVENT_TOKENS = {"ufc", "fight", "night", "event", "fn", "vs", "the", "on"}


def _distinctive_name_overlap(a: str, b: str) -> int:
    tokens_a = set(alnum_name(strip_accents(a)).split()) - _GENERIC_EVENT_TOKENS
    tokens_b = set(alnum_name(strip_accents(b)).split()) - _GENERIC_EVENT_TOKENS
    return len(tokens_a & tokens_b)


def _find_ufcstats_event_url(
    client: UfcStatsClient,
    settings,
    event_name: str,
    event_date: date,
    max_pages: int = DEFAULT_MAX_INDEX_PAGES,
) -> str | None:
    """detail_url of the ufcstats event matching (event_date, event_name).

    The completed index only shows the most recent events on page 1, so pages
    are walked until their dates are already older than the target (then the
    event is simply not listed). ALL same-date candidates are collected before
    picking — a per-page short-circuit would return the wrong event when a
    same-date pair straddles a page boundary — and the winner must share at
    least one DISTINCTIVE name token with the target: the date alone is
    ambiguous on double-event dates, and upserting the wrong event's card
    would corrupt TWO events (its existing fights get re-pointed onto this
    event_id by the ON CONFLICT update)."""
    candidates = []
    for page in range(1, max_pages + 1):
        index_page = client.fetch(EVENTS_INDEX_URL.format(page=page))
        records = parse_events_index(index_page.soup, settings)
        if not records:
            break  # ran past the last page
        candidates.extend(rec for rec in records if rec.event.event_date == event_date)
        dates = [r.event.event_date for r in records if r.event.event_date]
        if dates and min(dates) < event_date:
            break  # the index is past the target date; nothing older can match
    if not candidates:
        return None
    best = max(candidates, key=lambda rec: _distinctive_name_overlap(event_name, rec.event.name))
    if _distinctive_name_overlap(event_name, best.event.name) == 0:
        LOGGER.warning(
            "Rejecting same-date ufcstats event %r for target %r: no distinctive name token in common",
            best.event.name, event_name,
        )
        return None
    LOGGER.info("Matched ufcstats event %r (%s)", best.event.name, best.detail_url)
    return best.detail_url


def _find_fighter_id_by_name_other_source(connection, name: str, source: str) -> int | None:
    """id of the single existing fighter with this name under ANOTHER source.

    A card fighter can already exist only under e.g. source='espn'
    (espn_import_all inserts athletes it cannot match by name); creating a
    fresh ufcstats row would duplicate the person, splitting their data. Rows
    of the SAME source are deliberately excluded: had they been this person,
    the (source, source_id) lookup would have found them, so a same-source
    name hit is a homonym (two real fighters can share a name). 0 or 2+ hits
    return None (ambiguous -> create rather than guess)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM fighters
            WHERE source <> %s
              AND lower(btrim(regexp_replace(name, '\\s+', ' ', 'g')))
                = lower(btrim(regexp_replace(%s, '\\s+', ' ', 'g')))
            """,
            (source, name),
        )
        rows = cursor.fetchall()
    return int(rows[0][0]) if len(rows) == 1 else None


def _ensure_fighter_present_no_commit(
    connection,
    client: UfcStatsClient,
    settings,
    counts: Counter,
    fighter_id_by_source: dict[str, int],
    source_id: str | None,
) -> int | None:
    """Resolve or create a fighter WITHOUT committing.

    main._ensure_fighter_present commits (and on error rollbacks) after each
    new fighter, which in this module's single transaction would also persist
    every fight upserted so far and silently break the all-or-nothing contract
    (a later failure could then only roll back PART of the event). Here any
    error propagates so the caller's except/rollback undoes the WHOLE event,
    and the one final commit stays the only commit."""
    fighter_id = _resolve_fighter_id(connection, fighter_id_by_source, settings.source_name, source_id)
    if fighter_id is not None or not source_id:
        return fighter_id
    fighter_url = (
        f"http://ufcstats.com{source_id}"
        if source_id.startswith("/")
        else FIGHTER_DETAIL_URL_TEMPLATE.format(fighter_id=source_id)
    )
    fighter_page = client.fetch(fighter_url)
    fighter = parse_fighter_detail(fighter_page.soup, fighter_url, settings)
    fighter_id = _find_fighter_id_by_name_other_source(connection, fighter.name, settings.source_name)
    if fighter_id is not None:
        counts["fighters_matched_by_name"] += 1
        LOGGER.warning(
            "Fighter %s (%s) already exists under another source as id=%d; "
            "reusing that row instead of creating a duplicate",
            fighter.name, source_id, fighter_id,
        )
    else:
        fighter_id = upsert_fighter(connection, fighter)
        counts["fighters_backfilled"] += 1
        LOGGER.info("Created fighter %s (%s) as id=%d", fighter.name, source_id, fighter_id)
    fighter_id_by_source[source_id] = fighter_id
    return fighter_id


def backfill_event_fights(
    connection,
    client: UfcStatsClient,
    settings,
    event_id: int,
    dry_run: bool = False,
    max_pages: int = DEFAULT_MAX_INDEX_PAGES,
) -> Counter:
    counts: Counter = Counter()
    event_name, event_date = _get_event(connection, event_id)
    # Refuse to re-import onto an event whose card came from another source
    # (ufc.com/espn): those rows never conflict-update on (source, source_id),
    # so every bout would be INSERTED again as a duplicate. Checked BEFORE any
    # fetch so --dry-run surfaces the problem too.
    _assert_no_foreign_source_fights(connection, event_id, settings.source_name)
    LOGGER.info("Backfilling fights of event %d: %s (%s)", event_id, event_name, event_date)

    detail_url = _find_ufcstats_event_url(
        client, settings, event_name, event_date, max_pages=max_pages
    )
    if detail_url is None:
        raise ValueError(
            f"No ufcstats event matches {event_name!r} on {event_date} "
            f"in the completed index."
        )
    counts["event_matched"] = 1
    LOGGER.info("Matched ufcstats event page %s", detail_url)

    event_page = client.fetch(detail_url)
    fights = parse_event_fights(event_page.soup, settings)
    counts["fights_parsed"] = len(fights)
    LOGGER.info("Parsed %d fights on the ufcstats card", len(fights))

    fighter_id_by_source: dict[str, int] = {}
    try:
        for parsed_fight in fights:
            # Creating fighters writes, so in --dry-run we only RESOLVE existing
            # ones; unknown fighters leave the fight skipped (same policy as
            # import_recent_events).
            if not dry_run:
                _ensure_fighter_present_no_commit(
                    connection, client, settings, counts, fighter_id_by_source, parsed_fight.red_source_id
                )
                _ensure_fighter_present_no_commit(
                    connection, client, settings, counts, fighter_id_by_source, parsed_fight.blue_source_id
                )
            red_id = _resolve_fighter_id(
                connection, fighter_id_by_source, settings.source_name, parsed_fight.red_source_id
            )
            blue_id = _resolve_fighter_id(
                connection, fighter_id_by_source, settings.source_name, parsed_fight.blue_source_id
            )
            if red_id is None or blue_id is None:
                counts["fights_skipped_missing_fighter"] += 1
                LOGGER.warning(
                    "Skipping %s vs %s: fighter missing from DB (%s)",
                    parsed_fight.red_name, parsed_fight.blue_name, parsed_fight.detail_url,
                )
                continue
            # The event page only flags THAT a bout had a winner; the fight's own
            # detail page says WHICH fighter (and carries the stats tables).
            fight_page = client.fetch(parsed_fight.detail_url)
            winner_src = _winner_source_id(fight_page.soup)
            winner_id = None
            if winner_src == parsed_fight.red_source_id:
                winner_id = red_id
            elif winner_src == parsed_fight.blue_source_id:
                winner_id = blue_id
            elif winner_src is not None:
                counts["winner_unmatched"] += 1
                LOGGER.warning(
                    "winner %s matched neither corner on fight %s", winner_src, parsed_fight.detail_url
                )
            fight = FightRecord(
                event_id=event_id,  # ALWAYS the existing row: never a new event
                fighter_red_id=red_id,
                fighter_blue_id=blue_id,
                weight_class=parsed_fight.weight_class,
                weight_grams=None,
                # The detail page states the real round count ("Time format:
                # 5 Rnd (5-5-5-5-5)"); the event-page value is only inferred
                # from the weight cell and misses non-title 5-round headliners.
                scheduled_rounds=(
                    parse_scheduled_rounds(fight_page.soup) or parsed_fight.scheduled_rounds
                ),
                winner_id=winner_id,
                method=parsed_fight.method,
                end_round=parsed_fight.end_round,
                end_time=parsed_fight.end_time,
                odds_red=None,
                odds_blue=None,
                source=settings.source_name,
                source_id=parsed_fight.source_id,
                is_title_fight=parsed_fight.is_title_fight,
            )
            # UNIQUE (source, source_id) + ON CONFLICT DO UPDATE: the fights the
            # event already has are updated in place, the missing ones inserted.
            fight_id = upsert_fight(connection, fight) if not dry_run else -1
            counts["fights_upserted"] += 1
            LOGGER.info(
                "%s fight %s: %s vs %s",
                "Would upsert" if dry_run else "Upserted",
                parsed_fight.source_id, parsed_fight.red_name, parsed_fight.blue_name,
            )
            for fighter_stats in parse_fight_stats(fight_page.soup):
                fighter_id = _resolve_fighter_id(
                    connection, fighter_id_by_source, settings.source_name, fighter_stats.fighter_source_id
                )
                if fighter_id is None:
                    counts["stats_unresolved_fighter"] += 1
                    continue
                if not dry_run:
                    upsert_fight_stats(
                        connection, build_fight_stats_record(fight_id, fighter_id, fighter_stats)
                    )
                counts["stats_rows"] += 1
        if not dry_run:
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    return counts


def _get_events_needing_fights(
    connection, source_name: str, limit: int | None, max_fights: int
) -> list[tuple[int, str, object, str]]:
    """Past events with a partial/empty card keyed only under our own source.

    EXCLUDES any event that already has a fight from ANOTHER source (ufc.com /
    espn): re-importing those from ufcstats would duplicate the card (they never
    conflict-update on (source, source_id) — the same reason
    _assert_no_foreign_source_fights guards a single event), and they are
    backfill_results' job. An event with a fight count below ``max_fights`` —
    including zero fights — qualifies. Ordered newest-first; a fixed --limit lets
    the sweep run in bounded batches and is resumable (repaired cards stop
    qualifying). Returns (id, name, event_date, status)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name, e.event_date, e.status
            FROM events e
            WHERE e.event_date IS NOT NULL
              AND e.event_date < CURRENT_DATE
              AND NOT EXISTS (
                    SELECT 1 FROM fights f
                    WHERE f.event_id = e.id AND f.source IS DISTINCT FROM %s
              )
              AND (SELECT count(*) FROM fights f WHERE f.event_id = e.id) < %s
            ORDER BY e.event_date DESC
            """,
            (source_name, max_fights),
        )
        rows = [(int(r[0]), str(r[1]), r[2], str(r[3])) for r in cursor.fetchall()]
    return rows[:limit] if limit is not None else rows


def _flip_event_completed(connection, event_id: int) -> bool:
    """Flip a PAST event still stuck in 'upcoming' to 'completed'.

    A backfill needs status='completed' (that is when ufcstats has the results
    page). The daily refresh already flips dropped upcoming events by date; this
    is the same guarded UPDATE (only ever fires for a past date) for the rare
    event the sweep meets before that cron does. Returns whether a row changed.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE events SET status = 'completed' "
            "WHERE id = %s AND status <> 'completed' AND event_date < CURRENT_DATE",
            (event_id,),
        )
        return cursor.rowcount > 0


# counts key on backfill_event_fights' Counter -> aggregated key on the sweep total.
_SWEEP_ROLLUP = {
    "event_matched": "events_matched",
    "fights_parsed": "fights_parsed",
    "fights_upserted": "fights_upserted",
    "fights_skipped_missing_fighter": "fights_skipped_missing_fighter",
    "fighters_backfilled": "fighters_created",
    "stats_rows": "stats_rows",
}


def _sweep(connection, client, settings, candidates, dry_run: bool) -> Counter:
    """Run backfill_event_fights over each candidate, isolating per-event failures.

    Each event is its own all-or-nothing transaction (backfill_event_fights
    commits on success, rolls back and re-raises on error), so one bad event
    (no ufcstats match, foreign-source guard, parse error) is counted and the
    sweep continues to the next — the backfill_results pattern.
    """
    totals: Counter = Counter()
    totals["events_candidate"] = len(candidates)
    for event_id, name, _event_date, status in candidates:
        try:
            if status != "completed":
                if dry_run:
                    totals["events_upcoming_skipped_dry_run"] += 1
                    LOGGER.info(
                        "Dry-run: event %d (%s) is %s; would flip to completed first",
                        event_id, name, status,
                    )
                    continue
                if _flip_event_completed(connection, event_id):
                    connection.commit()
                    totals["events_status_flipped"] += 1
            counts = backfill_event_fights(
                connection, client, settings, event_id, dry_run=dry_run
            )
            totals["events_done"] += 1
            for src_key, dst_key in _SWEEP_ROLLUP.items():
                totals[dst_key] += counts.get(src_key, 0)
        except Exception as exc:  # noqa: BLE001 - isolate the failure, keep sweeping
            connection.rollback()
            totals["event_errors"] += 1
            LOGGER.warning("Event %d (%s) failed, skipping: %s", event_id, name, exc)
    return totals


def backfill_all_event_fights(
    dry_run: bool = False,
    limit: int | None = None,
    max_fights: int = DEFAULT_MAX_FIGHTS,
) -> Counter:
    """Sweep past events with an incomplete ufcstats card, re-importing each."""
    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        candidates = _get_events_needing_fights(
            connection, settings.source_name, limit, max_fights
        )
        LOGGER.info(
            "Past events with an incomplete card (<%d fights): %d",
            max_fights, len(candidates),
        )
        return _sweep(connection, client, settings, candidates, dry_run)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Re-import ufcstats fights onto existing completed event(s)."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--event-id", type=int, help="DB id of a single existing completed event."
    )
    mode.add_argument(
        "--all", action="store_true",
        help="Sweep every past event with an incomplete card (<--max-fights fights).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse + match + report what would be written, no writes."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="(--all) process at most this many events."
    )
    parser.add_argument(
        "--max-fights", type=int, default=DEFAULT_MAX_FIGHTS,
        help=f"(--all) fewer than this many fights = incomplete card (default {DEFAULT_MAX_FIGHTS}).",
    )
    args = parser.parse_args()

    if args.all:
        totals = backfill_all_event_fights(
            dry_run=args.dry_run, limit=args.limit, max_fights=args.max_fights
        )
        keys = [
            "events_candidate", "events_done", "events_matched", "events_status_flipped",
            "events_upcoming_skipped_dry_run", "fights_parsed", "fights_upserted",
            "fights_skipped_missing_fighter", "fighters_created", "stats_rows", "event_errors",
        ]
        print(json.dumps({k: totals.get(k, 0) for k in keys}, indent=2))
        if args.dry_run:
            print("Dry-run: nothing was written. Re-run without --dry-run to persist.")
        return

    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        try:
            counts = backfill_event_fights(
                connection, client, settings, args.event_id, dry_run=args.dry_run
            )
        except ValueError as exc:
            parser.error(str(exc))

    summary = {
        "event_id": args.event_id,
        "event_matched": counts.get("event_matched", 0),
        "fights_parsed": counts.get("fights_parsed", 0),
        "fights_upserted": counts.get("fights_upserted", 0),
        "fights_skipped_missing_fighter": counts.get("fights_skipped_missing_fighter", 0),
        # main._ensure_fighter_present counts new fighter rows as "fighters_backfilled".
        "fighters_created": counts.get("fighters_backfilled", 0),
        "stats_rows": counts.get("stats_rows", 0),
        "stats_unresolved_fighter": counts.get("stats_unresolved_fighter", 0),
        "winner_unmatched": counts.get("winner_unmatched", 0),
    }
    print(json.dumps(summary, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
