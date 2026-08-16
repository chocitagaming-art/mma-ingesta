"""`repair_incomplete_round_stats` no toca la BD sin `--apply`, y NUNCA con la
página de ufcstats a medias.

Las dos cosas que este script puede hacer mal son caras y opuestas:

  1. Escribir creyendo que simula. Por eso la simulación se comprueba a nivel de
     SQL: cero sentencias mutantes, cero commits, y la sesión abierta en
     `readonly=True` (en la vida real es Postgres quien rechaza la escritura).
  2. Borrar las 2 filas del R1 que SÍ son buenas y no poder reescribir nada,
     porque ufcstats sigue publicando solo el R1 — exactamente el fallo que este
     script viene a reparar. Por eso el segundo test sirve una página incompleta
     y exige que no salga ni un DELETE, y que el proceso termine en rojo.

Todo el parseo y la red van mockeados: los parsers devuelven registros ya
construidos y el cliente HTTP no abre ningún socket. Lo que se ejercita de
verdad es el emparejamiento (`_match_event`/`_match_fight`/`fighter_id_for`), la
validación y las guardas.
"""

import dataclasses
import types
from datetime import date

import pytest

from scripts import repair_incomplete_round_stats as mod
from src.scrapers.parsers.fights import (
    FightPageRecord,
    ParsedFightStats,
    ParsedRoundStats,
)

FIGHT_ID = 14895
EVENT_ID = 1064
EVENT_NAME = "UFC 330: Makhachev vs. Machado Garry"
EVENT_DATE = date(2026, 8, 15)
RED_ID, RED_NAME = 6956, "Chidi Njokuani"
BLUE_ID, BLUE_NAME = 6511, "Joel Alvarez"
STALE_ROUND_IDS = [41447, 41448]

# Lo que ufcstats sirve HOY (verificado el 2026-08-16): por asalto y totales.
POR_ASALTO = {
    (1, RED_NAME): (46, 69),
    (1, BLUE_NAME): (22, 50),
    (2, RED_NAME): (48, 70),
    (2, BLUE_NAME): (25, 52),
    (3, RED_NAME): (61, 90),
    (3, BLUE_NAME): (27, 62),
}
TOTALES = {RED_NAME: (155, 229), BLUE_NAME: (74, 164)}


def _fila_totales(fighter_id, valores):
    fila = dict.fromkeys(mod.TOTAL_COLUMNS, 0)
    fila.update(valores)
    return (fighter_id,) + tuple(fila[columna] for columna in mod.TOTAL_COLUMNS)


def _fila_ronda(row_id, ronda, fighter_id, valores):
    fila = dict.fromkeys(mod.ROUND_COLUMNS, 0)
    fila.update(valores)
    return (row_id, ronda, fighter_id) + tuple(fila[columna] for columna in mod.ROUND_COLUMNS)


def _responder(sql, params=None):
    """Estado real del 14895 el 2026-08-16: 2 filas de R1 y totales del R1.

    Cada rama se reconoce por algo EXCLUSIVO de esa consulta: `_get_bouts`
    también nombra `fight_stats_rounds` y `fight_stats` en sus subconsultas, y
    con marcadores laxos acaba contestando con filas de otra tabla.
    """
    plano = " ".join(sql.split())
    if "DELETE FROM fight_stats_rounds" in plano:
        return [(row_id,) for row_id in STALE_ROUND_IDS]
    if "SELECT COUNT(*), COUNT(DISTINCT round)" in plano:
        return [(6, 3)]
    if "ARRAY_AGG(DISTINCT fsr.round" in plano:  # el detector de candidatos
        return [(
            FIGHT_ID, EVENT_ID, EVENT_NAME, EVENT_DATE, "U-DEC", 3,
            RED_ID, BLUE_ID, RED_NAME, BLUE_NAME, 2, [1],
        )]
    if "SELECT fsr.id, fsr.round" in plano:
        return [
            _fila_ronda(41447, 1, RED_ID, {"sig_strikes_landed": 46, "sig_strikes_attempted": 69}),
            _fila_ronda(41448, 1, BLUE_ID, {"sig_strikes_landed": 22, "sig_strikes_attempted": 50}),
        ]
    if "SELECT fs.fighter_id" in plano:
        return [
            _fila_totales(RED_ID, {"sig_strikes_landed": 46, "sig_strikes_attempted": 69}),
            _fila_totales(BLUE_ID, {"sig_strikes_landed": 22, "sig_strikes_attempted": 50}),
        ]
    if "FROM fights fi" in plano:  # _get_bouts
        return [(
            FIGHT_ID, RED_ID, BLUE_ID, RED_NAME, BLUE_NAME, "U-DEC",
            True, False, "Keith Peterson", True, "/fighter-details/aaa", "/fighter-details/bbb",
        )]
    return []


