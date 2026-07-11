"""T3-A fase B: stats en vivo por pelea (espn_live_stats + live_fight_stats).

Fixtures recortadas de payloads REALES capturados durante UFC 329
(11-jul-2026, scripts/capture_live_samples.py): la core API de statistics
(splits.categories[].stats[]) con la pelea Durden vs Costa en curso, y el
scoreboard con el estado fino (STATUS_END_OF_ROUND / period / displayClock).
Sin red y sin BD real (fakedb + session falsa).

Contrato: la fila viva se PISA en cada pasada (dato vivo, la última lectura
gana) salvo stats NULL entrante (conserva el último JSON conocido); las
peleas con total final guardado (state='post' con stats) no se re-piden; y
nada de esto escribe en fights/fight_stats.
"""

from collections import Counter

from src.scrapers.espn_live_results import parse_scoreboard, process_events
from src.scrapers.espn_live_stats import (
    STAT_KEY_MAP,
    collect_fight_stats,
    parse_stat_values,
)
from src.scrapers.repositories.live_stats import (
    get_final_stats_fight_ids,
    prune_live_fight_stats,
    upsert_live_fight_stats,
)

# ------------------------------------------------------------------ fixtures

# Recorte del payload real comp-401883599-stats-5074121 (Costa, R2 en curso):
# los nombres/formatos son EXACTAMENTE los de la core API (timeInControl en
# segundos: 61.0 == "1:01"; submissions = intentos).
STATS_PAYLOAD = {
    "splits": {
        "id": "0",
        "name": "All Splits",
        "categories": [
            {
                "name": "general",
                "stats": [
                    {"name": "knockDowns", "value": 0.0, "displayValue": "0"},
                    {"name": "totalStrikesAttempted", "value": 52.0, "displayValue": "52"},
                    {"name": "totalStrikesLanded", "value": 23.0, "displayValue": "23"},
                    {"name": "sigStrikesAttempted", "value": 47.0, "displayValue": "47"},
                    {"name": "sigStrikesLanded", "value": 18.0, "displayValue": "18"},
                    {"name": "takedownsAttempted", "value": 0.0, "displayValue": "0"},
                    {"name": "takedownsLanded", "value": 0.0, "displayValue": "0"},
                    {"name": "submissions", "value": 2.0, "displayValue": "2"},
                    {"name": "timeInControl", "value": 61.0, "displayValue": "1:01"},
                    # Desglose posición×objetivo: el panel (como el de ESPN)
                    # lo muestra SUMADO por objetivo (distancia+clinch+suelo).
                    {"name": "sigDistanceHeadStrikesLanded", "value": 9.0, "displayValue": "9"},
                    {"name": "sigDistanceHeadStrikesAttempted", "value": 30.0, "displayValue": "30"},
                    {"name": "sigClinchHeadStrikesLanded", "value": 2.0, "displayValue": "2"},
                    {"name": "sigClinchHeadStrikesAttempted", "value": 3.0, "displayValue": "3"},
                    {"name": "sigGroundHeadStrikesLanded", "value": 4.0, "displayValue": "4"},
                    {"name": "sigGroundHeadStrikesAttempted", "value": 3.0, "displayValue": "3"},
                    {"name": "sigDistanceBodyStrikesLanded", "value": 2.0, "displayValue": "2"},
                    {"name": "sigDistanceBodyStrikesAttempted", "value": 4.0, "displayValue": "4"},
                    {"name": "sigDistanceLegStrikesLanded", "value": 1.0, "displayValue": "1"},
                    {"name": "sigDistanceLegStrikesAttempted", "value": 4.0, "displayValue": "4"},
                    # Métrica que NO almacenamos: debe ignorarse sin ruido.
                    {"name": "slamRate", "value": 0.0, "displayValue": "0"},
                ],
            }
        ],
    }
}

EXPECTED_COMPACT = {
    "kd": 0, "tsa": 52, "tsl": 23, "ssa": 47, "ssl": 18,
    "tda": 0, "tdl": 0, "sub": 2, "ctrl": 61,
    # Sumas por objetivo (head 9+2+4 / 30+3+3; body y leg solo distancia).
    "hl": 15, "ha": 36, "bl": 2, "ba": 4, "ll": 1, "la": 4,
}


class _FakeResponse:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    """Session falsa: devuelve payloads por athlete_id y registra las URLs."""

    def __init__(self, by_athlete):
        self.by_athlete = by_athlete
        self.requested = []

    def get(self, url, timeout=None, params=None):
        self.requested.append(url)
        for athlete_id, payload in self.by_athlete.items():
            if f"/competitors/{athlete_id}/statistics" in url:
                return _FakeResponse(payload)
        return _FakeResponse(None, ok=False)


# ------------------------------------------------------------------- parsing


def test_parse_stat_values_extracts_the_compact_subset():
    assert parse_stat_values(STATS_PAYLOAD) == EXPECTED_COMPACT


