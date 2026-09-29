"""Un detalle de ufc.com caido o vacio ya no cancela ni renombra la cartelera.

EL FALLO QUE FIJAN ESTOS TESTS, medido el 29-sep-2026: si la ficha de un evento
fallaba, `scrape_upcoming_events` lo escribia IGUAL, con `event.bouts=[]`.
`cancel_missing_upcoming_fights` recibia una lista vacia y, con su centinela
[-1], cancelaba la cartelera ENTERA; y `upsert_event_meta` le cambiaba el nombre
por el del cartel pelado ("Silva vs Wang"). Tres caminos, todos en verde:

  (a) un 403/5xx o un timeout: `detail_errors` subia a 1... y se escribia igual;
  (b) un 200 sin og:title (muro anti-bot): 0 errores y el nombre a 'UFC: X vs. Y';
  (c) un HTML cambiado sin `.c-listing-fight`: todas las carteleras a la vez.

El cron corre tambien el sabado por la manana: si pasa el 3-oct, la cartelera
del UFC 332 desaparece de la web durante todo el directo.

Aqui NO se toca `cancel_missing_upcoming_fights` ni su [-1] (lo fija
test_title_and_cancelled.py): la guarda va una capa por encima, en `_write_event`.

Todo offline: HTML en linea, `_get_soup` sustituido con monkeypatch y el `fakedb`
de conftest.py. Aqui nunca se llama a `scrape_upcoming_events`: abre su propia
conexion y, con el DATABASE_URL que exporta el megatest, escribiria en PRODUCCION.
Su bucle se prueba aparte, en test_upcoming_card_guard_loop.py, con la conexion
y la red sustituidas.
"""

import json
import logging
import re
from collections import Counter

import pytest
import requests
from bs4 import BeautifulSoup

from src.scrapers import upcoming_events as ue
from src.scrapers.repositories import fights as fights_repo

LOGGER_NAME = "src.scrapers.upcoming_events"


def _ficha(*combates: tuple[str, str, str], og_title: str | None = "UFC 332 | UFC") -> str:
    """Ficha de evento de ufc.com con un `.c-listing-fight` por (fmid, rojo, azul)."""
    cabecera = f'<meta property="og:title" content="{og_title}">' if og_title else ""
    tarjetas = "".join(
        f"""
        <div class="c-listing-fight" data-fmid="{fmid}">
          <div class="c-listing-fight__corner-name--red">{rojo}</div>
          <div class="c-listing-fight__corner-name--blue">{azul}</div>
        </div>
        """
        for fmid, rojo, azul in combates
    )
    return f"<html><head>{cabecera}</head><body>{tarjetas}</body></html>"


def _url(slug: str) -> str:
    return f"https://www.ufc.com/event/{slug}"


def _evento(slug: str = "ufc-332", n_combates: int = 0, detail_ok: bool = True) -> ue.ParsedEvent:
    """Un evento ya leido: `n_combates` en la ficha y el detalle bien o mal."""
    combates = [
        ue.ParsedBout(
            card_segment=None, bout_order=i, weight_class=None, scheduled_rounds=3,
            red_name=f"Rojo {i}", blue_name=f"Azul {i}", fmid=f"{slug}-{i}",
        )
        for i in range(1, n_combates + 1)
    ]
    return ue.ParsedEvent(
        source_id=slug,
        detail_url=_url(slug),
        headliner="Silva vs Wang",
        event_date=None,
        start_time=None,
        location=None,
        ticket_url=None,
        name="UFC 332: Silva vs. Wang",
        bouts=combates,
        detail_ok=detail_ok,
    )


def _evento_del_listado(slug: str) -> ue.ParsedEvent:
    """Como sale de `_parse_listing`: sin detalle todavia."""
    return ue.ParsedEvent(
        source_id=slug,
        detail_url=_url(slug),
        headliner="Silva vs Wang",
        event_date=None,
        start_time=None,
        location=None,
        ticket_url=None,
    )


