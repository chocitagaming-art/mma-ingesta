from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from psycopg2.extensions import connection as PgConnection

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
        cursor.execute(
            """
            INSERT INTO events (
                name, event_date, start_time, location, promotion_id, status,
                image_url, tagline, broadcast, ticket_url, headliner, source, source_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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


def upsert_event(connection: PgConnection, event: EventRecord) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO events (name, event_date, location, promotion_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (event.name, event.event_date, event.location, event.promotion_id),
        )
        row = cursor.fetchone()
        if row:
            return int(row[0])
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
        return int(cursor.fetchone()[0])