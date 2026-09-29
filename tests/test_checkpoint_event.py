"""El informe de la mañana siguiente (`scripts/checkpoint_event.py`) marca con
'*' los métodos provisionales que escribe el directo.

Tenía su propia copia a mano de la lista, ('Decision', 'Submission', 'KO/TKO'),
y se quedó atrás el día que 'DQ' pasó a ser provisional: una DQ del directo
habría salido sin asterisco, como si ufcstats ya la hubiera consolidado. Ahora
importa `espn_live_results.ESPN_PROVISIONAL_METHODS`.

No se abre ningún socket: `psycopg2.connect` devuelve la `RecordingConnection`
de conftest y la DSN es falsa.
"""

from __future__ import annotations

from datetime import date

from scripts import checkpoint_event

EVENTO = (1092, "UFC 332", date(2026, 10, 3), None, "completed")

# Las columnas de FIGHTS_SQL: bout_order, status, red, blue, winner, method,
# end_round, end_time, referee, scorecards, stats_rows.
COMBATES = [
    # La noche de la velada: el bucle ya escribió su 'DQ' y ufcstats no ha pasado.
    (1, "completed", "Ilimbek Akylbek Uulu", "Mehemmedeli Osmanli",
     "Ilimbek Akylbek Uulu", "DQ", 1, "2:19", None, 0, 0),
    # Uno ya consolidado por ufcstats: no es provisional y no lleva asterisco.
    (2, "completed", "Rafael Fiziev", "Manuel Torres",
     "Rafael Fiziev", "U-DEC", 3, "5:00", "Herb Dean", 3, 2),
]


def test_una_dq_del_directo_sale_marcada_como_provisional(fakedb, monkeypatch, capsys):
    def base_de_mentira(sql, params=None):
        return [EVENTO] if "FROM events" in sql else COMBATES

    conn = fakedb.Connection(base_de_mentira)
    sesiones = []
    conn.set_session = lambda readonly: sesiones.append(readonly)
    monkeypatch.setattr(checkpoint_event.psycopg2, "connect", lambda dsn: conn)
    monkeypatch.setenv("DATABASE_URL", "postgresql://falsa/no-se-usa")
    monkeypatch.setattr("sys.argv", ["checkpoint_event", "--event-id", "1092"])

    checkpoint_event.main()

    salida = capsys.readouterr().out
    assert "DQ*" in salida, "la DQ del directo sale como si ya la hubiera afinado ufcstats"
    assert "U-DEC*" not in salida
    assert "(provisional/ESPN: 1)" in salida
    assert sesiones == [True]  # sigue abriendo la sesión en solo lectura
    assert fakedb.mutating_statements(conn) == []
