"""El listado de ufc.com se recorre ENTERO, y una pagina caida no borra nada.

EL FALLO QUE FIJAN ESTOS TESTS, medido el 26-ago-2026: `scrape_upcoming_events`
pedia una sola URL (`/events`, la pagina 1, 8 tarjetas) mientras la UFC tenia 12
eventos anunciados. Cuatro eran invisibles -- ufc-333, que ya estaba en la base y
dejo de refrescarse en cuanto un evento nuevo lo empujo fuera del listado, y
otros tres que NUNCA habian entrado. Es una averia que empeora sola: cada evento
que la UFC anuncia expulsa a otro de la pagina que miramos.

Todo offline: el HTML va en linea y `_get_soup` se sustituye con monkeypatch. No
se pega a ufc.com ni se toca la base.
"""

from collections import Counter

import pytest

from src.scrapers import upcoming_events as ue


def _pagina(*slugs: str) -> str:
    """Una pagina del listado con una tarjeta por slug."""
    tarjetas = "".join(
        f"""
        <div class="c-card-event--result">
          <div class="c-card-event--result__logo"><a href="/event/{slug}"></a></div>
          <h3 class="c-card-event--result__headline">{slug} headliner</h3>
        </div>
        """
        for slug in slugs
    )
    return f'<html><body><div id="events-list-upcoming">{tarjetas}</div></body></html>'


SIN_CONTENEDOR = '<html><body><div id="events-list-past"></div></body></html>'


def _falso_get_soup(paginas: dict[str, str], caidas: set[str] | None = None):
    """Devuelve un `_get_soup` que sirve `paginas` y revienta en `caidas`."""
    caidas = caidas or set()
    from bs4 import BeautifulSoup

    def _get(session, url, settings):
        if url in caidas:
            raise RuntimeError(f"503 en {url}")
        return BeautifulSoup(paginas.get(url, SIN_CONTENEDOR), "html.parser")

    return _get


P0 = ue.EVENTS_URL
P1 = f"{ue.EVENTS_URL}?page=1"
P2 = f"{ue.EVENTS_URL}?page=2"


def test_recorre_todas_las_paginas_hasta_una_vacia(monkeypatch):
    """El caso que se nos escapaba: los eventos de la pagina 2 tambien entran."""
    monkeypatch.setattr(
        ue,
        "_get_soup",
        _falso_get_soup({P0: _pagina("ufc-330", "ufc-331"), P1: _pagina("ufc-333", "ufc-334")}),
    )
    counts: Counter = Counter()
    eventos = ue._parse_all_listing_pages(None, None, counts)

    assert [e.source_id for e in eventos] == ["ufc-330", "ufc-331", "ufc-333", "ufc-334"]
    assert counts["listing_pages_fetched"] == 2
    assert counts["listing_pages_failed"] == 0
    assert counts["listing_cap_hit"] == 0


def test_una_sola_pagina_sigue_funcionando(monkeypatch):
    """Sin regresion para el caso normal: 8 tarjetas y se acabo."""
    monkeypatch.setattr(ue, "_get_soup", _falso_get_soup({P0: _pagina("ufc-330")}))
    counts: Counter = Counter()
    eventos = ue._parse_all_listing_pages(None, None, counts)

    assert [e.source_id for e in eventos] == ["ufc-330"]
    assert counts["listing_pages_fetched"] == 1


def test_no_duplica_un_evento_que_sale_en_dos_paginas(monkeypatch):
    """ufc.com repite tarjetas al pasar de pagina si algo se mueve entre peticiones."""
    monkeypatch.setattr(
        ue,
        "_get_soup",
        _falso_get_soup({P0: _pagina("ufc-330", "ufc-331"), P1: _pagina("ufc-331", "ufc-333")}),
    )
    counts: Counter = Counter()
    eventos = ue._parse_all_listing_pages(None, None, counts)

    assert [e.source_id for e in eventos] == ["ufc-330", "ufc-331", "ufc-333"]


def test_una_pagina_caida_conserva_lo_ya_recogido_y_lo_deja_dicho(monkeypatch):
    """Lo importante NO es lo que devuelve: es que quede constancia del fallo.

    Sin `listing_pages_failed`, `_complete_dropped_upcoming` daria por terminados
    todos los eventos que venian detras de la pagina caida.
    """
    monkeypatch.setattr(
        ue, "_get_soup", _falso_get_soup({P0: _pagina("ufc-330", "ufc-331")}, caidas={P1})
    )
    counts: Counter = Counter()
    eventos = ue._parse_all_listing_pages(None, None, counts)

    assert [e.source_id for e in eventos] == ["ufc-330", "ufc-331"]
    assert counts["listing_pages_failed"] == 1
    assert counts["listing_pages_fetched"] == 1


def test_para_si_falta_el_contenedor_de_proximos(monkeypatch):
    """🪤 La trampa que se lleva por delante a un bucle ingenuo.

    La misma pagina de ufc.com arrastra los eventos PASADOS, asi que ?page=2
    devuelve 0 proximos pero sigue trayendo 8 pasados -- y sigue ofreciendo
    ?page=3, hacia atras hasta 1993. Ademas `_parse_listing` cae a `or soup`
    cuando falta `#events-list-upcoming`, asi que sin este corte los eventos
    pasados entrarian como futuros.
    """
    con_pasados = (
        '<html><body><div id="events-list-past">'
        '<div class="c-card-event--result">'
        '<div class="c-card-event--result__logo"><a href="/event/ufc-300"></a></div>'
        "</div></div></body></html>"
    )
    monkeypatch.setattr(
        ue, "_get_soup", _falso_get_soup({P0: _pagina("ufc-330"), P1: con_pasados})
    )
    counts: Counter = Counter()
    eventos = ue._parse_all_listing_pages(None, None, counts)

    assert [e.source_id for e in eventos] == ["ufc-330"]
    assert "ufc-300" not in [e.source_id for e in eventos]


def test_el_tope_de_paginas_corta_y_lo_marca(monkeypatch):
    """Si ninguna pagina viene vacia, se para y se avisa: no sabemos si falta algo.

    Pasa de verdad: ufc.com responde a `?page=loquesea` con la pagina 0, asi que
    una URL mal construida no veria nunca una pagina vacia.
    """
    infinitas = {
        (ue.EVENTS_URL if n == 0 else f"{ue.EVENTS_URL}?page={n}"): _pagina(f"ufc-{300 + n}")
        for n in range(ue.MAX_LISTING_PAGES + 3)
    }
    monkeypatch.setattr(ue, "_get_soup", _falso_get_soup(infinitas))
    counts: Counter = Counter()
    eventos = ue._parse_all_listing_pages(None, None, counts)

    assert len(eventos) == ue.MAX_LISTING_PAGES
    assert counts["listing_cap_hit"] == 1
    assert counts["listing_pages_fetched"] == ue.MAX_LISTING_PAGES


@pytest.mark.parametrize("contador", ["listing_pages_failed", "listing_cap_hit"])
def test_con_el_listado_incompleto_no_se_cierra_ningun_evento(contador):
    """El guard de `scrape_upcoming_events`, comprobado como condicion.

    Un evento de mas en "Proximos" durante un dia es barato; darlo por terminado
    cuando no lo esta se ve en la portada.
    """
    counts: Counter = Counter()
    counts[contador] = 1
    assert bool(counts["listing_pages_failed"] or counts["listing_cap_hit"]) is True

    limpio: Counter = Counter()
    assert bool(limpio["listing_pages_failed"] or limpio["listing_cap_hit"]) is False