def _base(activos: int, cancelados_por_ufc: int = 0, slug: str = "ufc-332"):
    """Responder del fakedb: la base tiene `activos` combates vivos en el evento `slug`.

    🪤 Tiene que distinguir el COUNT del resto: `RecordingCursor.rowcount` es
    `len(resultado)`, asi que la cancelacion devuelve una fila por combate que
    Postgres pasaria a 'cancelled'.

    🪤 Y el COUNT solo da `activos` si pregunta por ESE evento de ufc.com; con
    otro slug, con el nombre del evento o con otra fuente da 0, como la base de
    verdad. Si contestara a cualquier pregunta, una guarda que contara el evento
    equivocado pasaria estos tests y en produccion no retendria nunca nada.
    """
    ids = iter(range(100, 200))

    def responder(sql, params=None):
        plano = " ".join(sql.split())
        if "COUNT(*)" in plano:
            return [(activos,)] if params == ("ufc.com", slug, "ufc.com") else [(0,)]
        if plano.startswith("SELECT id FROM events"):
            return [(1092,)]  # el evento ya existe: rama UPDATE de upsert_event_meta
        if plano.startswith("INSERT INTO fights"):
            return [(next(ids),)]
        if "SET status = 'cancelled'" in plano:
            return [(9000 + i,) for i in range(cancelados_por_ufc)]
        return []

    return responder


def _escribe(fakedb, evento, activos, cancelados_por_ufc=0, forzar=None):
    conn = fakedb.Connection(_base(activos, cancelados_por_ufc, evento.source_id))
    counts: Counter = Counter()
    escrito = ue._write_event(conn, lambda nombre: None, counts, evento, 1, forzar)
    return conn, counts, escrito


# ------------------------------------------------------ el umbral de la guarda


@pytest.mark.parametrize(
    ("leidos", "activos", "salta"),
    [
        (0, 14, True),    # el sabado del UFC 332 con la ficha vacia
        (0, 0, False),    # evento recien anunciado, 'TBD vs. TBD': legitimo
        (0, 1, True),
        (1, 1, False),
        (1, 2, False),    # Road To UFC: pierde 1 de 2 y es normal
        (1, 3, True),
        (6, 14, True),    # menos de la mitad
        (7, 14, True),    # justo la mitad, pero caen 7 de golpe
        (10, 14, True),   # caen 4 de golpe: mas de 3
        (11, 14, False),  # caen 3: el maximo legitimo medido es 2
        (14, 14, False),
        (15, 14, False),  # combate nuevo en la cartelera
        (4, 8, True),
        (5, 8, False),
    ],
)
def test_umbral_de_la_guarda(leidos, activos, salta):
    assert ue._card_looks_broken(leidos, activos) is salta


# ------------------------------------------------------ la ficha: detail_ok


def test_detail_ok_nace_en_falso():
    """Un evento recien sacado del listado todavia no tiene detalle bueno."""
    assert _evento_del_listado("ufc-332").detail_ok is False


def test_una_ficha_sin_og_title_es_un_detalle_fallido():
    """Camino (b): un 200 sin og:title no es la ficha de un evento.

    En 38 eventos ufc.com nunca ha faltado (no hay un solo nombre 'UFC: ...' en
    la base) y ufc-335, anunciado sin combates, si lo trae. Si falta, es un muro
    anti-bot o una pagina rota, y se lanza ANTES de tocar el evento.
    """
    evento = _evento_del_listado("ufc-332")
    with pytest.raises(ValueError, match="og:title"):
        ue._parse_detail(BeautifulSoup(_ficha(og_title=None), "lxml"), evento)
    assert evento.name is None


def test_fetch_details_un_detalle_caido_solo_marca_a_ese_evento(monkeypatch):
    """Camino (a): el 503 cuenta como detail_error y deja ESE evento sin detalle."""
    fichas = {
        _url("ufc-332"): _ficha(("1", "Natalia Silva", "Wang Cong"), ("2", "A B", "C D")),
        _url("ufc-334"): _ficha(("3", "E F", "G H"), og_title="UFC 334 | UFC"),
    }

    def _get(session, url, settings):
        if url == _url("ufc-333"):
            raise requests.HTTPError("503 Server Error")
        return BeautifulSoup(fichas[url], "lxml")

    monkeypatch.setattr(ue, "_get_soup", _get)
    eventos = [_evento_del_listado(s) for s in ("ufc-332", "ufc-333", "ufc-334")]
    counts: Counter = Counter()
    ue._fetch_details(None, None, eventos, counts)

    assert [e.detail_ok for e in eventos] == [True, False, True]
    assert counts["detail_errors"] == 1
    assert counts["details_fetched"] == 2
    assert counts["bouts_parsed"] == 3
    assert eventos[0].name == "UFC 332: Silva vs. Wang"