def _pagina_del_evento():
    return [FightPageRecord(
        red_name=RED_NAME,
        blue_name=BLUE_NAME,
        red_source_id="/fighter-details/aaa",
        blue_source_id="/fighter-details/bbb",
        weight_class="Welterweight",
        scheduled_rounds=3,
        winner_corner="red",
        method="U-DEC",
        end_round=3,
        end_time="5:00",
        detail_url="http://ufcstats.com/fight-details/6e495deac1f57169",
        source_id="6e495deac1f57169",
    )]


def _rondas_parseadas(rondas):
    return [
        ParsedRoundStats(
            fighter_source_id=None,
            fighter_name=nombre,
            round=ronda,
            stats={
                "sig_strikes_landed": landed,
                "sig_strikes_attempted": attempted,
                "takedowns_landed": 0,
                "takedowns_attempted": 0,
                "submission_attempts": 0,
                "control_time_seconds": 0,
                "knockdowns": 0,
            },
        )
        for (ronda, nombre), (landed, attempted) in POR_ASALTO.items()
        if ronda in rondas
    ]


def _totales_parseados(por_asalto):
    """Totales coherentes con los asaltos servidos.

    Es lo que hace ufcstats de verdad —y la raíz del fallo—: los totales de la
    página son la SUMA de las filas por asalto que tenga publicadas, así que con
    solo el R1 publicado los totales SON los del R1.
    """
    salida = []
    for nombre in (RED_NAME, BLUE_NAME):
        filas = [r for r in por_asalto if r.fighter_name == nombre]
        stats = dict.fromkeys(mod.TOTAL_COLUMNS, 0)
        stats["sig_strikes_landed"] = sum(f.stats["sig_strikes_landed"] for f in filas)
        stats["sig_strikes_attempted"] = sum(f.stats["sig_strikes_attempted"] for f in filas)
        salida.append(ParsedFightStats(fighter_source_id=None, fighter_name=nombre, stats=stats))
    return salida


def _montar(monkeypatch, fakedb, rondas):
    """Deja el módulo listo con una página de ufcstats que sirve `rondas`."""
    conn = fakedb.Connection(_responder)
    aperturas = []

    def _abrir(dsn, readonly):
        aperturas.append((dsn, readonly))
        return conn

    monkeypatch.setattr(mod, "_open_connection", _abrir)
    monkeypatch.setattr(
        mod, "get_settings",
        lambda: types.SimpleNamespace(database_url="x", user_agent="agente ufcstats.com"),
    )
    monkeypatch.setattr(
        mod, "UfcStatsClient",
        lambda settings: types.SimpleNamespace(fetch=lambda url: types.SimpleNamespace(soup=object())),
    )
    monkeypatch.setattr(
        mod, "parse_events_index",
        lambda soup, settings: [types.SimpleNamespace(
            event=types.SimpleNamespace(event_date=EVENT_DATE, name=EVENT_NAME),
            detail_url="http://ufcstats.com/event-details/b96619b3acd7d9da",
        )],
    )
    monkeypatch.setattr(mod, "parse_event_fights", lambda soup, settings: _pagina_del_evento())
    parseadas = _rondas_parseadas(rondas)
    monkeypatch.setattr(mod, "parse_fight_stats_rounds", lambda soup: parseadas)
    monkeypatch.setattr(mod, "parse_fight_stats", lambda soup: _totales_parseados(parseadas))
    return conn, aperturas


def test_la_simulacion_no_escribe_nada(monkeypatch, fakedb, capsys):
    """Sin --apply: ni DELETE ni INSERT ni commit, y la sesión en readonly."""
    conn, aperturas = _montar(monkeypatch, fakedb, rondas={1, 2, 3})
    monkeypatch.setattr("sys.argv", ["repair_incomplete_round_stats", "--fight-id", str(FIGHT_ID)])

    mod.main()

    assert aperturas == [("x", True)], "la sesión de simulación tiene que ser readonly"
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    salida = capsys.readouterr().out
    assert "DRY-RUN — nothing written" in salida
    assert "Would repair 1 fight(s): [14895]" in salida
    # El plan que se le enseña al humano lleva los números buenos, no los del R1.
    assert "155/229" in salida and "74/164" in salida


def test_pagina_incompleta_no_borra_nada(monkeypatch, fakedb, capsys):
    """ufcstats sirviendo solo el R1 (el fallo original) no dispara ningún borrado."""
    conn, _ = _montar(monkeypatch, fakedb, rondas={1})
    monkeypatch.setattr("sys.argv", ["repair_incomplete_round_stats", "--fight-id", str(FIGHT_ID)])

    with pytest.raises(SystemExit) as salida_error:
        mod.main()

    assert salida_error.value.code == 1
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    salida = capsys.readouterr().out
    assert "SKIP" in salida
    assert "still publishes rounds [1]" in salida
    assert "Cannot repair 1 fight(s): [14895]" in salida


