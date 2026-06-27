from __future__ import annotations

import threading
import time
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

# When the pool is momentarily exhausted (a burst of concurrent /predict
# requests), wait briefly for a connection to be returned instead of failing the
# request outright. Bounded so a genuinely stuck pool still surfaces an error
# rather than hanging forever.
POOL_ACQUIRE_TIMEOUT_SECONDS = 5.0
POOL_ACQUIRE_RETRY_INTERVAL_SECONDS = 0.05


def _getconn_with_wait(
    connection_pool: psycopg2_pool.ThreadedConnectionPool,
    timeout: float = POOL_ACQUIRE_TIMEOUT_SECONDS,
    interval: float = POOL_ACQUIRE_RETRY_INTERVAL_SECONDS,
):
    """Borrow a connection, briefly waiting/retrying if the pool is exhausted.

    psycopg2's ThreadedConnectionPool raises PoolError immediately when every
    connection is checked out. For the long-lived service we'd rather let a burst
    queue up for a fraction of a second than turn it into a 500, so we retry until
    a connection frees up or the bounded timeout elapses (then we re-raise)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return connection_pool.getconn()
        except psycopg2_pool.PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(interval)


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
    pool = _pool  # capture once: close_pool() could null the global concurrently
    if pool is not None and database_url == _pool_url:
        connection = _getconn_with_wait(pool)
        try:
            yield connection
            # Pooled connections are reused: end the implicit transaction so the
            # socket is not returned "idle in transaction" holding locks.
            connection.rollback()
        except Exception:
            connection.rollback()
            raise
        finally:
            pool.putconn(connection)
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