def test_fetch_details_un_evento_anunciado_sin_combates_es_un_detalle_bueno(monkeypatch):
    """ufc-335 el 29-sep: 200, og:title 'UFC 335 | UFC' y 0 combates. Es legitimo."""
    monkeypatch.setattr(
        ue, "_get_soup", lambda s, url, st: BeautifulSoup(_ficha(og_title="UFC 335 | UFC"), "lxml")
    )
    eventos = [_evento_del_listado("ufc-335")]
    counts: Counter = Counter()
    ue._fetch_details(None, None, eventos, counts)

    assert eventos[0].detail_ok is True
    assert eventos[0].bouts == []
    assert counts["detail_errors"] == 0


def test_fetch_details_un_muro_sin_og_title_cuenta_como_error(monkeypatch):
    muro = "<html><body>Just a moment...</body></html>"
    monkeypatch.setattr(ue, "_get_soup", lambda s, url, st: BeautifulSoup(muro, "lxml"))
    eventos = [_evento_del_listado("ufc-332")]
    counts: Counter = Counter()
    ue._fetch_details(None, None, eventos, counts)

    assert eventos[0].detail_ok is False
    assert counts["detail_errors"] == 1
    assert counts["details_fetched"] == 0


# ------------------------------------------------------ _write_event


def test_con_el_detalle_fallido_no_se_toca_nada(fakedb, caplog):
    """Ni el nombre, ni la fecha, ni un solo combate. Ni siquiera se pregunta a la base."""
    sin_ficha = _evento(n_combates=0, detail_ok=False)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        conn, counts, escrito = _escribe(fakedb, sin_ficha, activos=14)

    assert escrito is False
    assert fakedb.executed_statements(conn) == []
    assert counts["events_skipped_detail"] == 1
    assert counts["cards_guarded"] == 0
    assert "ufc-332" in caplog.text


def test_una_ficha_vacia_con_14_activos_no_cancela_ni_renombra(fakedb, caplog):
    """El caso del sabado: 200 vacio, detalle 'bueno' y 14 combates en la base."""
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        conn, counts, escrito = _escribe(fakedb, _evento(n_combates=0), activos=14)

    assert escrito is False
    assert fakedb.mutating_statements(conn) == []
    todo = " ".join(fakedb.executed_statements(conn))
    assert "UPDATE events" not in todo
    assert "SET status = 'cancelled'" not in todo
    assert counts["cards_guarded"] == 1
    assert counts["bouts_cancelled"] == 0
    assert (
        "ufc-332: ufc.com da 0 combates y la base tiene 14 activos; no se toca el evento"
        in caplog.text
    )


def test_media_cartelera_de_golpe_tampoco_se_cancela(fakedb):
    """Camino (d): una ficha parcial (solo la estelar) no se lleva 7 combates."""
    conn, counts, escrito = _escribe(fakedb, _evento(n_combates=7), activos=14)

    assert escrito is False
    assert fakedb.mutating_statements(conn) == []
    assert counts["cards_guarded"] == 1


def test_un_evento_recien_anunciado_sin_combates_si_se_escribe(fakedb):
    """0 leidos y 0 activos: 'TBD vs. TBD' (1097, 1100, 1101 hoy). Se escribe como siempre."""
    conn, counts, escrito = _escribe(fakedb, _evento("ufc-335", n_combates=0), activos=0)

    assert escrito is True
    assert any(s.startswith("UPDATE events SET") for s in fakedb.mutating_statements(conn))
    assert counts["cards_guarded"] == 0


@pytest.mark.parametrize(
    ("slug", "leidos", "activos"),
    [
        ("road-to-ufc-5", 1, 2),  # Road To UFC: 2 -> 1
        ("ufc-332", 13, 14),      # una baja normal de semana de pelea
    ],
)
def test_una_baja_legitima_se_escribe_y_se_cancela_lo_que_falta(fakedb, slug, leidos, activos):
    conn, counts, escrito = _escribe(
        fakedb, _evento(slug, n_combates=leidos), activos=activos, cancelados_por_ufc=1
    )

    assert escrito is True
    mutaciones = fakedb.mutating_statements(conn)
    assert any(s.startswith("UPDATE events SET") for s in mutaciones)
    assert sum(s.startswith("INSERT INTO fights") for s in mutaciones) == leidos
    assert any("SET status = 'cancelled'" in s for s in mutaciones)
    assert counts["bouts_cancelled"] == 1
    assert counts["cards_guarded"] == 0


