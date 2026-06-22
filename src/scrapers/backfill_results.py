"""Fill results AND fight_stats onto past ufc.com event bouts from ufcstats.

upcoming_events.py scrapes ufc.com for upcoming cards, storing each bout with NULL
winner/method/end_round/end_time and no fight_stats. Once an event happens it
moves to "Pasados" (by date) but still shows no result and no strike stats,
because ufc.com never publishes them and the ufcstats historical importer keeps
its own event rows (so re-importing would duplicate the event). This closes the
gap onto the EXISTING ufc.com bout rows:

  1. Find past ufc.com events whose bouts still lack a result OR fight_stats.
  2. Match each to the corresponding ufcstats event by date (+ name tie-break).
  3. For each bout, open its ufcstats fight detail page once and fill the result
     (winner/method/round/time) AND the fight_stats (sig strikes + the #45
     head/body/leg + distance/clinch/ground breakdown), matching fighters by name
     (corner-swap tolerant) so no duplicate events/fights are ever created.

Idempotent: results only fill where method is still NULL; stats upsert ON
CONFLICT. Wired as the last step of refresh_upcoming.py, so every daily refresh
fully ingests whatever just finished — that's how just-completed events get
their strike stats without the historical re-import.

Usage (writes to the DB):
    python -m src.scrapers.backfill_results --dry-run   # find + match + report, no writes
    python -m src.scrapers.backfill_results             # fill results + stats
    python -m src.scrapers.backfill_results --limit 5   # at most N events this run
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter

from .config import get_settings
from .db import connect
from .enrich_ranked import _fold
from .http import UfcStatsClient
from .logging_config import configure_logging
from .parsers.events import parse_events_index
from .parsers.fights import (
    FightPageRecord,
    build_fight_stats_record,
    parse_event_fights,
    parse_fight_stats,
)
from .repositories.fights import upsert_fight_stats
from .utils import clean_text

LOGGER = logging.getLogger(__name__)

UFC_SOURCE = "ufc.com"
UFCSTATS_EVENTS_URL = "http://ufcstats.com/statistics/events/completed"


class _Bout:
    __slots__ = ("id", "red_id", "blue_id", "red_name", "blue_name", "method", "has_stats")

    def __init__(self, id, red_id, blue_id, red_name, blue_name, method=None, has_stats=False):
        self.id = id
        self.red_id = red_id
        self.blue_id = blue_id
        self.red_name = red_name or ""
        self.blue_name = blue_name or ""
        self.method = method
        self.has_stats = has_stats

    def key(self) -> frozenset[str]:
        return frozenset({_fold(self.red_name), _fold(self.blue_name)})

    def fighter_id_for(self, name: str) -> int | None:
        """Map a fighter NAME (from ufcstats) onto this bout's red/blue id."""
        folded = _fold(name)
        if folded == _fold(self.red_name):
            return self.red_id
        if folded == _fold(self.blue_name):
            return self.blue_id
        return None