def test_con_apply_borra_las_viejas_y_reescribe_todo(monkeypatch, fakedb, capsys):
    """Con --apply: un DELETE de las 2 filas rancias, 6+2 upserts y UN commit."""
    conn, aperturas = _montar(monkeypatch, fakedb, rondas={1, 2, 3})
    monkeypatch.setattr(
        "sys.argv",
        ["repair_incomplete_round_stats", "--fight-id", str(FIGHT_ID), "--apply"],
    )

    mod.main()

    assert aperturas == [("x", False)], "en modo --apply la sesión NO puede ser readonly"
    mutantes = fakedb.mutating_statements(conn)
    borrados = [s for s in mutantes if s.startswith("DELETE FROM fight_stats_rounds")]
    rondas = [s for s in mutantes if "INSERT INTO fight_stats_rounds" in s]
    totales = [s for s in mutantes if "INSERT INTO fight_stats (" in s]
    assert len(borrados) == 1
    assert len(rondas) == 6, "3 asaltos x 2 luchadores"
    assert len(totales) == 2
    assert borrados[0].endswith("RETURNING id")
    assert conn.commits == 1
    assert "APPLIED: fight 14895" in capsys.readouterr().out


def test_no_escribe_stats_de_un_luchador_ajeno_al_combate(monkeypatch, fakedb, capsys):
    """El respaldo por `source_id` no puede sacar a alguien de FUERA del combate.

    `get_fighter_id_by_source` busca en toda la tabla `fighters`, donde hoy hay
    un par de nombres duplicados. Si el nombre de la página no casa con ninguna
    esquina y el id que vuelve no es el de este combate, resolverlo plantaría
    las stats de la pelea sobre alguien que no la disputó — y este script BORRA
    las filas buenas antes de reescribir. Tiene que negarse y no tocar nada.
    """
    conn, _ = _montar(monkeypatch, fakedb, rondas={1, 2, 3})
    # La página sirve dos homónimos que no casan con ninguna esquina del bout,
    # pero cuyo source_id SÍ resuelve a sendas filas de `fighters`.
    ajenos = {
        RED_NAME: ("Ilia Topuria", "/fighter-details/zzz"),
        BLUE_NAME: ("Max Holloway", "/fighter-details/yyy"),
    }

    def _renombrar(registros):
        return [
            dataclasses.replace(
                registro,
                fighter_name=ajenos[registro.fighter_name][0],
                fighter_source_id=ajenos[registro.fighter_name][1],
            )
            for registro in registros
        ]

    parseadas = _rondas_parseadas({1, 2, 3})
    monkeypatch.setattr(mod, "parse_fight_stats_rounds", lambda soup: _renombrar(parseadas))
    monkeypatch.setattr(
        mod, "parse_fight_stats", lambda soup: _renombrar(_totales_parseados(parseadas))
    )
    monkeypatch.setattr("sys.argv", ["repair_incomplete_round_stats", "--fight-id", str(FIGHT_ID)])

    def _responder_con_homonimos(sql, params=None):
        if "FROM fighters WHERE source" in " ".join(sql.split()):
            return [(9998,)] if params[1] == "/fighter-details/zzz" else [(9999,)]
        return _responder(sql, params)

    conn._responder = _responder_con_homonimos

    with pytest.raises(SystemExit) as salida_error:
        mod.main()

    assert salida_error.value.code == 1
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    salida = capsys.readouterr().out
    assert "cannot map round-stats fighter" in salida
    assert "Cannot repair 1 fight(s): [14895]" in salida


def test_la_guarda_del_borrado_hace_rollback(monkeypatch, fakedb, capsys):
    """Si el DELETE se lleva filas que no estaban en el plan, no se confirma nada."""
    conn, _ = _montar(monkeypatch, fakedb, rondas={1, 2, 3})
    monkeypatch.setattr(
        "sys.argv",
        ["repair_incomplete_round_stats", "--fight-id", str(FIGHT_ID), "--apply"],
    )

    def _responder_con_fila_extra(sql, params=None):
        plano = " ".join(sql.split())
        if "DELETE FROM fight_stats_rounds" in plano:
            return [(41447,), (41448,), (99999,)]  # alguien insertó por debajo
        return _responder(sql, params)

    conn._responder = _responder_con_fila_extra

    with pytest.raises(SystemExit) as salida_error:
        mod.main()

    assert "GUARD TRIPPED" in str(salida_error.value.code)
    assert conn.commits == 0
    assert conn.rollbacks == 1
