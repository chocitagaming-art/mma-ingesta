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
    # SIMULATED: the strict shape, and "vegas" is a real hint of the APEX card
    # 1098. Since the review of 18-oct three rules stop it (the "zuffa" and
    # "boxing" exclusion, the 1098 surnames that are not in the bout, and "ufc"
    # missing from the card). The "ufc in the card" rule on its own is pinned
    # by test_cartelera_sin_ufc_no_casa_aunque_tenga_pista.
    boxeo = _v("box", "Garcia vs Benn Weigh-Ins | Zuffa Boxing Las Vegas", date(2026, 10, 9))
    assert _match(1098, [boxeo], {}) is None
    # Next to the real one it is not even a second candidate (no "ambiguous").
    assert _match(1098, UPLOADS + [boxeo]) == "G-XGRj5qe4c"


def test_cartelera_sin_ufc_no_casa_aunque_tenga_pista():
    # SIMULATED, against cards WITHOUT a matchup in the name (Freedom 250 and
    # the real row 317, Road to UFC): the surname rule cannot protect them and
    # no excluded word is in the title, so the "ufc" in the card is the ONLY
    # thing between another promotion's weigh-in and «Pesaje oficial».
    dia = date.fromordinal(EVENTS[1084].event_date.toordinal() - 1)
    pfl = _v("pfl", "Smith vs Jones Weigh-Ins | PFL Washington", dia)
    assert _match(1084, [pfl], {}) is None
    one = _v("one", "Smith vs Jones Weigh-Ins | ONE Fight Night Shanghai", date(2025, 8, 21))
    assert _match(TargetEvent(*_ROW_317), [one], {}) is None
    # The control: the same shape with "UFC" in the card matches both.
    assert _match(1084, [_v("ufc", "Smith vs Jones Weigh-Ins | UFC Washington", dia)], {}) == "ufc"
    assert _match(TargetEvent(*_ROW_317), [_WI_317], {}) == "RTU000000WI"


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
        "ufc333wi",
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


def _responder(targets, neighbors, update_rows):
    """Fake DB for run_weighins: the targets SELECT, the neighbours SELECT
    (the one that also reads weighin_video_id) and the guarded UPDATE."""

    def responder(sql, params=None):
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT") and "WEIGHIN_VIDEO_ID FROM" in upper:
            return neighbors
        if upper.startswith("SELECT"):
            return targets
        if upper.startswith("UPDATE"):
            return update_rows
        return []

    return responder


def _run_responder(update_rows):
    return _responder([_ROW_1093], [(*_ROW_1093, None)], update_rows)


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
    assert ("ufc333wi", "Volkanovski vs Evloev Weigh-Ins | UFC 333", 1093, "ufc333wi") == _params(conn)
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


# ------------------------------------- review of 18-oct: two cards back to back
# The REAL rows 316 and 317 (read 18-oct-2026): two cards in the same city on
# consecutive days. Road to UFC has no matchup in its name, so before this fix
# the Fight Night weigh-in ("<A> vs <B> Weigh-Ins | UFC Shanghai", the real
# shape of EWtcsj7jMr8) matched BOTH through "shanghai" and was written twice.

_ROW_316 = (316, "UFC Fight Night: Walker vs. Zhang", "Shanghai, Hebei, China", date(2025, 8, 23))
_ROW_317 = (317, "UFC - Road to UFC 4.6", "Shanghai, Hebei, China", date(2025, 8, 22))
# SIMULATED titles in the real format.
_WI_316 = _v("FN0000000WI", "Walker vs Zhang Weigh-Ins | UFC Shanghai", date(2025, 8, 22))
_WI_317 = _v("RTU000000WI", "Smith vs Jones Weigh-Ins | Road to UFC Shanghai", date(2025, 8, 21))


def _written(conn):
    """(video_id, event_id) of every UPDATE the pass sent."""
    out = []
    for cur in conn.cursors:
        for sql, params in cur.executed:
            if " ".join(sql.split()).upper().startswith("UPDATE"):
                out.append((params[0], params[2]))
    return out


