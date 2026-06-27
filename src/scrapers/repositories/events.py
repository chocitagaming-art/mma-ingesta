from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from psycopg2.extensions import connection as PgConnection

from ..matching import strip_accents
from ..models import EventRecord


@dataclass(frozen=True)
class EventMetaRecord:
    name: str
    event_date: date | None
    start_time: datetime | None
    location: str | None
    promotion_id: int
    status: str
    image_url: str | None
    tagline: str | None
    broadcast: str | None
    ticket_url: str | None
    headliner: str | None
    source: str
    source_id: str


def upsert_event_meta(connection: PgConnection, event: EventMetaRecord) -> int:
    """Upsert an event by (source, source_id), returning its id. Used for upcoming
    events scraped from ufc.com; leaves the existing ufcstats events untouched."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM events WHERE source = %s AND source_id = %s",
            (event.source, event.source_id),
        )
        row = cursor.fetchone()
        if row:
            event_id = int(row[0])
            cursor.execute(
                """
                UPDATE events SET
                    name = %s, event_date = %s, start_time = %s, location = %s,
                    promotion_id = %s, status = %s, image_url = %s, tagline = %s,
                    broadcast = %s, ticket_url = %s, headliner = %s
                WHERE id = %s
                """,
                (
                    event.name, event.event_date, event.start_time, event.location,
                    event.promotion_id, event.status, event.image_url, event.tagline,
                    event.broadcast, event.ticket_url, event.headliner, event_id,
                ),
            )
            return event_id
        # The (source, source_id) lookup above missed, so this is a new ufc.com card.
        # It can still collide with an existing row on the events_name_date_promotion_key
        # natural key (migration 007) — e.g. the ufcstats historical twin once the date
        # arrives. ON CONFLICT DO UPDATE merges the upcoming metadata onto that row
        # instead of raising IntegrityError or inserting a duplicate.
        cursor.execute(
            """
            INSERT INTO events (
                name, event_date, start_time, location, promotion_id, status,
                image_url, tagline, broadcast, ticket_url, headliner, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name, event_date, promotion_id) DO UPDATE SET
                start_time = EXCLUDED.start_time, location = EXCLUDED.location,
                status = EXCLUDED.status, image_url = EXCLUDED.image_url,
                tagline = EXCLUDED.tagline, broadcast = EXCLUDED.broadcast,
                ticket_url = EXCLUDED.ticket_url, headliner = EXCLUDED.headliner,
                source = EXCLUDED.source, source_id = EXCLUDED.source_id
            RETURNING id
            """,
            (
                event.name, event.event_date, event.start_time, event.location,
                event.promotion_id, event.status, event.image_url, event.tagline,
                event.broadcast, event.ticket_url, event.headliner, event.source,
                event.source_id,
            ),
        )
        return int(cursor.fetchone()[0])


def get_event_id(
    connection: PgConnection,
    event: EventRecord,
) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM events
            WHERE name = %s
              AND event_date IS NOT DISTINCT FROM %s
              AND location IS NOT DISTINCT FROM %s
              AND promotion_id = %s
            """,
            (event.name, event.event_date, event.location, event.promotion_id),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else None


# Tokens shared by ~every UFC card; useless for telling two cards apart, so they
# are dropped before comparing names (only headliner surnames / the card number
# remain as the discriminating signal).
_EVENT_STOPWORDS = {
    "ufc", "fight", "night", "on", "espn", "abc", "fox", "fx", "fuel", "tv",
    "vs", "the", "presents", "dana", "white", "s", "contender", "series", "tuf",
}


def _name_tokens(name: str) -> set[str]:
    folded = strip_accents((name or "").lower())
    return set(re.sub(r"[^a-z0-9 ]", " ", folded).split())


def _significant_tokens(name: str) -> set[str]:
    return _name_tokens(name) - _EVENT_STOPWORDS


def find_existing_event_id(connection: PgConnection, event: EventRecord) -> int | None:
    """Existing event id for this card, tolerant of cross-source name/location drift.

    ufc.com and ufcstats name and locate the same card differently (different
    location strings, 'vs' vs 'vs.', occasionally a numbered alias), so the exact
    (name, date, location, promotion) match used by get_event_id misses the twin and
    a fresh INSERT would create a DUPLICATE event. Fallback: among same-(date,
    promotion) events, match the one sharing a MEANINGFUL name token (a headliner
    surname or the card number, after dropping the ubiquitous 'ufc/fight/night/...'
    stopwords). A shared date alone is NOT enough — every UFC event has
    promotion_id=1 and the UFC can run two distinct cards on one date, so requiring a
    real name overlap lets the cross-source twin match while a genuinely different
    same-day card stays unmatched and gets imported. Used by both the historical
    importer (main.scrape_events) and the catch-up (import_recent_events).
    """
    exact = get_event_id(connection, event)
    if exact is not None:
        return exact
    if event.event_date is None:
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, name FROM events WHERE event_date = %s AND promotion_id = %s ORDER BY id",
            (event.event_date, event.promotion_id),
        )
        rows = cursor.fetchall()
    if not rows:
        return None
    target = _significant_tokens(event.name)
    if not target:
        return None  # nothing distinctive to match on -> treat as a new card
    best_id, best_overlap = None, 0
    for row_id, row_name in rows:  # ordered by id -> deterministic
        overlap = len(target & _significant_tokens(row_name))
        if overlap > best_overlap:
            best_overlap, best_id = overlap, int(row_id)
    # Require >=2 shared meaningful tokens (typically both headliner surnames). One
    # shared token can be coincidental; zero means a different card -> import it.
    return best_id if best_overlap >= 2 else None


def upsert_event(connection: PgConnection, event: EventRecord) -> int:
    with connection.cursor() as cursor:
        # Explicit target so the dedup fires against events_name_date_promotion_key
        # (migration 007). NULL natural-key columns bypass the conflict (a UNIQUE
        # treats NULLs as distinct), so those rows are always inserted as new.
        cursor.execute(
            """
            INSERT INTO events (name, event_date, location, promotion_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name, event_date, promotion_id) DO NOTHING
            RETURNING id
            """,
            (event.name, event.event_date, event.location, event.promotion_id),
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])
        # DO NOTHING fired: a row already holds this natural key. Look it up by the
        # SAME columns as the conflict target (NOT location, which is not part of the
        # key and can drift across sources) so the conflicting row is always found.
        cursor.execute(
            """
            SELECT id
            FROM events
            WHERE name = %s
              AND event_date IS NOT DISTINCT FROM %s
              AND promotion_id IS NOT DISTINCT FROM %s
            ORDER BY id
            LIMIT 1
            """,
            (event.name, event.event_date, event.promotion_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "upsert_event: ON CONFLICT fired but no row matched the natural key "
                f"(name={event.name!r}, event_date={event.event_date!r}, "
                f"promotion_id={event.promotion_id!r})"
            )
        return int(row[0])