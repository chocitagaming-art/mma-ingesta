"""Weigh-in (pesaje) pass of match_faceoffs: the strict title rule, the event
hint, the window, the duration net, the ambiguity guard, the guarded write and
the run pass. No network, no DB (fakedb recorder).

Every title here is REAL unless the test says otherwise. The fixture
tests/fixtures/weighin_uploads_real.json holds the 77 uploads of the official
UFC channel (UCvgfXK4nTYKudb0rFR6noLA) with "weigh" in the title between
17-may and 17-oct-2026, the real `events` rows of the 23 cards in that span and
the real videos.list durations (read 18-oct-2026).

The new names are looked up on the module (mf.match_weighin, ...) instead of
imported at the top ON PURPOSE: against the code before this pass each test
fails on its own, so the red run counts tests, not one collection error.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from src.scrapers import match_faceoffs as mf
from src.scrapers.match_faceoffs import FeedVideo, TargetEvent
from src.scrapers.repositories import events as events_repo

FIXTURE = Path(__file__).parent / "fixtures" / "weighin_uploads_real.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
UPLOADS = [
    FeedVideo(u["id"], u["title"], date.fromisoformat(u["published"]))
    for u in DATA["uploads"]
]
DURATIONS: dict[str, int] = DATA["durations"]
EVENTS = {
    e["id"]: TargetEvent(
        e["id"], e["name"], e["location"], date.fromisoformat(e["event_date"])
    )
    for e in DATA["events"]
}


def _match(event, videos=None, durations=None):
    """Video id the weigh-in rule picks for ``event`` (id or TargetEvent)."""
    if isinstance(event, int):
        event = EVENTS[event]
    hit = mf.match_weighin(
        event,
        UPLOADS if videos is None else videos,
        DURATIONS if durations is None else durations,
    )
    return hit.video_id if hit else None


def _v(video_id, title, day):
    return FeedVideo(video_id, title, day)


# ------------------------------------------------------------------ casar


def test_casa_el_pesaje_del_1098():
    hit = mf.match_weighin(EVENTS[1098], UPLOADS, DURATIONS)
    assert hit is not None
    assert hit.video_id == "G-XGRj5qe4c"
    # The title travels with the match: it is what the page prints under the
    # player, so it has to be the one YouTube published, not a made-up label.
    assert hit.title == "Allen vs Duncan Weigh-Ins | UFC Vegas 122"


def test_casa_una_numerada_por_el_numero():
    # 1092 also matches by surname (Silva, Wang). Strip the name down to the
    # number and drop the location: the "ufc 332" token alone must carry it.
    assert _match(1092) == "12kk1GE8ELg"
    solo_numero = TargetEvent(1092, "UFC 332", None, date(2026, 10, 3))
    assert _match(solo_numero) == "12kk1GE8ELg"


def test_casa_noche_ufc_por_la_marca():
    assert _match(1088) == "Hh9icPderiM"
    solo_marca = TargetEvent(1088, "Noche UFC", None, date(2026, 9, 12))
    assert _match(solo_marca) == "Hh9icPderiM"


def test_casa_por_apellido_cuando_la_ciudad_no_sale():
    # 1065: the location says Pudong, the title says Shanghai. Only the
    # main-event surname "nurmagomedov" links them.
    assert _match(1065) == "EWtcsj7jMr8"
    sin_apellidos = TargetEvent(
        1065,
        "UFC Fight Night",
        EVENTS[1065].location,
        EVENTS[1065].event_date,
    )
    assert _match(sin_apellidos) is None, "without the surname nothing ties it"


def test_casa_freedom_250_sin_vs_en_el_nombre():
    assert _match(1084) == "vlFDAtiz1EQ"
    assert mf._bout_tokens("UFC Freedom 250") == set()


def test_bout_tokens_saca_los_apellidos_del_estelar():
    assert mf._bout_tokens("UFC Fight Night: Buckley vs. Malott") == {"buckley", "malott"}
    assert mf._bout_tokens("UFC Fight Night: Rosas Jr. vs. Barcelos") == {"rosas", "barcelos"}
    # Digits never become a token ("2" of a rematch, card numbers).
    assert mf._bout_tokens("Crypto.com UFC 331: Van vs. Pantoja 2") == {"van", "pantoja"}
    assert mf._bout_tokens(None) == set()


def test_1089_edmonton_casa_con_el_titulo_real():
    # REAL (15-oct the owner set it by hand; 18-oct read from the API):
    # EWTYRIk_pcw "Buckley vs Malott Weigh-Ins | UFC Edmonton", 304 s, 16-oct.
    # The fixture also holds, in the same window, the ceremonial IXSay7YbTCM
    # (17-oct, 4055 s) and the short Ra_28E_kJFI "Buckley makes weight! #UFC
    # #MMA #UFCEdmonton" (16-oct, 20 s). Only the official one may come out.
    assert _match(1089) == "EWTYRIk_pcw"


def test_1093_casaria_con_el_formato_esperado():
    # SIMULATED: UFC 333 (Abu Dhabi, 24-oct) has not weighed in yet. The title
    # follows the format the channel used for the 19 real cards.
    esperado = _v("ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333", date(2026, 10, 23))
    assert _match(1093, UPLOADS + [esperado]) == "ufc333wi"
    # And it can never be taken by the Edmonton card a week earlier.
    adelantado = _v("ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333", date(2026, 10, 16))
    assert _match(1089, UPLOADS + [adelantado]) == "EWTYRIk_pcw"


# --------------------------------------------------------------- rechazar


def test_rechaza_el_ceremonial():
    # The page labels it "Pesaje oficial": a ceremonial must never get there.
    real = _v("IXSay7YbTCM", "UFC Edmonton: Ceremonial Weigh-In", date(2026, 10, 16))
    assert _match(1089, [real], {}) is None
    # SIMULATED, with the strict shape: only the exclusion stops it.
    forma = _v("cer", "Buckley vs Malott Ceremonial Weigh-Ins | UFC Edmonton", date(2026, 10, 16))
    assert _match(1089, [forma], {}) is None


def test_rechaza_el_morning_weigh_in_show():
    real = _v("iY5UU2SvSlY", "UFC 332: Morning Weigh-In Show", date(2026, 10, 2))
    assert _match(1092, [real], {}) is None
    forma = _v("show", "Buckley vs Malott Weigh-Ins | UFC Edmonton Morning Show", date(2026, 10, 16))
    assert _match(1089, [forma], {}) is None


def test_rechaza_los_shorts_con_almohadilla():
    figgy = _v("KxLSbe4eG1w", "Figgy and Talbott have made weight! #ufc332", date(2026, 10, 2))
    assert _match(1092, [figgy], {}) is None
    gamrot = _v("UH5PHCY2tBM", "Salkilld and Gamrot weigh in! #ufcvegas120", date(2026, 8, 7))
    assert _match(1087, [gamrot], {}) is None
    buckley = _v("Ra_28E_kJFI", "Buckley makes weight! #UFC #MMA #UFCEdmonton", date(2026, 10, 16))
    assert _match(1089, [buckley], {}) is None
    # SIMULATED: the strict shape with a hashtag glued on is still a short.
    forma = _v("hash", "Buckley vs Malott Weigh-Ins | UFC Edmonton #ufcedmonton", date(2026, 10, 16))
    assert _match(1089, [forma], {}) is None


def test_rechaza_los_careos():
    careo = _v("c1", "UFC Vegas 122: Fighter Face-offs", date(2026, 10, 9))
    assert _match(1098, [careo], {}) is None
    noche = _v("qpjPPGtcS3I", "Noche UFC: Weigh-In Faceoffs", date(2026, 9, 11))
    assert _match(1088, [noche], {}) is None
    forma = _v("c2", "Allen vs Duncan Face-offs Weigh-Ins | UFC Vegas 122", date(2026, 10, 9))
    assert _match(1098, [forma], {}) is None


def test_rechaza_el_boxeo_de_la_misma_ventana():
    # REAL: "Garcia vs Benn: Ceremonial Weigh-In" fell inside the Noche UFC
    # window (12-sep).
    real = _v("35QMmEPfbx8", "Garcia vs Benn: Ceremonial Weigh-In", date(2026, 9, 12))
    assert _match(1088, [real], {}) is None
    # SIMULATED, built so ONLY the "ufc in the card" check can stop it: the
    # strict shape, and "vegas" is a real hint of the APEX card 1098.
    boxeo = _v("box", "Garcia vs Benn Weigh-Ins | Zuffa Boxing Las Vegas", date(2026, 10, 9))
    assert _match(1098, [boxeo], {}) is None
    # Next to the real one it is not even a second candidate (no "ambiguous").
    assert _match(1098, UPLOADS + [boxeo]) == "G-XGRj5qe4c"


def test_rechaza_el_pesaje_de_otra_velada():
    otra = _v("BLM5aLbYIvM", "Gamrot vs Salkilld Weigh-Ins | UFC Vegas 120", date(2026, 10, 16))
    assert _match(1089, [otra], {}) is None


def test_rechaza_fuera_de_ventana():
    titulo = "Allen vs Duncan Weigh-Ins | UFC Vegas 122"  # event 2026-10-10
    for dia, esperado in (
        (date(2026, 10, 7), None),  # d-3
        (date(2026, 10, 8), "w"),  # d-2
        (date(2026, 10, 9), "w"),  # d-1 (the real one, all 19 cards)
        (date(2026, 10, 10), "w"),  # d0
        (date(2026, 10, 11), None),  # d+1: the weigh-in is never after the card
    ):
        assert _match(1098, [_v("w", titulo, dia)], {}) == esperado, dia
    assert mf._WEIGHIN_DAYS_BEFORE == 2
    assert mf._WEIGHIN_DAYS_AFTER == 0


def test_rechaza_el_formato_de_terceros():
    # REAL title of what was picked by hand for 1090 (TheMacLife channel).
    # Only the UFC's own "<A> vs <B> Weigh-Ins | <card>" shape is accepted.
    tercero = _v("enkyfSnB0r0", "UFC 331: Official Weigh-Ins", date(2026, 9, 18))
    assert _match(1090, [tercero], {}) is None
    assert _match(1090) == "k1968bd6pTg"


def test_duracion_conocida_corta_rechaza():
    video = _v("EWTYRIk_pcw", "Buckley vs Malott Weigh-Ins | UFC Edmonton", date(2026, 10, 16))
    assert _match(1089, [video], {"EWTYRIk_pcw": 120}) is None
    assert _match(1089, [video], {"EWTYRIk_pcw": mf.MIN_WEIGHIN_SECONDS}) == "EWTYRIk_pcw"
    # The shortest real one (1089, 304 s) keeps a wide margin over the cut.
    assert mf.MIN_WEIGHIN_SECONDS == 180
    assert min(DURATIONS[v] for v in DATA["expected"].values() if v) == 304


def test_duracion_desconocida_no_bloquea():
    # Fail-open, same reasoning as _duration_ok: no key / failed videos.list
    # must not empty every event.
    video = _v("EWTYRIk_pcw", "Buckley vs Malott Weigh-Ins | UFC Edmonton", date(2026, 10, 16))
    assert _match(1089, [video], {}) == "EWTYRIk_pcw"
    assert mf.match_weighin(EVENTS[1089], [video], None).video_id == "EWTYRIk_pcw"


def test_directo_programado_P0D_rechaza():
    # A scheduled live reports "P0D" -> 0 s.
    assert mf.parse_iso8601_duration("P0D") == 0
    video = _v("live", "Buckley vs Malott Weigh-Ins | UFC Edmonton", date(2026, 10, 16))
    assert _match(1089, [video], {"live": 0}) is None


def test_ambiguo_no_escribe_nada(caplog):
    uno = _v("a1", "Allen vs Duncan Weigh-Ins | UFC Vegas 122", date(2026, 10, 9))
    dos = _v("a2", "Allen vs Duncan Weigh-Ins | UFC Vegas 122 (Full)", date(2026, 10, 9))
    with caplog.at_level("WARNING"):
        assert _match(1098, [uno, dos], {}) is None
    assert any("ambiguo" in r.getMessage() for r in caplog.records)


def test_el_mismo_video_en_los_dos_feeds_no_es_ambiguo():
    # RSS + API carry the same upload twice: one candidate, not two.
    uno = _v("a1", "Allen vs Duncan Weigh-Ins | UFC Vegas 122", date(2026, 10, 9))
    assert _match(1098, [uno, uno], {}) == "a1"


def test_sin_fecha_de_evento_no_casa():
    sin_fecha = TargetEvent(1098, EVENTS[1098].name, EVENTS[1098].location, None)
    assert _match(sin_fecha) is None


def test_REPLAY_las_veladas_reales():
    """The 23 real cards 16-may..24-oct against the 77 real 'weigh' uploads.

    19 get exactly their official weigh-in (the 18 measured on 15-oct plus the
    real 1089); 1063 Belgrade and 1094 Road to UFC got no video in that format,
    1081 weighed in before the first upload of the fixture and 1093 has not
    weighed in yet: those four stay None. No upload may land on a card that is
    not its own."""
    esperado = DATA["expected"]
    obtenido = {str(eid): _match(eid) for eid in EVENTS}
    assert obtenido == esperado
    assert sum(1 for v in obtenido.values() if v) == 19
    casados = [v for v in obtenido.values() if v]
    assert len(casados) == len(set(casados)), "one video, one card"


# --------------------------------------------------- candidatos y feeds


def test_weighin_candidate_ids_solo_la_forma_estricta():
    ids = mf.weighin_candidate_ids(UPLOADS[:40], UPLOADS[40:], None)
    titulos = {u.video_id: u.title for u in UPLOADS}
    assert ids, "the fixture has official weigh-ins"
    for vid in ids:
        t = titulos[vid].lower()
        assert " vs " in t and "weigh-ins |" in t
        assert "ceremonial" not in t and "#" not in t and "show" not in t
    assert "EWTYRIk_pcw" in ids
    assert "IXSay7YbTCM" not in ids and "Ra_28E_kJFI" not in ids
    assert len(ids) == len(set(ids))


def test_merge_feeds_quita_repetidos_y_conserva_el_orden():
    a = _v("a", "A", date(2026, 10, 1))
    b = _v("b", "B", date(2026, 10, 2))
    c = _v("c", "C", date(2026, 10, 3))
    assert mf._merge_feeds([a, b], [b, c]) == [a, b, c]
    assert mf._merge_feeds([a], None) == [a]
    assert mf._merge_feeds(None, None) == []


# ------------------------------------------------------- escritura (repo)


def _sql(conn, fakedb):
    return " ".join(fakedb.mutating_statements(conn)[0].split())


def _params(conn):
    return [params for cur in conn.cursors for _, params in cur.executed][-1]


def test_set_event_weighin_video_solo_si_null_y_upcoming(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert events_repo.set_event_weighin_video(
        conn, 1093, "ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333"
    ) is True
    sql = _sql(conn, fakedb)
    # Never over a value set by hand, never on a completed card.
    assert "weighin_video_id IS NULL" in sql
    assert "status = 'upcoming'" in sql
    assert "WHERE id = %s" in sql


def test_set_event_weighin_video_id_y_titulo_juntos(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    events_repo.set_event_weighin_video(
        conn, 1093, "ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333"
    )
    sql = _sql(conn, fakedb)
    assert "SET weighin_video_id = %s, weighin_video_title = %s" in sql
    assert _params(conn) == (
        "ufc333wi",
        "Volkanovski vs Evloev Weigh-Ins | UFC 333",
        1093,
    )


def test_set_event_weighin_video_vacio_no_hace_nada(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert events_repo.set_event_weighin_video(conn, 1093, "", "x") is False
    assert events_repo.set_event_weighin_video(conn, 1093, None, "x") is False
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------------- objetivos y pase (run)


_ROW_1093 = (
    1093,
    "UFC 333: Volkanovski vs. Evloev",
    "Etihad Arena, Abu Dhabi, United Arab Emirates",
    date(2026, 10, 24),
)
_PESAJE_1093 = [
    _v("ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333", date(2026, 10, 23)),
    _v("cer333", "UFC 333: Ceremonial Weigh-In", date(2026, 10, 23)),
]


def test_get_weighin_target_events_ventana_y_guardas(fakedb):
    captured = {}

    def responder(sql, params=None):
        captured["sql"] = " ".join(sql.split())
        return [_ROW_1093]

    conn = fakedb.Connection(responder)
    events = mf.get_weighin_target_events(conn)
    assert events == [TargetEvent(*_ROW_1093)]
    sql = captured["sql"]
    assert "status = 'upcoming'" in sql
    assert "weighin_video_id IS NULL" in sql
    assert "event_date BETWEEN CURRENT_DATE - 1 AND CURRENT_DATE + 2" in sql


def _run_responder(update_rows):
    def responder(sql, params=None):
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT"):
            return [_ROW_1093]
        if upper.startswith("UPDATE"):
            return update_rows
        return []

    return responder


def test_run_weighins_escribe_y_commitea(fakedb, caplog):
    conn = fakedb.Connection(_run_responder([(1,)]))
    with caplog.at_level("INFO"):
        counts = mf.run_weighins(conn, apply=True, videos=_PESAJE_1093, durations={})
    assert counts["weighin_targets"] == 1
    assert counts["weighin_matched"] == 1
    assert counts["weighin_no_match"] == 0
    assert counts["weighin_written"] == 1
    assert conn.commits == 1
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 1 and "weighin_video_id IS NULL" in updates[0]
    assert ("ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333", 1093) == _params(conn)
    assert any(
        "weigh-in ufc333wi" in r.getMessage() and "1093" in r.getMessage()
        for r in caplog.records
    )


def test_run_weighins_dry_run_no_escribe(fakedb):
    conn = fakedb.Connection(_run_responder([(1,)]))
    counts = mf.run_weighins(conn, apply=False, videos=_PESAJE_1093, durations={})
    assert counts["weighin_matched"] == 1
    assert counts["weighin_written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_run_weighins_no_pisa_lo_puesto_a_mano(fakedb):
    # The guarded UPDATE touched no row (someone set it by hand meanwhile).
    conn = fakedb.Connection(_run_responder([]))
    counts = mf.run_weighins(conn, apply=True, videos=_PESAJE_1093, durations={})
    assert counts["weighin_matched"] == 1
    assert counts["weighin_written"] == 0
    assert conn.commits == 0


def test_run_weighins_sin_video_cuenta_no_match(fakedb):
    conn = fakedb.Connection(_run_responder([(1,)]))
    counts = mf.run_weighins(conn, apply=True, videos=_PESAJE_1093[1:], durations={})
    assert counts["weighin_targets"] == 1
    assert counts["weighin_no_match"] == 1
    assert counts["weighin_written"] == 0
    assert fakedb.mutating_statements(conn) == []


# ----------------------------------------------------------- el workflow


def test_workflow_conserva_el_nombre_y_suma_el_cron_del_viernes():
    yml = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / "refresh-faceoffs.yml"
    ).read_text(encoding="utf-8")
    # notify-on-failure.yml finds it by this EXACT name.
    assert yml.splitlines()[0] == "name: Match face-off videos"
    assert '- cron: "30 18 * * 5"' in yml
    assert "weighin_video_id" in yml


@pytest.mark.parametrize("event_id", [1098, 1090, 1089])
def test_los_tres_pesajes_de_la_base_salen_del_canal_oficial(event_id):
    # 1098 and 1089 hold today exactly what the rule picks; 1090 holds the
    # TheMacLife upload, and the rule picks the UFC's own k1968bd6pTg.
    assert _match(event_id) == {
        1098: "G-XGRj5qe4c",
        1090: "k1968bd6pTg",
        1089: "EWTYRIk_pcw",
    }[event_id]