def _get_events_needing_results(connection, limit: int | None) -> list[tuple[int, str, object]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name, e.event_date
            FROM events e
            WHERE e.source = %s
              AND e.event_date IS NOT NULL
              AND e.event_date < CURRENT_DATE
              AND EXISTS (
                SELECT 1 FROM fights fi
                WHERE fi.event_id = e.id
                  AND (
                    fi.method IS NULL
                    OR NOT EXISTS (SELECT 1 FROM fight_stats fs WHERE fs.fight_id = fi.id)
                  )
              )
            ORDER BY e.event_date DESC
            """,
            (UFC_SOURCE,),
        )
        rows = [(int(r[0]), str(r[1]), r[2]) for r in cursor.fetchall()]
    return rows[:limit] if limit is not None else rows


def _get_bouts(connection, event_id: int) -> list[_Bout]:
    """All bouts of an event with result/stats state, so we fill whichever is missing."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fi.id, fi.fighter_red_id, fi.fighter_blue_id,
                   COALESCE(red.name, fi.fighter_red_name) AS red_name,
                   COALESCE(blue.name, fi.fighter_blue_name) AS blue_name,
                   fi.method,
                   EXISTS (SELECT 1 FROM fight_stats fs WHERE fs.fight_id = fi.id) AS has_stats
            FROM fights fi
            LEFT JOIN fighters red ON red.id = fi.fighter_red_id
            LEFT JOIN fighters blue ON blue.id = fi.fighter_blue_id
            WHERE fi.event_id = %s
            """,
            (event_id,),
        )
        return [_Bout(int(r[0]), r[1], r[2], r[3], r[4], r[5], bool(r[6])) for r in cursor.fetchall()]


def _match_event(event_name: str, event_date, index_records) -> str | None:
    """Pick the ufcstats event matching this ufc.com event: same date, best name."""
    same_date = [rec for rec in index_records if rec.event.event_date == event_date]
    if not same_date:
        return None
    if len(same_date) == 1:
        return same_date[0].detail_url
    target = _fold(event_name)
    best = max(same_date, key=lambda rec: _name_overlap(target, _fold(rec.event.name)))
    return best.detail_url


def _name_overlap(a: str, b: str) -> int:
    return len(set(a.split()) & set(b.split()))


def _winner_name_from_soup(soup) -> str | None:
    """Read the winner off a fight's OWN detail-page soup.

    The event page only flags THAT a bout has a winner (a single green "win" flag),
    not which fighter — so we read the per-fighter W/L status. Returns the winning
    fighter's name, or None for a draw / no-contest / unparseable result.
    """
    names = [
        clean_text(a.get_text(" ", strip=True)) or ""
        for a in soup.select(".b-fight-details__person-name a[href*='/fighter-details/']")
    ]
    statuses = [
        s
        for s in (
            (clean_text(n.get_text(" ", strip=True)) or "").lower()
            for n in soup.select(".b-fight-details__person-status")
        )
        if s
    ]
    if len(names) >= 2 and len(statuses) >= 2:
        if statuses[0] == "w":
            return names[0]
        if statuses[1] == "w":
            return names[1]
    return None


def _winner_id_for(bout: _Bout, winner_name: str | None) -> int | None:
    """Map a winning fighter name onto this bout's red/blue id (corner-swap tolerant)."""
    if not winner_name:
        return None
    winner = _fold(winner_name)
    if _fold(bout.red_name) == winner:
        return bout.red_id
    if _fold(bout.blue_name) == winner:
        return bout.blue_id
    return None


def _fill_event(connection, client, settings, event_id, bouts, detail_url, counts, dry_run):
    page = client.fetch(detail_url)
    fights = parse_event_fights(page.soup, settings)
    by_key: dict[frozenset[str], FightPageRecord] = {
        frozenset({_fold(f.red_name), _fold(f.blue_name)}): f for f in fights
    }
    for bout in bouts:
        needs_result = bout.method is None
        needs_stats = not bout.has_stats
        if not needs_result and not needs_stats:
            continue
        fight = by_key.get(bout.key())
        if fight is None:
            counts["bouts_unmatched"] += 1
            LOGGER.info("  no ufcstats fight for %s vs %s", bout.red_name, bout.blue_name)
            continue
        # One fetch of the fight's own detail page serves both the result and the stats.
        fight_page = client.fetch(fight.detail_url)
        if needs_result:
            winner_name = _winner_name_from_soup(fight_page.soup)
            winner_id = _winner_id_for(bout, winner_name)
            LOGGER.info(
                "  %s vs %s -> winner=%r (id=%s) method=%s round=%s time=%s",
                bout.red_name, bout.blue_name,
                winner_name, winner_id, fight.method, fight.end_round, fight.end_time,
            )
            if dry_run:
                counts["bouts_filled"] += 1
            else:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE fights
                        SET winner_id = %s, method = %s, end_round = %s, end_time = %s
                        WHERE id = %s AND method IS NULL
                        """,
                        (winner_id, fight.method, fight.end_round, fight.end_time, bout.id),
                    )
                    if cursor.rowcount:
                        counts["bouts_filled"] += 1
        if needs_stats:
            for fighter_stats in parse_fight_stats(fight_page.soup):
                fighter_id = bout.fighter_id_for(fighter_stats.fighter_name)
                if fighter_id is None:
                    counts["stats_unmatched"] += 1
                    continue
                if not dry_run:
                    upsert_fight_stats(connection, build_fight_stats_record(bout.id, fighter_id, fighter_stats))
                counts["stats_rows"] += 1


def backfill(dry_run: bool = False, limit: int | None = None) -> dict:
    settings = get_settings()
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        events = _get_events_needing_results(connection, limit)
        counts["events_candidate"] = len(events)
        LOGGER.info("Past ufc.com events missing results: %d", len(events))
        if not events:
            return dict(counts)

        client = UfcStatsClient(settings)
        index = parse_events_index(client.fetch(UFCSTATS_EVENTS_URL).soup, settings)
        LOGGER.info("ufcstats completed-events index (page 1): %d events", len(index))

        for event_id, name, event_date in events:
            detail_url = _match_event(name, event_date, index)
            if detail_url is None:
                counts["events_unmatched"] += 1
                LOGGER.info("No ufcstats match for %r (%s)", name, event_date)
                continue
            counts["events_matched"] += 1
            bouts = _get_bouts(connection, event_id)
            LOGGER.info("%r (%s) -> %s | %d bouts", name, event_date, detail_url, len(bouts))
            try:
                _fill_event(connection, client, settings, event_id, bouts, detail_url, counts, dry_run)
                if not dry_run:
                    connection.commit()
            except Exception as exc:  # noqa: BLE001 - keep going to the next event
                connection.rollback()
                counts["event_errors"] += 1
                LOGGER.exception("Failed to fill %r: %s", name, exc)
    return dict(counts)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Backfill results onto past ufc.com event bouts from ufcstats.")
    parser.add_argument("--dry-run", action="store_true", help="Find + match + report, no writes.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many events.")
    args = parser.parse_args()
    counts = backfill(dry_run=args.dry_run, limit=args.limit)
    keys = [
        "events_candidate", "events_matched", "events_unmatched",
        "bouts_filled", "bouts_unmatched", "stats_rows", "stats_unmatched", "event_errors",
    ]
    print(json.dumps({k: counts.get(k, 0) for k in keys}, indent=2))


if __name__ == "__main__":
    main()