def test_parse_stat_values_is_defensive():
    assert parse_stat_values(None) is None
    assert parse_stat_values({}) is None
    assert parse_stat_values({"splits": {}}) is None
    assert parse_stat_values({"splits": {"categories": []}}) is None
    # Valores no numéricos se saltan; si no queda nada usable -> None.
    broken = {"splits": {"categories": [{"stats": [{"name": "knockDowns", "value": "x"}]}]}}
    assert parse_stat_values(broken) is None


def test_stat_key_map_matches_the_app_contract():
    # Las claves compactas que la web mapea (mma-app live-stats): no renombrar
    # sin tocar ambos lados. Las derivadas hl/ha/bl/ba/ll/la salen de
    # _TARGET_SUM_KEYS (desglose por objetivo estilo ESPN).
    assert set(STAT_KEY_MAP.values()) == {
        "kd", "tsl", "tsa", "ssl", "ssa", "tdl", "tda", "sub", "ctrl"
    }
    from src.scrapers.espn_live_stats import _TARGET_SUM_KEYS

    assert set(_TARGET_SUM_KEYS.keys()) == {"hl", "ha", "bl", "ba", "ll", "la"}


def test_collect_fight_stats_keys_by_our_fighter_ids():
    session = _FakeSession({"4686725": STATS_PAYLOAD, "5074121": STATS_PAYLOAD})
    stats = collect_fight_stats(
        session, "600059148", "401883599",
        [("4686725", 201), ("5074121", 202)],
    )
    assert set(stats.keys()) == {"201", "202"}
    assert stats["201"] == EXPECTED_COMPACT
    # URL de la core API con event/comp/athlete correctos.
    assert any(
        "events/600059148/competitions/401883599/competitors/4686725/statistics" in url
        for url in session.requested
    )


def test_collect_fight_stats_partial_and_empty_sides():
    # Solo un lado servido -> solo esa clave; ninguno -> None (el UPSERT
    # conserva el último JSON conocido vía COALESCE).
    session = _FakeSession({"4686725": STATS_PAYLOAD})
    stats = collect_fight_stats(
        session, "600059148", "401883599", [("4686725", 201), ("5074121", 202)]
    )
    assert set(stats.keys()) == {"201"}
    none_session = _FakeSession({})
    assert collect_fight_stats(
        none_session, "600059148", "401883599", [("4686725", 201), (None, 202)]
    ) is None


# --------------------------------------------------------------- repositorio


