"""Shared in-memory fake DB for the scraper tests.

Every test in this suite mocks the database: ``RecordingConnection`` records the
SQL each cursor runs and answers ``fetch*`` from a caller-supplied responder, so
we can assert *what would be written* without ever opening a socket to Neon.
"""

from types import SimpleNamespace

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
