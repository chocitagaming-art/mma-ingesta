"""UFC face-off (careo) matching: feed parse, conservative match guard, run,
and the first-writer-wins repository write. No network, no DB (fakedb recorder).

The XML fixture mirrors the real UFC channel Atom feed (yt:videoId + title +
published), verified against channel UCvgfXK4nTYKudb0rFR6noLA on 2026-07-18.
"""

import sys
from datetime import date
from types import SimpleNamespace

from src.scrapers import match_faceoffs
from src.scrapers.match_faceoffs import (
    FeedVideo,
    TargetEvent,
    fetch_channel_uploads,
    match_event,
    parse_feed,
    parse_playlist_page,
    uploads_playlist_id,
)
from src.scrapers.repositories.events import set_event_faceoff_video

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <yt:videoId>Vb_0zQ-hIzM</yt:videoId>
    <title>UFC Oklahoma City: Fighter Face-offs</title>
    <published>2026-07-17T20:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>zTqNg1ECqs4</yt:videoId>
    <title>UFC Oklahoma City: Ceremonial Weigh-In</title>
    <published>2026-07-17T18:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>ppv330face1</yt:videoId>
    <title>UFC 330: Fighter Face-offs</title>
    <published>2026-08-14T20:00:00+00:00</published>
  </entry>