def test_con_estelar_exige_un_apellido_en_el_titulo():
    fn = TargetEvent(*_ROW_316)
    # The Road to UFC weigh-in shares city, window and "ufc" with 316, but
    # neither Walker nor Zhang: it is not this card's weigh-in.
    assert _match(fn, [_WI_317], {}) is None
    assert _match(fn, [_WI_316], {}) == "FN0000000WI"
    # A surname only in the CARD half does not count: it has to be the bout.
    raro = _v("x", "Smith vs Jones Weigh-Ins | UFC Shanghai Walker", date(2025, 8, 22))
    assert _match(fn, [raro], {}) is None
    # Road to UFC has no matchup in its name: the other hints still work.
    assert _match(TargetEvent(*_ROW_317), [_WI_317], {}) == "RTU000000WI"


def test_run_weighins_316_317_el_pesaje_de_la_fight_night_va_solo_a_la_suya(fakedb):
    conn = fakedb.Connection(
        _responder([_ROW_317, _ROW_316], [(*_ROW_317, None), (*_ROW_316, None)], [(1,)])
    )
    counts = mf.run_weighins(conn, apply=True, videos=[_WI_316], durations={})
    assert _written(conn) == [("FN0000000WI", 316)]
    assert counts["weighin_written"] == 1
    assert counts["weighin_matched"] == 1
    assert counts["weighin_no_match"] == 1


def test_run_weighins_316_317_el_jueves_el_de_road_to_ufc_no_cae_en_la_fight_night(fakedb):
    conn = fakedb.Connection(
        _responder([_ROW_317, _ROW_316], [(*_ROW_317, None), (*_ROW_316, None)], [(1,)])
    )
    mf.run_weighins(conn, apply=True, videos=[_WI_317], durations={})
    assert _written(conn) == [("RTU000000WI", 317)]


def test_run_weighins_no_reutiliza_el_video_de_otra_velada(fakedb):
    # 316 already holds it (set by hand or by an earlier run): 317, the only
    # target left, must not take it on the next run.
    conn = fakedb.Connection(
        _responder([_ROW_317], [(*_ROW_317, None), (*_ROW_316, "FN0000000WI")], [(1,)])
    )
    counts = mf.run_weighins(conn, apply=True, videos=[_WI_316], durations={})
    assert _written(conn) == []
    assert counts["weighin_written"] == 0
    # 316 holds ANOTHER id (a third-party upload picked by hand, like 1090):
    # the video still names 316's main event, so it is never 317's.
    conn = fakedb.Connection(
        _responder([_ROW_317], [(*_ROW_317, None), (*_ROW_316, "enkyfSnB0r0")], [(1,)])
    )
    mf.run_weighins(conn, apply=True, videos=[_WI_316], durations={})
    assert _written(conn) == []


def test_run_weighins_no_reutiliza_el_video_de_una_vecina_sin_apellidos(fakedb):
    # SIMULATED target next to the real row 317, which already holds the Road
    # to UFC weigh-in. Neither name has a matchup, so no surname owns the
    # video: only the "already held by another card" guard keeps 318 from
    # taking it (in production the UPDATE's NOT EXISTS would refuse it too,
    # but the dry run would still count it as matched).
    otra = (318, "UFC Fight Night", "Shanghai, Hebei, China", date(2025, 8, 23))
    conn = fakedb.Connection(
        _responder([otra], [(*_ROW_317, "RTU000000WI"), (*otra, None)], [(1,)])
    )
    counts = mf.run_weighins(conn, apply=True, videos=[_WI_317], durations={})
    assert _written(conn) == []
    assert counts["weighin_matched"] == 0
    assert counts["weighin_no_match"] == 1