# ------------------------------------------------------ --forzar-evento


def test_forzar_un_evento_solo_afecta_a_ese_slug(fakedb, caplog):
    """Una bajada REAL que la guarda retiene se fuerza a mano, slug a slug.

    El 332 pierde 4 de 14 de verdad y se fuerza con los 10 que se ven en
    ufc.com. El 333, con la misma bajada y sin forzar, sigue retenido.
    """
    forzar = {"ufc-332": 10}
    bajada = {"activos": 14, "cancelados_por_ufc": 4, "forzar": forzar}
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        conn_332, counts_332, escrito_332 = _escribe(fakedb, _evento("ufc-332", 10), **bajada)
        conn_333, counts_333, escrito_333 = _escribe(fakedb, _evento("ufc-333", 10), **bajada)

    assert escrito_332 is True
    assert any("SET status = 'cancelled'" in s for s in fakedb.mutating_statements(conn_332))
    assert counts_332["bouts_cancelled"] == 4
    assert counts_332["cards_forced"] == 1
    assert counts_332["cards_guarded"] == 0

    assert escrito_333 is False
    assert fakedb.mutating_statements(conn_333) == []
    assert counts_333["cards_guarded"] == 1
    assert counts_333["cards_forced"] == 0
    assert "ufc-332" in caplog.text and "forzado" in caplog.text.lower()


@pytest.mark.parametrize("leidos", [0, 4, 9])
def test_forzar_no_escribe_si_en_ese_pase_ufc_com_da_otro_numero(fakedb, caplog, leidos):
    """El forzado no se fia de la ficha de ESE pase: exige los combates que se vieron.

    El caso del sabado: la guarda retiene una bajada real, el dueno la comprueba
    en el navegador y lanza el dispatch con los 10 que ve... y justo en ese pase
    ufc.com sirve la ficha vacia o a medias. Si el forzado escribiera lo que
    llegase, cancelaria la cartelera entera, y en VERDE. Se retiene como
    siempre, en rojo, y el WARNING dice que no cuadra.
    """
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        conn, counts, escrito = _escribe(
            fakedb, _evento("ufc-332", leidos), activos=14, cancelados_por_ufc=14 - leidos,
            forzar={"ufc-332": 10},
        )

    assert escrito is False
    assert fakedb.mutating_statements(conn) == []
    assert counts["cards_guarded"] == 1
    assert counts["cards_forced"] == 0
    assert f"ufc-332: --forzar-evento esperaba 10 combates y ufc.com da {leidos}" in caplog.text


def test_forzar_no_escribe_si_ufc_com_da_mas_de_los_que_se_vieron(fakedb, caplog):
    """«Exactamente N» vale en los dos sentidos: con MÁS combates que N tampoco.

    El 332 baja de 14 a 10 (la guarda salta: 4 de golpe) y alguien lo fuerza
    con 9. No cuadra con lo que se vio, así que se retiene como siempre.
    """
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        conn, counts, escrito = _escribe(
            fakedb, _evento("ufc-332", 10), activos=14, cancelados_por_ufc=4,
            forzar={"ufc-332": 9},
        )

    assert escrito is False
    assert fakedb.mutating_statements(conn) == []
    assert counts["cards_guarded"] == 1
    assert counts["cards_forced"] == 0
    assert "ufc-332: --forzar-evento esperaba 9 combates y ufc.com da 10" in caplog.text


def test_forzar_nunca_escribe_una_cartelera_vacia(fakedb):
    """Ni pidiendolo con 0: una ficha sin combates es el fallo que la guarda para.

    La linea de comandos ya no acepta N=0; esto cubre a quien llame a la funcion
    directamente.
    """
    conn, counts, escrito = _escribe(
        fakedb, _evento("ufc-332", 0), activos=14, cancelados_por_ufc=14, forzar={"ufc-332": 0}
    )

    assert escrito is False
    assert fakedb.mutating_statements(conn) == []
    assert counts["cards_guarded"] == 1
    assert counts["cards_forced"] == 0


