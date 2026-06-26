"""Integration tests for the optional connection pool in db.connect().

These hit the real Neon database (read-only `SELECT pg_backend_pid()`), which is
exactly what they verify: that the long-lived service reuses one server-side
connection instead of opening a fresh one per query, while the scrapers (no pool)
keep their original open/close-per-use behaviour.
"""

import pytest

from src.scrapers import db
from src.scrapers.config import get_settings


@pytest.fixture(autouse=True)
def _clean_pool():
    # Guarantee no pool leaks between tests in either direction.
    db.close_pool()
    yield
    db.close_pool()


def _backend_pid(connection) -> int:
    with db.cursor(connection) as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        return cur.fetchone()["pid"]


def test_pool_reuses_a_single_server_connection():
    url = get_settings().database_url
    db.init_pool(url, minconn=1, maxconn=1)
    with db.connect(url) as c1:
        pid1 = _backend_pid(c1)
    with db.connect(url) as c2:
        pid2 = _backend_pid(c2)
    # Same server-side backend pid -> the pooled socket was reused, not reopened.
    assert pid1 == pid2


def test_without_pool_opens_a_new_connection_each_time():
    url = get_settings().database_url
    with db.connect(url) as c1:
        pid1 = _backend_pid(c1)
    with db.connect(url) as c2:
        pid2 = _backend_pid(c2)
    # Distinct backend pids -> scrapers still open/close a fresh connection.
    assert pid1 != pid2
