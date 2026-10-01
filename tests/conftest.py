"""Shared in-memory fake DB for the scraper tests.

Every test in this suite mocks the database: ``RecordingConnection`` records the
SQL each cursor runs and answers ``fetch*`` from a caller-supplied responder, so
we can assert *what would be written* without ever opening a socket to Neon.
``NeonLikeConnection`` adds Neon's idle-in-transaction timeout on top of it.
"""

from types import SimpleNamespace

import psycopg2
import pytest


class RecordingCursor:
    def __init__(self, responder):
        self._responder = responder
        self.executed: list[tuple[str, object]] = []
        self._result: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        result = self._responder(sql, params)
        self._result = list(result) if result is not None else []

    def executemany(self, sql, seq):
        self.executed.append((sql, list(seq)))
        self._result = []

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    @property
    def rowcount(self):
        return len(self._result)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingConnection:
    def __init__(self, responder):
        self._responder = responder
        self.cursors: list[RecordingCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, cursor_factory=None):
        cur = RecordingCursor(self._responder)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NeonClock:
    """Fake wall clock for NeonLikeConnection: tests advance it by hand
    (typically from the injected ``sleeper``), so no test really waits."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NeonLikeCursor(RecordingCursor):
    def __init__(self, connection: "NeonLikeConnection"):
        super().__init__(connection._responder)
        self._connection = connection

    def execute(self, sql, params=None):
        conn = self._connection
        conn._check_alive()
        # psycopg2 (autocommit off) opens a transaction with the first
        # statement, SELECTs included, and keeps it open until commit/rollback.
        conn.in_txn = True
        super().execute(sql, params)
        conn.last_activity = conn.clock.now


class NeonLikeConnection(RecordingConnection):
    """RecordingConnection that behaves like our Neon server.

    Neon runs with ``idle_in_transaction_session_timeout=5min`` (``SHOW``ed
    from its config file): a session left idle INSIDE an open transaction for
    more than 300 s is killed by the server. The client only finds out on its
    next round trip — a statement, a commit or a rollback — with
    ``psycopg2.OperationalError: SSL connection has been closed unexpectedly``
    (measured read-only against Neon; ``rollback()`` raises it too), and every
    later call gets ``InterfaceError``. enrich-facts died exactly like that on
    1-sep-2026 (run 33484806063). A session idle OUTSIDE a transaction is fine.
    """

    IDLE_IN_TRANSACTION_TIMEOUT = 300.0

    def __init__(self, responder, clock: NeonClock):
        super().__init__(responder)
        self.clock = clock
        self.in_txn = False
        self.last_activity = clock.now
        self.dead = False

    def _check_alive(self) -> None:
        if self.dead:
            raise psycopg2.InterfaceError("connection already closed")
        idle = self.clock.now - self.last_activity
        if self.in_txn and idle > self.IDLE_IN_TRANSACTION_TIMEOUT:
            self.dead = True
            raise psycopg2.OperationalError(
                "SSL connection has been closed unexpectedly\n"
            )

    def cursor(self, cursor_factory=None):
        cur = NeonLikeCursor(self)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self._check_alive()
        self.in_txn = False
        self.last_activity = self.clock.now
        super().commit()

    def rollback(self):
        self._check_alive()
        self.in_txn = False
        self.last_activity = self.clock.now
        super().rollback()


def _executed_statements(conn: RecordingConnection) -> list[str]:
    return [sql for cur in conn.cursors for sql, _ in cur.executed]


def _mutating_statements(conn: RecordingConnection) -> list[str]:
    """SQL statements that would write (UPDATE/DELETE/INSERT), normalized."""
    out = []
    for sql in _executed_statements(conn):
        upper = sql.upper()
        if "UPDATE " in upper or "DELETE " in upper or "INSERT " in upper:
            out.append(" ".join(sql.split()))
    return out


@pytest.fixture
def fakedb():
    return SimpleNamespace(
        Connection=RecordingConnection,
        Cursor=RecordingCursor,
        NeonLikeConnection=NeonLikeConnection,
        NeonClock=NeonClock,
        executed_statements=_executed_statements,
        mutating_statements=_mutating_statements,
    )


@pytest.fixture(autouse=True)
def _sin_latidos_de_verdad(monkeypatch):
    """Ningun test escribe el latido del bucle en la base de VERDAD.

    ESTO NO ES PARANOIA, PASO. El latido del bucle (17-ago-2026) se escribe
    dentro de `run_bounded_loop`, y los tres tests de esa funcion que ya
    existian —test_run_bounded_loop_stops_at_deadline y sus dos hermanos— la
    llaman con `refresh_live_results` parcheado para que la pasada SALGA BIEN.
    O sea que empezaron a escribir en `service_heartbeats` de produccion cada
    vez que alguien corriera la suite con un .env delante. Medido: la fila
    'live-loop' saltaba de las 19:22 a las 20:15 con solo lanzar pytest.

    Rompia ademas la invariante que declara la cabecera de este fichero: aqui
    no se abre un socket a Neon. La red va en el fixture y no en cada test a
    proposito: asi tambien cubre al test que se escriba el mes que viene.

    Los tests que prueban el latido lo vuelven a parchear ellos mismos (el
    ultimo `monkeypatch.setattr` manda), asi que este fixture no les estorba.
    """
    from src.scrapers import espn_live_results

    intentos: list[str] = []
    monkeypatch.setattr(
        espn_live_results, "escribir_latido_del_bucle", lambda detalle: intentos.append(detalle)
    )
    return intentos