def test_upsert_live_fight_stats_overwrites_but_keeps_last_stats(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    upsert_live_fight_stats(
        conn, 11, "in", "STATUS_END_OF_ROUND", "End R2", 2, "2:37",
        {"201": EXPECTED_COMPACT},
    )
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "INSERT INTO live_fight_stats" in flat
    assert "ON CONFLICT (fight_id) DO UPDATE" in flat
    # Dato VIVO: la última lectura pisa estado/asalto/reloj...
    assert "state = EXCLUDED.state" in flat
    assert "period = EXCLUDED.period" in flat
    # ...pero un fallo puntual del endpoint (stats NULL) no borra lo mostrado.
    assert "stats = COALESCE(EXCLUDED.stats, live_fight_stats.stats)" in flat
    assert params[0] == 11 and params[1] == "in"
    assert '"ctrl": 61' in params[6]


def test_upsert_live_fight_stats_null_stats_written_as_sql_null(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    upsert_live_fight_stats(conn, 11, "in", None, "Walkouts", 0, "-", None)
    _, params = conn.cursors[0].executed[0]
    assert params[6] is None


def test_get_final_stats_fight_ids_only_post_with_stats(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(11,), (13,)])
    assert get_final_stats_fight_ids(conn, [11, 12, 13]) == {11, 13}
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # 'post' sin stats NO es final: se reintenta en la siguiente pasada.
    assert "state = 'post'" in flat and "stats IS NOT NULL" in flat
    assert params == ([11, 12, 13],)
    assert get_final_stats_fight_ids(conn, []) == set()


def test_prune_live_fight_stats_deletes_by_age(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    prune_live_fight_stats(conn, older_than_hours=48)
    sql, params = conn.cursors[0].executed[0]
    assert "DELETE FROM live_fight_stats" in sql
    assert params == (48,)


# ------------------------------------------------------- cableado del bucle


def _scoreboard_with_one_live_fight():
    """Evento con 1 pelea en curso (End R2) y 1 terminada, ids reales."""
    return {
        "events": [
            {
                "id": "600059148",
                "name": "UFC 329: McGregor vs. Holloway 2",
                "date": "2026-07-11T21:00Z",
                "competitions": [
                    {
                        "id": "401883599",
                        "competitors": [
                            {"id": "4686725", "winner": False,
                             "athlete": {"displayName": "Cody Durden"}},
                            {"id": "5074121", "winner": False,
                             "athlete": {"displayName": "Alessandro Costa"}},
                        ],
                        "status": {
                            "clock": 157.0, "displayClock": "2:37", "period": 2,
                            "type": {"name": "STATUS_END_OF_ROUND", "state": "in",
                                     "completed": False, "description": "End of Round",
                                     "detail": "End R2", "shortDetail": "End R2"},
                        },
                        "details": [],
                    },
                    {
                        "id": "401883600",
                        "competitors": [
                            {"id": "3155424", "winner": True,
                             "athlete": {"displayName": "Winner Person"}},
                            {"id": "3155425", "winner": False,
                             "athlete": {"displayName": "Loser Person"}},
                        ],
                        "status": {
                            "clock": 0.0, "displayClock": "2:19", "period": 2,
                            "type": {"name": "STATUS_FINAL", "state": "post",
                                     "completed": True, "description": "Final",
                                     "detail": "Final", "shortDetail": "Final"},
                        },
                        "details": [{"id": "1", "type": {"id": "9", "text": "Unofficial Winner Submission"}}],
                    },
                ],
            }
        ]
    }


ESPN_TO_DB = {"4686725": 201, "5074121": 202, "3155424": 203, "3155425": 204}
FIGHT_ID_BY_PAIR = {frozenset({201, 202}): 11, frozenset({203, 204}): 12}


def _responder(final_ids_rows):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT id, name, nickname"):
            return [
                (db_id, name, None, None, None, None, None, None, None)
                for db_id, name in [
                    (201, "Cody Durden"), (202, "Alessandro Costa"),
                    (203, "Winner Person"), (204, "Loser Person"),
                ]
            ]
        if "FROM fighters WHERE source" in flat:
            db_id = ESPN_TO_DB.get(params[1])
            return [(db_id,)] if db_id else []
        if flat.startswith("SELECT id FROM events"):
            return [(5,)]
        if "FROM fights WHERE event_id" in flat:
            pair = frozenset({params[1], params[2]})
            fight_id = FIGHT_ID_BY_PAIR.get(pair)
            return [(fight_id,)] if fight_id else []
        if "FROM live_fight_stats" in flat:
            return final_ids_rows
        if flat.startswith("UPDATE fights"):
            return [(1,)]
        return []

    return responder


def test_process_events_writes_live_rows_for_in_and_post(fakedb):
    conn = fakedb.Connection(_responder(final_ids_rows=[]))
    session = _FakeSession({aid: STATS_PAYLOAD for aid in ESPN_TO_DB})
    events = parse_scoreboard(_scoreboard_with_one_live_fight())
    counts = process_events(conn, events, promotion_id=1, dry_run=False, stats_session=session)
    # Resultado de la terminada + 2 filas vivas (in + post).
    assert counts["fights_updated"] == 1
    assert counts["live_stats_written"] == 2
    inserts = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "INSERT INTO live_fight_stats" in sql
    ]
    assert len(inserts) == 2
    by_fight = {params[0]: params for _, params in inserts}
    assert by_fight[11][1] == "in" and by_fight[11][3] == "End R2" and by_fight[11][4] == 2
    assert by_fight[12][1] == "post"
    # Stats con claves = NUESTROS fighter ids.
    assert '"201"' in by_fight[11][6] and '"202"' in by_fight[11][6]
    assert conn.commits >= 1


def test_process_events_skips_fights_with_final_stats(fakedb):
    # La pelea 12 ya tiene su total final guardado -> no se re-pide ni re-escribe.
    conn = fakedb.Connection(_responder(final_ids_rows=[(12,)]))
    session = _FakeSession({aid: STATS_PAYLOAD for aid in ESPN_TO_DB})
    events = parse_scoreboard(_scoreboard_with_one_live_fight())
    counts = process_events(conn, events, promotion_id=1, dry_run=False, stats_session=session)
    assert counts["live_stats_written"] == 1
    assert counts["live_stats_skipped_final"] == 1
    assert not any(
        "/competitors/3155424/statistics" in url or "/competitors/3155425/statistics" in url
        for url in session.requested
    )


def test_process_events_dry_run_writes_no_live_rows(fakedb):
    conn = fakedb.Connection(_responder(final_ids_rows=[]))
    session = _FakeSession({aid: STATS_PAYLOAD for aid in ESPN_TO_DB})
    events = parse_scoreboard(_scoreboard_with_one_live_fight())
    counts = process_events(conn, events, promotion_id=1, dry_run=True, stats_session=session)
    assert counts["live_stats_written"] == 2
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_process_events_without_session_behaves_like_before(fakedb):
    # Compat: sin stats_session (tests antiguos, llamadas legacy) no hay ni
    # fetch ni fila viva — el módulo se comporta exactamente como la fase A.
    conn = fakedb.Connection(_responder(final_ids_rows=[]))
    events = parse_scoreboard(_scoreboard_with_one_live_fight())
    counts = process_events(conn, events, promotion_id=1, dry_run=False)
    assert counts["live_stats_written"] == 0
    assert not any(
        "INSERT INTO live_fight_stats" in sql
        for cur in conn.cursors
        for sql, params in cur.executed
    )


def test_parse_scoreboard_carries_fine_status():
    events = parse_scoreboard(_scoreboard_with_one_live_fight())
    live = events[0].fights[0]
    assert live.status_name == "STATUS_END_OF_ROUND"
    assert live.status_detail == "End R2"
    assert (live.end_round, live.end_time) == (2, "2:37")