</feed>
"""


def _feed():
    return parse_feed(FEED_XML)


def _oklahoma_event():
    return TargetEvent(
        id=1061,
        name="UFC Fight Night: Du Plessis vs. Usman",
        location="Paycom Center, Oklahoma City, OK, United States",
        event_date=date(2026, 7, 18),
    )


# --------------------------------------------------------------------- parsing


def test_parse_feed_extracts_id_title_date():
    videos = _feed()
    assert len(videos) == 3
    assert videos[0] == FeedVideo("Vb_0zQ-hIzM", "UFC Oklahoma City: Fighter Face-offs", date(2026, 7, 17))


def test_parse_feed_bad_xml_returns_empty():
    assert parse_feed("<not-a-feed") == []


# ----------------------------------------------------------------------- match


def test_match_by_city_picks_faceoff_over_weighin():
    # Both the face-off and the ceremonial weigh-in share the city and window;
    # only the face-off passes the title whitelist.
    assert match_event(_oklahoma_event(), _feed()) == "Vb_0zQ-hIzM"


def test_match_rejects_when_only_weighin_present():
    weighin_only = [v for v in _feed() if "Weigh-In" in v.title]
    assert match_event(_oklahoma_event(), weighin_only) is None


def test_match_by_ufc_number_for_ppv():
    ppv = TargetEvent(
        id=1064,
        name="UFC 330: Makhachev vs. Machado Garry",
        location="Xfinity Mobile Arena, Philadelphia, PA, United States",
        event_date=date(2026, 8, 15),
    )
    # City (Philadelphia) is NOT in the title; the 'UFC 330' token carries it.
    assert match_event(ppv, _feed()) == "ppv330face1"


def test_match_rejects_out_of_date_window():
    stale = TargetEvent(1061, _oklahoma_event().name, _oklahoma_event().location, date(2026, 1, 1))
    assert match_event(stale, _feed()) is None


def test_match_rejects_wrong_city_and_no_number():
    other = TargetEvent(9, "UFC Fight Night: Someone vs. Other", "Arena, Las Vegas, NV, USA", date(2026, 7, 18))
    assert match_event(other, _feed()) is None


def test_match_none_when_event_date_missing():
    undated = TargetEvent(9, "UFC Fight Night: X vs. Y", "Arena, Oklahoma City, OK", None)
    assert match_event(undated, _feed()) is None


def test_match_las_vegas_fight_night_via_vegas_alias():
    # UFC titles Apex/Las Vegas Fight Nights "UFC Vegas NNN: Fighter Faceoffs",
    # not by the city "Las Vegas" and with no card number. The careo must still
    # match on the 'vegas' token — the rescue pass' bread-and-butter case.
    event = TargetEvent(
        1058,
        "UFC Fight Night: Kape vs. Horiguchi",
        "UFC Apex, Las Vegas, NV, United States",
        date(2026, 6, 20),
    )
    feed = [
        FeedVideo("KDEt5A8GRYY", "UFC Vegas 119: Fighter Faceoffs", date(2026, 6, 19))
    ]
    assert match_event(event, feed) == "KDEt5A8GRYY"


def test_match_vegas_alias_when_city_data_says_nevada():
    # Some rows carry 'Nevada' as the city; the video is still 'UFC Vegas NNN'.
    event = TargetEvent(
        1083,
        "UFC Fight Night: Muhammad vs. Bonfim",
        "UFC Apex, Nevada, United States",
        date(2026, 6, 6),
    )
    feed = [
        FeedVideo("Y8cz6VbyjyU", "UFC Vegas 118: Fighter Faceoffs", date(2026, 6, 5))
    ]
    assert match_event(event, feed) == "Y8cz6VbyjyU"


def test_match_vegas_event_rejects_other_citys_faceoff():
    # A Las Vegas event whose window only holds ANOTHER city's face-offs must not
    # borrow it (no 'vegas' token in the title) — mis-attribution guard holds.
    event = TargetEvent(
        9,
        "UFC Fight Night: A vs. B",
        "UFC Apex, Las Vegas, NV, United States",
        date(2026, 6, 20),
    )
    feed = [
        FeedVideo("okc", "UFC Oklahoma City: Fighter Face-offs", date(2026, 6, 19))
    ]
    assert match_event(event, feed) is None


def test_match_by_city_token_when_title_drops_generic_words():
    # 'Oklahoma City' matches on the distinctive 'oklahoma' token even if the
    # title omits the generic 'City' word.
    event = TargetEvent(
        1061,
        "UFC Fight Night: X vs. Y",
        "Paycom Center, Oklahoma City, OK, United States",
        date(2026, 7, 18),
    )
    feed = [FeedVideo("okc2", "UFC Oklahoma: Fighter Faceoffs", date(2026, 7, 17))]
    assert match_event(event, feed) == "okc2"


# ------------------------------------------------------------------------- run


def _responder(update_result=None):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return [(
                1061,
                "UFC Fight Night: Du Plessis vs. Usman",
                "Paycom Center, Oklahoma City, OK, United States",
                date(2026, 7, 18),
            )]
        if "UPDATE" in upper:
            return update_result or []
        return []

    return responder


def test_run_matches_and_writes(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = match_faceoffs.run(conn, apply=True, feed=_feed())
    assert counts["matched"] == 1
    assert counts["written"] == 1
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 1
    assert "faceoff_video_id IS NULL" in updates[0]
    assert conn.commits == 1


def test_run_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = match_faceoffs.run(conn, apply=False, feed=_feed())
    assert counts["matched"] == 1
    assert counts["written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


# ------------------------------------------------------------------ repository


def test_set_event_faceoff_video_first_writer_wins(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    set_event_faceoff_video(conn, 1061, "Vb_0zQ-hIzM")
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    assert "SET faceoff_video_id = %s" in sql
    assert "WHERE id = %s AND faceoff_video_id IS NULL" in sql


def test_set_event_faceoff_video_empty_is_noop(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert set_event_faceoff_video(conn, 1061, "") is False
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------------- API rescue: playlist parse

# One page of the real playlistItems response shape (part=snippet,contentDetails).
PLAYLIST_PAGE_1 = {
    "items": [
        {
            "contentDetails": {
                "videoId": "rescueFace1",
                "videoPublishedAt": "2026-07-10T20:00:00Z",
            },
            "snippet": {"title": "UFC 329: Fighter Face-offs"},
        },
        {
            "contentDetails": {
                "videoId": "someHighlight",
                "videoPublishedAt": "2026-07-10T18:00:00Z",
            },
            "snippet": {"title": "UFC 329: Free Fight"},
        },
    ],
    "nextPageToken": "PAGE2",
}

PLAYLIST_PAGE_2 = {
    "items": [
        {
            "contentDetails": {
                "videoId": "oldVideo",
                "videoPublishedAt": "2026-05-01T00:00:00Z",
            },
            "snippet": {"title": "An old upload"},
        }
    ]
}


def test_uploads_playlist_id_swaps_uc_prefix_for_uu():
    channel = "UCvgfXK4nTYKudb0rFR6noLA"
    assert uploads_playlist_id(channel) == "UU" + channel[2:]


def test_parse_playlist_page_extracts_feedvideos():
    videos = parse_playlist_page(PLAYLIST_PAGE_1)
    assert videos[0] == FeedVideo(
        "rescueFace1", "UFC 329: Fighter Face-offs", date(2026, 7, 10)
    )
    assert len(videos) == 2


def test_parse_playlist_page_falls_back_to_snippet_fields():
    page = {
        "items": [
            {
                "snippet": {
                    "title": "Fallback",
                    "resourceId": {"videoId": "vv"},
                    "publishedAt": "2026-07-10T00:00:00Z",
                }
            }
        ]
    }
    assert parse_playlist_page(page) == [FeedVideo("vv", "Fallback", date(2026, 7, 10))]


def test_parse_playlist_page_skips_incomplete_items():
    page = {
        "items": [
            {"contentDetails": {"videoId": "no-date-or-title"}},
            {"snippet": {"title": "no id"}},
        ]
    }
    assert parse_playlist_page(page) == []


# ------------------------------------------------ API rescue: paged uploads


def test_fetch_channel_uploads_pages_until_published_after():
    pages = {None: PLAYLIST_PAGE_1, "PAGE2": PLAYLIST_PAGE_2}
    calls = []

    def fetcher(params):
        calls.append(params)
        return pages[params.get("pageToken")]

    videos = fetch_channel_uploads(
        "key", published_after=date(2026, 7, 1), max_pages=10, fetcher=fetcher
    )
    # page 1 (2026-07-10) is kept; page 2 (2026-05-01) predates the cutoff -> stop.
    assert [v.video_id for v in videos] == ["rescueFace1", "someHighlight"]
    assert len(calls) == 2
    assert calls[0]["playlistId"] == "UUvgfXK4nTYKudb0rFR6noLA"
    assert calls[0]["key"] == "key"
    assert "contentDetails" in calls[0]["part"]


def test_fetch_channel_uploads_respects_max_pages():
    endless = {
        "items": [
            {
                "contentDetails": {
                    "videoId": "v",
                    "videoPublishedAt": "2026-07-10T00:00:00Z",
                },
                "snippet": {"title": "recent"},
            }
        ],
        "nextPageToken": "MORE",
    }
    calls = []

    def fetcher(params):
        calls.append(params)
        return endless

    videos = fetch_channel_uploads(
        "key", published_after=date(2026, 1, 1), max_pages=3, fetcher=fetcher
    )
    assert len(calls) == 3
    assert len(videos) == 3


# ----------------------------------------------- API rescue: target query


def test_get_rescue_target_events_targets_null_faceoff_in_window(fakedb):
    captured = {}

    def responder(sql, params=None):
        if sql.strip().startswith("SELECT"):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params
            return [(
                1060,
                "UFC 329: Volkanovski vs. Lopes",
                "T-Mobile Arena, Las Vegas, NV, United States",
                date(2026, 7, 11),
            )]
        return []

    conn = fakedb.Connection(responder)
    events = match_faceoffs.get_rescue_target_events(conn, lookback_days=45)
    assert events[0].id == 1060
    assert "faceoff_video_id IS NULL" in captured["sql"]
    assert "status = 'upcoming'" not in captured["sql"]
    assert captured["params"] == (45,)


# ----------------------------------------------------- API rescue: run pass

_RESCUE_EVENT = (
    1060,
    "UFC 329: Volkanovski vs. Lopes",
    "T-Mobile Arena, Las Vegas, NV, United States",
    date(2026, 7, 11),
)
API_FEED = [FeedVideo("Jv_-5jgsXvc", "UFC 329: Fighter Face-offs", date(2026, 7, 10))]


def _rescue_responder(*, rss_rows, rescue_rows, update_result=None):
    def responder(sql, params=None):
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT") and "STATUS = 'UPCOMING'" in upper:
            return rss_rows
        if upper.startswith("SELECT") and "FACEOFF_VIDEO_ID IS NULL" in upper:
            return rescue_rows
        if "UPDATE" in upper:
            return update_result or []
        return []

    return responder


def test_run_rescues_passed_event_via_api_feed(fakedb):
    conn = fakedb.Connection(
        _rescue_responder(
            rss_rows=[], rescue_rows=[_RESCUE_EVENT], update_result=[(1,)]
        )
    )
    counts = match_faceoffs.run(
        conn, apply=True, feed=[], api_feed=API_FEED, rescue_days=45
    )
    assert counts["rescued"] == 1
    assert counts["written"] == 1
    assert len(fakedb.mutating_statements(conn)) == 1
    assert conn.commits == 1


def test_run_rescue_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(
        _rescue_responder(
            rss_rows=[], rescue_rows=[_RESCUE_EVENT], update_result=[(1,)]
        )
    )
    counts = match_faceoffs.run(conn, apply=False, feed=[], api_feed=API_FEED)
    assert counts["rescued"] == 1
    assert counts["written"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_run_skips_rescue_when_api_feed_none(fakedb):
    # No api_feed -> the rescue query must never run (backward compatible).
    conn = fakedb.Connection(
        _rescue_responder(rss_rows=[], rescue_rows=[_RESCUE_EVENT])
    )
    counts = match_faceoffs.run(conn, apply=True, feed=[])
    assert "rescued" not in counts
    assert counts["written"] == 0


def test_run_rescue_skips_event_already_matched_by_rss(fakedb):
    okc = (
        1061,
        "UFC Fight Night: Du Plessis vs. Usman",
        "Paycom Center, Oklahoma City, OK, United States",
        date(2026, 7, 18),
    )
    conn = fakedb.Connection(
        _rescue_responder(rss_rows=[okc], rescue_rows=[okc], update_result=[(1,)])
    )
    # Both passes would match 1061; the RSS write wins and rescue must skip it.
    counts = match_faceoffs.run(
        conn, apply=True, feed=_feed(), api_feed=_feed(), rescue_days=45
    )
    assert counts["matched"] == 1
    assert counts.get("rescued", 0) == 0
    assert counts["written"] == 1
    assert len(fakedb.mutating_statements(conn)) == 1


# ---------------------------------------------------- API rescue: main wiring

_RSS_STUB = [FeedVideo("x", "UFC 330: Fighter Face-offs", date(2026, 8, 14))]


class _FakeConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _empty_conn(fakedb):
    return _FakeConnCtx(fakedb.Connection(lambda sql, params=None: []))


def test_main_without_youtube_key_skips_api_rescue(monkeypatch, fakedb):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(match_faceoffs, "fetch_channel_feed", lambda *a, **k: _RSS_STUB)

    def boom(*a, **k):
        raise AssertionError("fetch_channel_uploads must not run without a key")

    monkeypatch.setattr(match_faceoffs, "fetch_channel_uploads", boom)
    monkeypatch.setattr(
        match_faceoffs, "get_settings", lambda: SimpleNamespace(database_url="x")
    )
    monkeypatch.setattr(match_faceoffs, "connect", lambda url: _empty_conn(fakedb))
    monkeypatch.setattr(sys, "argv", ["prog"])
    match_faceoffs.main()  # must not raise


def test_main_with_youtube_key_runs_api_rescue(monkeypatch, fakedb):
    monkeypatch.setenv("YOUTUBE_API_KEY", "quota-key")
    monkeypatch.setattr(match_faceoffs, "fetch_channel_feed", lambda *a, **k: _RSS_STUB)
    seen = {}

    def fake_uploads(api_key, **kwargs):
        seen["api_key"] = api_key
        seen["published_after"] = kwargs.get("published_after")
        return [FeedVideo("resc", "UFC 999: Fighter Face-offs", date(2026, 1, 1))]

    monkeypatch.setattr(match_faceoffs, "fetch_channel_uploads", fake_uploads)
    monkeypatch.setattr(
        match_faceoffs, "get_settings", lambda: SimpleNamespace(database_url="x")
    )
    monkeypatch.setattr(match_faceoffs, "connect", lambda url: _empty_conn(fakedb))
    monkeypatch.setattr(sys, "argv", ["prog"])
    match_faceoffs.main()
    assert seen["api_key"] == "quota-key"
    assert seen["published_after"] is not None


# ------------------------------------------------- guarda de ciudad (2-ago-26)
# El careo del 1063 se perdio porque `_place_tokens` devolvia un CONJUNTO VACIO
# y `any()` sobre vacio es False siempre. La causa no era el corte de longitud
# (el parche del 31-jul proponia subirlo a 4, y eso rompe "Rio de Janeiro"):
# era leer solo el 2o campo de `location`.


def test_belgrade_ya_no_devuelve_el_conjunto_vacio():
    # EL CASO REAL DEL 1063. El 2o campo es 'BG', de 2 caracteres: se caia por
    # el corte de longitud y el conjunto quedaba vacio. 'Belgrade' estaba en el
    # 1er campo, que el codigo no miraba nunca.
    tokens = match_faceoffs._place_tokens("Belgrade Arena, BG, Serbia")
    assert "belgrade" in tokens
    assert tokens, "un conjunto vacio hace que any() sea False SIEMPRE"


def test_washington_dc_tenia_el_mismo_fallo():
    assert "washington" in match_faceoffs._place_tokens("Washington, DC, USA")


def test_rio_sobrevive_al_corte_de_tres_caracteres():
    # El contraejemplo que tumbo el parche del 31-jul: con un corte de 4, 'rio'
    # desaparece y "UFC Rio" deja de casar. El corte SIGUE en 3.
    tokens = match_faceoffs._place_tokens("Rio de Janeiro, Rio de Janeiro, Brazil")
    assert "rio" in tokens
    assert "janeiro" in tokens


def test_la_ciudad_cuenta_aunque_el_segundo_campo_sea_el_estado():
    # 'London, England, United Kingdom': el 2o campo es la REGION, asi que antes
    # el unico token era 'england' y un video "UFC London ... Faceoffs" no
    # casaba. Le pasaba igual a Miami/Florida y a 8 mas.
    assert "london" in match_faceoffs._place_tokens("London, England, United Kingdom")
    assert "miami" in match_faceoffs._place_tokens("Miami, Florida, USA")


def test_los_tipos_de_recinto_no_son_tokens():
    # 'arena' nombra un edificio, no un sitio: como token casaria con el careo
    # de cualquier otra velada dentro de la ventana de fechas.
    tokens = match_faceoffs._place_tokens("Belgrade Arena, BG, Serbia")
    assert "arena" not in tokens


def test_el_pais_demasiado_repetido_no_es_token():
    # 'usa' sale en 91 de las 185 location reales: no discrimina nada.
    tokens = match_faceoffs._place_tokens("Miami, Florida, USA")
    assert "usa" not in tokens
    assert "united" not in match_faceoffs._place_tokens(
        "Meta APEX, Las Vegas, NV, United States"
    )


def test_el_1087_del_apex_sigue_dando_vegas():
    # La velada del 8-ago. Tiene que seguir funcionando por los dos caminos: el
    # token normal y la regla especial de Nevada/Apex.
    tokens = match_faceoffs._place_tokens("Meta APEX, Las Vegas, NV, United States")
    assert "vegas" in tokens


def test_ninguna_location_real_se_queda_sin_tokens():
    # Las formas que existen de verdad en la BD (185 distintas el 2-ago-2026),
    # una por cada patron. Ninguna puede devolver el conjunto vacio.
    for location in (
        "Belgrade Arena, BG, Serbia",
        "Washington, DC, USA",
        "T-Mobile Arena, Las Vegas, NV, United States",
        "Rio de Janeiro, Brazil",
        "Natal, Rio Grande do Norte, Brazil",
        "Ottawa, Ontario, Canada",
        "Etihad Arena, Abu Dhabi, United Arab Emirates",
        "Las Vegas",
    ):
        assert match_faceoffs._place_tokens(location), f"sin tokens: {location}"


def test_sin_location_no_revienta():
    assert match_faceoffs._place_tokens(None) == set()
    assert match_faceoffs._place_tokens("") == set()


def test_ufc_no_puede_ser_token_aunque_este_en_el_nombre_del_recinto():
    # REGRESION REAL del 2-ago: al leer la location entera, "UFC Apex, Las
    # Vegas" aportaba el token 'ufc'... que sale en TODOS los titulos de careo,
    # asi que una velada de Vegas casaba con el video de Oklahoma City.
    tokens = match_faceoffs._place_tokens("UFC Apex, Las Vegas, NV, United States")
    assert "ufc" not in tokens
    assert "vegas" in tokens, "pero la guarda buena tiene que seguir ahi"


# ------------------------------------------- el tope, y por que hasta hoy mentia
#
# 🪤 `test_fetch_channel_uploads_respects_max_pages` (arriba) comprueba que el
# tope se respeta, y eso ya estaba bien. Lo que nadie miraba es lo que pasa
# DESPUES: el bucle salia sin log ni excepcion, asi que una lista corta por
# truncamiento era indistinguible de «el canal no tiene mas videos». El rescate
# seguia y devolvia «no match» para eventos cuyos videos ni siquiera descargo.
# No es que mirase y no estuviera: es que no miro.


def test_uploads_avisa_cuando_agota_el_tope_sin_llegar_a_la_fecha(caplog):
    endless = {
        "items": [
            {
                "contentDetails": {"videoId": "v", "videoPublishedAt": "2026-07-10T00:00:00Z"},
                "snippet": {"title": "recent"},
            }
        ],
        "nextPageToken": "MORE",
    }

    with caplog.at_level("WARNING"):
        fetch_channel_uploads(
            "key",
            published_after=date(2026, 1, 1),
            max_pages=3,
            fetcher=lambda params: endless,
        )

    avisos = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(avisos) == 1, "el truncamiento tiene que avisar"
    texto = avisos[0].getMessage()
    # Y con la fecha REALMENTE alcanzada dentro: un «truncado» a secas no dice
    # cuanto falta, y sin eso nadie sabe que --max-pages pedir. Este asserto es
    # el que separa un aviso util de uno decorativo.
    assert "2026-07-10" in texto
    assert "2026-01-01" in texto


def test_uploads_NO_avisa_cuando_alcanza_la_fecha(caplog):
    # CONTROL NEGATIVO, y es el que decide si el aviso sirve: una alarma que
    # tambien salta en la corrida buena es ruido, y el cron la dispara dos veces
    # al dia. Aqui la segunda pagina ya trae un video anterior al cutoff.
    paginas = [
        {
            "items": [
                {
                    "contentDetails": {"videoId": "a", "videoPublishedAt": "2026-07-10T00:00:00Z"},
                    "snippet": {"title": "recent"},
                }
            ],
            "nextPageToken": "MORE",
        },
        {
            "items": [
                {
                    "contentDetails": {"videoId": "b", "videoPublishedAt": "2025-12-01T00:00:00Z"},
                    "snippet": {"title": "viejo"},
                }
            ],
            "nextPageToken": "MORE",
        },
    ]
    it = iter(paginas)

    with caplog.at_level("WARNING"):
        fetch_channel_uploads(
            "key",
            published_after=date(2026, 1, 1),
            max_pages=20,
            fetcher=lambda params: next(it),
        )

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_el_flag_max_pages_llega_de_verdad_al_fetcher(monkeypatch):
    # 🪤 EL MEDIO ARREGLO QUE ESTE TEST IMPIDE. Anadir `--max-pages` al parser y
    # olvidarse de pasarlo en la llamada deja un flag que se acepta, no da
    # error, y no hace absolutamente nada: el backfill seguiria cortando a 20
    # paginas y el operador creeria que miro 120. Un test sobre el parser solo
    # (`args.max_pages == 7`) da VERDE con ese medio arreglo puesto.
    visto = {}

    def falso_fetch_uploads(api_key, *, published_after=None, max_pages=None, **kw):
        visto["max_pages"] = max_pages
        visto["published_after"] = published_after
        return []

    monkeypatch.setattr(match_faceoffs, "fetch_channel_uploads", falso_fetch_uploads)
    monkeypatch.setattr(
        match_faceoffs,
        "fetch_channel_feed",
        lambda session: [FeedVideo("x", "UFC Vegas 1: Fighter Face-offs", date(2026, 7, 1))],
    )
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(sys, "argv", ["match_faceoffs.py", "--max-pages", "7"])

    # Se corta en `connect`, que es el primer sitio DESPUES del rescate: con
    # --dump-feed el main sale antes de llegar a el y el test no probaria nada.
    monkeypatch.setattr(match_faceoffs, "get_settings", lambda: SimpleNamespace(database_url="x"))
    monkeypatch.setattr(match_faceoffs, "connect", _corta())

    try:
        match_faceoffs.main()
    except _Corte:
        pass

    assert visto.get("max_pages") == 7


class _Corte(Exception):
    pass


def _corta():
    def _connect(*a, **kw):
        raise _Corte()

    return _connect


def test_el_recinto_generico_del_1059_ya_no_es_un_token_de_lugar():
    # "National Gymnastics Arena, Baku, Azerbaijan". Con la ventana de 45 dias
    # del cron el riesgo era casi nulo; con los ~210 dias que pide el backfill
    # del historico, cualquier video con "face-off" y "national" en el titulo se
    # le escribiria encima — y es first-writer-wins, o sea irreversible.
    tokens = match_faceoffs._place_tokens("National Gymnastics Arena, Baku, Azerbaijan")
    assert "national" not in tokens
    assert "gymnastics" not in tokens
    # Y no pierde nada: le quedan los dos que de verdad lo nombran.
    assert {"baku", "azerbaijan"} <= tokens


# ------------------------------------------- la MARCA de la velada (Noche UFC)
#
# 🪤 El guard sacaba el token distintivo SOLO de `location`, y la UFC titula las
# veladas de marca con la marca, jamas con la ciudad. Los dos unicos careos de
# una Noche UFC que hay en el canal oficial en 9 anos lo demuestran:
#   7dhhFNCQEcQ  2025-09-12  "Noche UFC: Fighter Faceoffs"
#   qpjPPGtcS3I  2023-09-15  "Noche UFC: Weigh-In Faceoffs"
# Ninguno lleva ciudad ni "ufc <N>", asi que los dos se RECHAZABAN — y como la
# escritura es first-writer-wins, el careo se quedaba a NULL para siempre.
#
# Todo lo de aqui abajo sale de datos reales: los 797 nombres de `events` y las
# 20.000 subidas del canal UCvgfXK4nTYKudb0rFR6noLA (2017-05-23 -> 2026-09-11),
# de las que 483 llevan "face-off" en el titulo.


def _noche_ufc_1088():
    """El evento 1088 tal y como esta en la BD el 11-sep-2026."""
    return TargetEvent(
        id=1088,
        name="Noche UFC: Silva vs. Delgado",
        location="Desert Diamond Arena, Glendale, AZ, United States",
        event_date=date(2026, 9, 12),
    )


def _video(titulo, dia=11):
    return [FeedVideo("vid", titulo, date(2026, 9, dia))]


def test_noche_ufc_casa_por_la_marca_del_nombre():
    # El titulo REAL de la Noche UFC de 2025. Sin este cambio: RECHAZADO.
    assert match_event(_noche_ufc_1088(), _video("Noche UFC: Fighter Faceoffs")) == "vid"


def test_noche_ufc_casa_con_la_marca_al_final_del_titulo():
    # La otra forma que usa el canal: "<A> vs <B> <cosa> | Noche UFC", que es
    # como esta titulado el pesaje de ESTA velada (Hh9icPderiM).
    assert match_event(_noche_ufc_1088(), _video("Silva vs Delgado Face-Offs | Noche UFC")) == "vid"


def test_la_marca_no_abre_la_puerta_al_careo_de_otra_ciudad():
    assert match_event(_noche_ufc_1088(), _video("UFC Paris: Fighter Face-offs")) is None


def test_la_marca_no_abre_la_puerta_al_careo_de_una_numerada():
    assert match_event(_noche_ufc_1088(), _video("UFC 330: Fighter Face-offs")) is None


def test_el_careo_de_boxeo_de_la_misma_ventana_sigue_fuera():
    # El canal oficial emite tambien boxeo. "Canelo vs Crawford: Final Faceoffs"
    # es REAL (13-sep-2025) y cayo DENTRO de la ventana de la Noche UFC de 2025;
    # el 12-sep-2026 el canal esta promocionando "Garcia vs Benn" igual. Por eso
    # "final" es stopword de marca.
    assert match_event(_noche_ufc_1088(), _video("Canelo vs Crawford: Final Faceoffs", 12)) is None
    assert match_event(_noche_ufc_1088(), _video("Garcia vs Benn: Final Faceoffs", 12)) is None


def test_la_marca_no_se_salta_la_lista_blanca_del_titulo():
    # La condicion 1 sigue mandando: sin "face-off" no hay careo, lleve la marca
    # que lleve. Los dos titulos son REALES de esta semana.
    assert match_event(_noche_ufc_1088(), _video("Noche UFC: Ceremonial Weigh-In", 8)) is None
    assert match_event(_noche_ufc_1088(), _video("Silva vs Delgado Weigh-Ins | Noche UFC")) is None


def test_el_hashtag_pegado_no_es_la_marca():
    # Los shorts del canal escriben "#NocheUFC" todo junto, y "noche" se busca
    # como PALABRA: no hay frontera entre "noche" y "ufc". Un short no puede
    # llevarse la columna.
    assert match_event(_noche_ufc_1088(), _video("Silva and Delgado FACE-OFF #NocheUFC")) is None


def test_los_apellidos_del_estelar_no_son_token_de_marca():
    # Solo cuenta lo anterior a los dos puntos. Si contase el nombre entero,
    # "silva" y "delgado" serian tokens del guard.
    assert match_faceoffs._name_tokens("Noche UFC: Silva vs. Delgado") == {"noche"}
    assert match_event(_noche_ufc_1088(), _video("Best Silva Face-Offs Ever")) is None


def test_fighter_nunca_es_token_de_marca():
    # LA MINA. "fighter" sale de 28 filas reales ("The Ultimate Fighter N
    # Finale") y esta en 108 de los 483 titulos de careo del canal, porque el
    # formato canonico ES "UFC <sitio>: Fighter Face-offs".
    assert match_faceoffs._name_tokens("The Ultimate Fighter 33 Finale: Ortega vs Rodriguez") == set()
    assert match_faceoffs._name_tokens("The Ultimate Fighter 31 Finale") == set()


def test_un_evento_con_numero_no_aporta_tokens_de_marca():
    # El numero de cartelera ya lo guarda _UFC_NUM_RE, que es mas fino. De paso
    # cierra los patrocinios.
    assert match_faceoffs._name_tokens("Crypto.com UFC 331: Van vs. Pantoja 2") == set()
    assert match_faceoffs._name_tokens("Polymarket UFC 334: TBD vs. TBD") == set()
    assert match_faceoffs._name_tokens("UFC 306: Riyadh Season Noche UFC") == set()


def test_una_cabecera_que_es_un_emparejamiento_no_aporta_marca():
    # "Ortiz vs Shamrock 3: The Final Chapter" es una fila REAL: sin este corte,
    # "ortiz" y "shamrock" serian tokens de marca. Y si manana el scraper trae
    # la velada SIN separador, los apellidos entrarian por la misma puerta: el
    # modulo prefiere fallar por defecto.
    assert match_faceoffs._name_tokens("Ortiz vs Shamrock 3: The Final Chapter") == set()
    assert match_faceoffs._name_tokens("Noche UFC Silva vs Delgado") == set()


def test_fox_y_fuel_no_son_tokens_de_marca():
    # 33 filas reales de 2011-2013 ("UFC on FOX / FUEL TV"). El cron no las
    # alcanza, pero el backfill historico de ~210 dias que contempla el modulo
    # si, y la escritura es irreversible. Ninguna tiene careo en YouTube.
    assert match_faceoffs._name_tokens("UFC on FOX: Henderson vs. Diaz") == set()
    assert match_faceoffs._name_tokens("UFC on FUEL TV: Silva vs. Stann") == set()


def test_los_digitos_sueltos_no_son_token_de_marca():
    assert match_faceoffs._name_tokens("UFC Freedom 250") == {"freedom"}


def test_ufc_freedom_250_casa_por_la_marca():
    # El otro careo que se perdia, y es un caso REAL ya pasado: iDjZkLhkZw8,
    # "UFC Freedom 250: Fighter Faceoffs". Su location ("Washington, DC, USA")
    # no sale en el titulo y "250" no va pegado a "UFC", asi que ni ciudad ni
    # numero lo alcanzaban.
    event = TargetEvent(
        id=1084,
        name="UFC Freedom 250",
        location="Washington, DC, USA",
        event_date=date(2026, 6, 14),
    )
    feed = [FeedVideo("iDjZkLhkZw8", "UFC Freedom 250: Fighter Faceoffs", date(2026, 6, 13))]
    assert match_event(event, feed) == "iDjZkLhkZw8"


def test_ninguna_fila_real_suelta_un_token_de_marca_peligroso():
    # CENTINELA. Ejecutado el 11-sep-2026 sobre las 797 filas de `events`: solo
    # TRES sueltan token de marca, y son estas. Aqui van ademas una muestra de
    # cada familia que NO debe soltar ninguno. Si manana alguien afloja un
    # recorte, este test lo canta.
    sueltan = {
        "UFC Macao: Franklin vs Le": {"macao"},
        "UFC Freedom 250": {"freedom"},
        "Noche UFC: Silva vs. Delgado": {"noche"},
    }
    for nombre, esperado in sueltan.items():
        assert match_faceoffs._name_tokens(nombre) == esperado, nombre
    mudas = [
        "UFC Fight Night: Du Plessis vs. Usman",
        "UFC 299: Sandhagen vs. Vera 2",
        "The Ultimate Fighter 31 Finale: Jones vs Miller",
        "UFC on FOX: Johnson vs. Reis",
        "UFC on FUEL TV: Barao vs. McDonald",
        "UFC Live: Jones vs. Matyushenko",
        "Road To UFC: Maheshate vs. Flowers",
        "UFC - Road to UFC 4.6",
        "Crypto.com UFC 331: Van vs. Pantoja 2",
        "Ortiz vs Shamrock 3: The Final Chapter",
        "UFC Fight Night - Fight for the Troops",
    ]
    for nombre in mudas:
        assert match_faceoffs._name_tokens(nombre) == set(), nombre
    union = set()
    for nombre in list(sueltan) + mudas:
        union |= match_faceoffs._name_tokens(nombre)
    assert union == {"macao", "freedom", "noche"}


def test_sin_nombre_no_revienta():
    assert match_faceoffs._name_tokens(None) == set()
    assert match_faceoffs._name_tokens("") == set()
