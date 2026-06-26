from __future__ import annotations

import threading
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor

# Shared connection pool. OPTIONAL: only the long-lived FastAPI service
# (service.py) initializes it via init_pool(). The scrapers never call
# init_pool(), so they keep opening/closing a direct connection per use and
# their behaviour is unchanged. Neon's free tier caps concurrent connections,
# so the service reuses a small handful of sockets instead of opening one per
# query (3 per /predict request).
_pool: psycopg2_pool.ThreadedConnectionPool | None = None
_pool_url: str | None = None
_pool_lock = threading.Lock()


def init_pool(database_url: str, minconn: int = 1, maxconn: int = 5) -> None:
    """Create the shared connection pool once (idempotent, thread-safe)."""
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            return
        _pool = psycopg2_pool.ThreadedConnectionPool(
            minconn, maxconn, dsn=database_url
        )
        _pool_url = database_url


def close_pool() -> None:
    """Dispose of the shared pool (service shutdown)."""
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None
            _pool_url = None


@contextmanager
def connect(database_url: str):
    """Yield a database connection.

    When the shared pool has been initialized for this same URL, the connection
    is borrowed from it and returned afterwards (no socket churn). Otherwise a
    fresh connection is opened and closed, preserving the original scraper
    behaviour exactly.
    """
    if _pool is not None and database_url == _pool_url:
        connection = _pool.getconn()
        try:
            yield connection
            # Pooled connections are reused: end the implicit transaction so the
            # socket is not returned "idle in transaction" holding locks.
            connection.rollback()
        except Exception:
            connection.rollback()
            raise
        finally:
            _pool.putconn(connection)
    else:
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
