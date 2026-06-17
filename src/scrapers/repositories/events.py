from __future__ import annotations

from psycopg2.extensions import connection as PgConnection

from ..models import EventRecord


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