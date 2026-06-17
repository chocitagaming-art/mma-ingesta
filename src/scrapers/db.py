from __future__ import annotations

from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


@contextmanager
def connect(database_url: str):
    connection = psycopg2.connect(database_url)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def cursor(connection):
    db_cursor = connection.cursor(cursor_factory=RealDictCursor)
    try:
        yield db_cursor
    finally:
        db_cursor.close()