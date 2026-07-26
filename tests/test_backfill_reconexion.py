"""Una desconexion de Neon no puede tirar un rescate de horas.

QUE PASO DE VERDAD (26-jul-2026, lote de 455 eventos): a los 7 minutos Neon
cerro la conexion en mitad de un `_fill_event`. Hasta ahi, normal — pasa. Lo
grave fue lo que vino despues:

    psycopg2.OperationalError: server closed the connection unexpectedly
    During handling of the above exception, another exception occurred:
    psycopg2.InterfaceError: connection already closed

El `except` por evento existe justamente para seguir con el siguiente, pero su
`connection.rollback()` reventaba sobre una conexion ya muerta, y ESA segunda
excepcion no la cogia nadie: el proceso entero moria y se perdian los 431
eventos que faltaban. El manejo de errores era mas fragil que el error.

Dos reglas, y las dos se prueban aqui:
  1. Un rollback sobre una conexion muerta NO puede propagarse.
  2. Con la conexion caida hay que RECONECTAR y seguir, no abandonar el lote.
"""

from __future__ import annotations

import psycopg2
import pytest

from src.scrapers.backfill_results import reconectar_si_hace_falta, rollback_seguro


class _ConexionFalsa:
    def __init__(self, closed: int = 0, rollback_revienta: bool = False):
        self.closed = closed
        self._rollback_revienta = rollback_revienta
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1
        if self._rollback_revienta:
            raise psycopg2.InterfaceError("connection already closed")


# --------------------------------------------------------- rollback seguro


def test_el_rollback_normal_se_hace():
    conn = _ConexionFalsa()
    assert rollback_seguro(conn) is True
    assert conn.rollbacks == 1


def test_un_rollback_sobre_conexion_muerta_no_propaga():
    # El caso exacto del 26-jul: sin esto, el proceso entero muere.
    conn = _ConexionFalsa(closed=1, rollback_revienta=True)
    assert rollback_seguro(conn) is False  # no explota: informa y sigue


# ------------------------------------------------------------ reconexion


def test_con_la_conexion_viva_no_se_reconecta():
    conn = _ConexionFalsa()
    llamadas = []
    nueva = reconectar_si_hace_falta(conn, lambda: llamadas.append(1) or _ConexionFalsa())
    assert nueva is conn
    assert llamadas == []


def test_con_la_conexion_cerrada_se_abre_otra():
    muerta = _ConexionFalsa(closed=1)
    viva = _ConexionFalsa()
    nueva = reconectar_si_hace_falta(muerta, lambda: viva)
    assert nueva is viva


def test_si_la_reconexion_tambien_falla_se_propaga():
    """Aqui SI hay que rendirse: sin BD no hay nada que hacer, y seguir
    intentando 431 eventos contra un servidor caido solo alarga la agonia."""
    def _no_hay_manera():
        raise psycopg2.OperationalError("no se puede conectar")

    with pytest.raises(psycopg2.OperationalError):
        reconectar_si_hace_falta(_ConexionFalsa(closed=1), _no_hay_manera)
