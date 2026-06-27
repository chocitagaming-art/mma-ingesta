"""Bounded wait/retry when the connection pool is momentarily exhausted (item E).

A burst of concurrent /predict requests can briefly check out every pooled
connection; rather than 500 immediately we wait a fraction of a second for one to
be returned. These tests drive db._getconn_with_wait directly with a fake pool and
a fake clock, so they're instant and never touch Neon.
"""

import pytest
from psycopg2 import pool as psycopg2_pool

from src.scrapers import db


def test_getconn_waits_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    class FakePool:
        def getconn(self):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise psycopg2_pool.PoolError("connection pool exhausted")
            return "borrowed-connection"

    sleeps: list[float] = []
    monkeypatch.setattr(db.time, "sleep", lambda seconds: sleeps.append(seconds))

    connection = db._getconn_with_wait(FakePool(), timeout=5.0, interval=0.01)

    assert connection == "borrowed-connection"
    assert attempts["n"] == 3
    # Retried twice (two waits) before a connection freed up.
    assert sleeps == [0.01, 0.01]


def test_getconn_raises_after_bounded_timeout(monkeypatch):
    class FakePool:
        def getconn(self):
            raise psycopg2_pool.PoolError("connection pool exhausted")

    clock = {"t": 0.0}
    monkeypatch.setattr(db.time, "monotonic", lambda: clock["t"])

    def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(db.time, "sleep", fake_sleep)

    with pytest.raises(psycopg2_pool.PoolError):
        db._getconn_with_wait(FakePool(), timeout=0.2, interval=0.05)

    # The bounded deadline was reached instead of looping forever.
    assert clock["t"] >= 0.2