def test_run_weighins_un_video_para_dos_veladas_sin_apellidos_no_escribe(fakedb, caplog):
    # SIMULATED: two cards without a matchup in the name, same city, same
    # weekend. Nothing tells them apart, so neither gets it.
    otra = (318, "UFC Fight Night", "Shanghai, Hebei, China", date(2025, 8, 23))
    conn = fakedb.Connection(
        _responder([_ROW_317, otra], [(*_ROW_317, None), (*otra, None)], [(1,)])
    )
    with caplog.at_level("WARNING"):
        counts = mf.run_weighins(conn, apply=True, videos=[_WI_317], durations={})
    assert _written(conn) == []
    assert counts["weighin_written"] == 0
    assert any("varias veladas" in r.getMessage() for r in caplog.records)


def test_get_weighin_neighbor_events_lee_todas_las_de_la_ventana(fakedb):
    captured = {}

    def responder(sql, params=None):
        captured["sql"] = " ".join(sql.split())
        return [(*_ROW_316, "FN0000000WI")]

    conn = fakedb.Connection(responder)
    assert mf.get_weighin_neighbor_events(conn) == [
        (TargetEvent(*_ROW_316), "FN0000000WI")
    ]
    sql = captured["sql"]
    # Any status and any weigh-in value: a completed or already filled card
    # still owns its own weigh-in.
    assert "status" not in sql
    assert "weighin_video_id IS NULL" not in sql
    assert "event_date BETWEEN CURRENT_DATE - 3 AND CURRENT_DATE + 2" in sql


def test_set_event_weighin_video_no_repite_un_video_ya_usado(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    events_repo.set_event_weighin_video(conn, 317, "FN0000000WI", "t")
    sql = _sql(conn, fakedb)
    assert "NOT EXISTS (SELECT 1 FROM events o WHERE o.weighin_video_id = %s)" in sql
    assert _params(conn)[-1] == "FN0000000WI"


def test_rechaza_boxeo_y_bjj_aunque_digan_ufc():
    # SIMULATED, against Freedom 250 (no matchup in the name, so the surname
    # rule does not protect it): the same channel uploads Zuffa Boxing and BJJ.
    dia = date.fromordinal(EVENTS[1084].event_date.toordinal() - 1)
    for titulo in (
        "Smith vs Jones Weigh-Ins | Zuffa Boxing x UFC Freedom 250",
        "Smith vs Jones Weigh-Ins | UFC BJJ Freedom 250",
    ):
        assert _match(1084, [_v("x", titulo, dia)], {}) is None, titulo
    # The control: the same shape with a plain UFC card does match.
    assert _match(1084, [_v("x", "Smith vs Jones Weigh-Ins | UFC Freedom 250", dia)], {}) == "x"


def test_rechaza_directos_y_series_con_la_forma_estricta():
    # SIMULATED traps against the real 1093 (none exists in the real data).
    d = date(2026, 10, 23)
    for titulo in (
        "Volkanovski vs Evloev Weigh-Ins | UFC 333 Live",
        "Volkanovski vs Evloev Morning Weigh-Ins | UFC 333",
        "Volkanovski vs Evloev Weigh-Ins | UFC 333 Embedded",
    ):
        assert _match(1093, [_v("x", titulo, d)], {}) is None, titulo


def test_duracion_larga_rechaza():
    video = _v("w", "Volkanovski vs Evloev Weigh-Ins | UFC 333", date(2026, 10, 23))
    assert _match(1093, [video], {"w": 7200}) is None
    assert _match(1093, [video], {"w": mf.MAX_WEIGHIN_SECONDS}) == "w"
    assert mf.MAX_WEIGHIN_SECONDS == 1800
    # Real: the official ones last 304-915 s; the shortest ceremonial with a
    # known duration lasts 3071 s.
    oficiales = [DURATIONS[v] for v in DATA["expected"].values() if v]
    assert max(oficiales) == 915 < mf.MAX_WEIGHIN_SECONDS
    ceremoniales = [
        DURATIONS[u.video_id]
        for u in UPLOADS
        if "Ceremonial" in u.title and u.video_id in DURATIONS
    ]
    assert min(ceremoniales) == 3071 > mf.MAX_WEIGHIN_SECONDS
