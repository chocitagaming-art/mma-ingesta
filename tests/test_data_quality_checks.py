"""Tests for the read-only data-quality invariants (F5 qa-data-coverage).

Query shapes, the CRITICAL vs INFO split that drives the workflow exit code,
the markdown render, and that collect() never writes. fakedb answers each query.
"""

from src.scrapers import data_quality_checks as dqc


def _responder(dup_rank=(), no_bouts=(), dup_names=(), espejadas=()):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "FROM rankings" in flat:
            return list(dup_rank)
        if "FROM events" in flat and "status = 'upcoming'" in flat:
            return list(no_bouts)
        if "FROM fight_scorecards" in flat:
            return list(espejadas)
        if "FROM fighters" in flat:
            return list(dup_names)
        return []

    return responder


def test_collect_reads_all_three_checks_read_only(fakedb):
    conn = fakedb.Connection(
        _responder(
            dup_rank=[(1, "Lightweight", 5, "2026-07-01", 2)],
            no_bouts=[(900, "Ghost Event", "2026-08-01")],
            dup_names=[("john doe", 2, [1, 2])],
        )
    )
    data = dqc.collect(connection=conn)
    assert data["duplicate_rank_positions"][0]["rank_position"] == 5
    assert data["upcoming_without_bouts"][0]["id"] == 900
    assert data["duplicate_fighter_names"][0]["ids"] == [1, 2]
    # A monitoring check must never write.
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_query_shapes(fakedb):
    conn = fakedb.Connection(_responder())
    dqc._collect_raw(conn)
    stmts = [" ".join(sql.split()) for cur in conn.cursors for sql, _ in cur.executed]
    rankings_sql = next(s for s in stmts if "FROM rankings" in s)
    assert "HAVING count(*) > 1" in rankings_sql
    assert "GROUP BY promotion_id, division, rank_position, snapshot_date" in rankings_sql
    events_sql = next(s for s in stmts if "FROM events" in s)
    assert "status = 'upcoming'" in events_sql
    assert "NOT EXISTS (SELECT 1 FROM fights f WHERE f.event_id = e.id)" in events_sql


def test_has_critical_only_on_hard_invariants():
    # Duplicate names alone is INFO, not critical.
    assert dqc.has_critical(
        {"duplicate_rank_positions": [], "upcoming_without_bouts": [],
         "duplicate_fighter_names": [{"name": "x", "count": 2, "ids": [1, 2]}]}
    ) is False
    # A duplicate rank slot IS critical.
    assert dqc.has_critical(
        {"duplicate_rank_positions": [{"rank_position": 3}],
         "upcoming_without_bouts": [], "duplicate_fighter_names": []}
    ) is True
    # An empty upcoming event IS critical.
    assert dqc.has_critical(
        {"duplicate_rank_positions": [], "upcoming_without_bouts": [{"id": 1}],
         "duplicate_fighter_names": []}
    ) is True


def test_render_markdown_sections_and_clean_state():
    clean = {"duplicate_rank_positions": [], "upcoming_without_bouts": [],
             "duplicate_fighter_names": []}
    md = dqc._render_markdown(clean)
    assert "rank_position duplicado" in md
    assert "Eventos 'upcoming' SIN peleas" in md
    assert "Nombres de luchador duplicados" in md
    assert "Ninguno" in md

    dirty = {
        "duplicate_rank_positions": [
            {"promotion_id": 1, "division": "LW", "rank_position": 5,
             "snapshot_date": "2026-07-01", "count": 2}
        ],
        "upcoming_without_bouts": [{"id": 900, "name": "Ghost Event", "event_date": "2026-08-01"}],
        "duplicate_fighter_names": [{"name": "john doe", "count": 2, "ids": [1, 2]}],
    }
    md = dqc._render_markdown(dirty)
    assert "Ghost Event" in md
    assert "john doe" in md
    assert "puesto #5" in md


# ------------------------------- tarjetas de jueces con la orientación invertida


def test_las_tarjetas_espejadas_son_criticas(fakedb):
    """Una sola pelea ya dispara la alarma: no hay umbral que valga.

    EL FALLO QUE VIGILA. `fight_scorecards` llegó a tener 2412 de 4020 peleas
    (60 %) con las notas en la esquina equivocada, y la ficha pública las
    pintaba EN COLOR: afirmaba que un juez le había dado el combate al que lo
    perdió. Nadie se enteró porque nada lo miraba.
    """
    fila = (3180, "Lance Gibson Jr.", "King Green", "S-DEC", 2, 1)
    conn = fakedb.Connection(_responder(espejadas=[fila]))
    datos = dqc.collect(conn)

    assert datos["mirrored_scorecards"] == [{
        "fight_id": 3180, "red": "Lance Gibson Jr.", "blue": "King Green",
        "method": "S-DEC", "cards_for_red": 2, "cards_for_blue": 1,
    }]
    assert dqc.has_critical(datos) is True
    assert "3180" in dqc._render_markdown(datos)


def test_sin_espejadas_no_alarma(fakedb):
    conn = fakedb.Connection(_responder())
    datos = dqc.collect(conn)

    assert datos["mirrored_scorecards"] == []
    assert dqc.has_critical(datos) is False
    assert "Ninguna" in dqc._render_markdown(datos)


def test_la_consulta_excluye_las_peleas_sin_ganador(fakedb):
    """Los empates y los anulados NO son un fallo: son indecidibles.

    ufcstats marca 'D'/'D' o 'NC'/'NC' en los dos bloques, así que no hay a qué
    anclar el par y su orden en la fuente es arbitrario. Si la consulta los
    incluyera, la alarma sonaría para siempre por 82 peleas que nadie puede
    arreglar — y una alarma que no se puede apagar deja de mirarse.
    """
    conn = fakedb.Connection(_responder())
    dqc.collect(conn)
    sql = next(
        " ".join(s.split())
        for cur in conn.cursors for s, _p in cur.executed
        if "FROM fight_scorecards" in " ".join(s.split())
    )

    assert "f.winner_id IS NOT NULL" in sql
    assert "t.n_red <> t.n_blue" in sql, "un 1-1-1 no tiene mayoría que comparar"