def test_forzar_nunca_se_salta_la_guarda_del_detalle(fakedb):
    """Forzar es para una bajada real que ufc.com SI publica, no para una ficha caida."""
    conn, counts, escrito = _escribe(
        fakedb, _evento(n_combates=0, detail_ok=False), activos=14, forzar={"ufc-332": 14}
    )

    assert escrito is False
    assert fakedb.executed_statements(conn) == []
    assert counts["events_skipped_detail"] == 1
    assert counts["cards_forced"] == 0


# ------------------------------------------------------ los tres caminos, de punta a punta


@pytest.mark.parametrize(
    "camino",
    ["a_http_503", "b_200_sin_og_title", "c_html_cambiado"],
)
def test_ninguno_de_los_tres_caminos_toca_el_ufc_332(monkeypatch, fakedb, camino):
    """Ficha -> `_fetch_details` -> `_write_event`, con 14 combates vivos en la base."""
    def _get(session, url, settings):
        if camino == "a_http_503":
            raise requests.HTTPError("503 Server Error")
        if camino == "b_200_sin_og_title":
            return BeautifulSoup("<html><body></body></html>", "lxml")
        return BeautifulSoup(_ficha(), "lxml")  # og:title bien, sin .c-listing-fight

    monkeypatch.setattr(ue, "_get_soup", _get)
    evento = _evento_del_listado("ufc-332")
    counts: Counter = Counter()
    ue._fetch_details(None, None, [evento], counts)
    conn = fakedb.Connection(_base(activos=14, cancelados_por_ufc=14))
    escrito = ue._write_event(conn, lambda nombre: None, counts, evento, 1)

    assert escrito is False
    assert fakedb.mutating_statements(conn) == []
    assert counts["bouts_cancelled"] == 0
    assert counts["events_skipped_detail"] + counts["cards_guarded"] == 1


# ------------------------------------------------------ el recuento de la base


def test_el_recuento_usa_el_mismo_where_que_la_cancelacion(fakedb):
    """Se cuenta EXACTAMENTE lo que la cancelacion podria tocar, ni mas ni menos."""
    conn_cancel = fakedb.Connection(lambda sql, params=None: [])
    fights_repo.cancel_missing_upcoming_fights(conn_cancel, 1092, "ufc.com", [11])
    cancel = " ".join(fakedb.executed_statements(conn_cancel)[0].split())

    conn = fakedb.Connection(lambda sql, params=None: [(14,)])
    activos = fights_repo.count_active_upcoming_fights(conn, "ufc.com", "ufc-332")
    (sql, params), = conn.cursors[0].executed
    cuenta = " ".join(sql.split())

    assert activos == 14
    for filtro in (
        "source = %s",
        "winner_id IS NULL AND method IS NULL",
        "status IS DISTINCT FROM 'cancelled'",
    ):
        assert filtro in cancel and filtro in cuenta, filtro
    # El filtro de fuente de los COMBATES, no solo el del evento: "e.source = %s"
    # tambien contiene "source = %s", asi que sin esto un COUNT que perdiera el
    # filtro de fuera seguiria pasando el bucle de arriba.
    fuente_de_los_combates = re.compile(r"(?<![.\w])source = %s")
    assert fuente_de_los_combates.search(cancel) and fuente_de_los_combates.search(cuenta)
    assert "NOT (id = ANY" not in cuenta
    assert params == ("ufc.com", "ufc-332", "ufc.com")


def test_la_guarda_cuenta_los_activos_por_el_slug_y_la_fuente_de_ufc_com(fakedb):
    """Ni por el nombre del evento, que no es clave, ni con otra fuente.

    Las dos preguntas darian 0 en la base de verdad, y con 0 activos la guarda
    no retiene nunca: se apagaria sin que ningun otro test se enterase.
    """
    conn, _, _ = _escribe(fakedb, _evento("ufc-332", n_combates=14), activos=14)

    cuentas = [p for cur in conn.cursors for sql, p in cur.executed if "COUNT(*)" in sql]
    assert cuentas == [("ufc.com", "ufc-332", "ufc.com")]


def test_un_evento_que_no_esta_en_la_base_tiene_cero_activos(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert fights_repo.count_active_upcoming_fights(conn, "ufc.com", "ufc-340") == 0


# ------------------------------------------------------ el resumen


def test_el_resumen_saca_los_contadores_nuevos_aunque_sean_cero():
    resumen = json.loads(ue._build_summary(Counter()))
    for clave in ("events_skipped_detail", "cards_guarded", "cards_forced"):
        assert resumen[clave] == 0, clave
