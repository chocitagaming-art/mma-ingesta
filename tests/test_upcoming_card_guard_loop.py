"""La guarda de la cartelera, en el bucle DE VERDAD de `scrape_upcoming_events`.

POR QUE ESTE FICHERO (revision del 29-sep-2026). test_upcoming_card_guard.py
prueba `_fetch_details` y `_write_event` sueltos, pero quien los cablea es el
bucle de `scrape_upcoming_events`: que `detail_ok` salga de `_fetch_details`,
que los eventos forzados lleguen hasta `_write_event`, y que solo se haga commit
y se cuente `events_written` si el evento se ha escrito de verdad. Cinco
mutaciones de ese cableado pasaban la suite entera en verde. Dos apagaban la
guarda en produccion sin que se notara: contar los activos por el nombre del
evento, o con otra fuente, da 0 en la base de verdad, y con 0 no retiene nunca.

🪤 COMO SE LLAMA A `scrape_upcoming_events` SIN TOCAR PRODUCCION. Abre su propia
conexion con `connect(get_settings().database_url)`, y el megatest exporta el
DATABASE_URL de verdad. Aqui se sustituyen `ue.get_settings` (con una URL
ficticia), `ue.connect` (que solo acepta esa URL y no abre nada),
`ue._new_session`, `ue._get_soup` y `ue.get_all_fighters`. Y por si un cambio de
manana abriera otra puerta, `psycopg2.connect` y las peticiones de `requests`
revientan: aqui no se abre un socket, ni a Neon ni a ufc.com.

La base es un modelo en memoria de las SQL que corre el bucle, con eventos y
combates: el COUNT de la guarda cuenta lo que haya para el slug y la fuente por
los que pregunta, como Postgres. Una SQL que el modelo no conoce revienta, sale
como `write_errors`, y todos los tests lo miran.
"""

import logging
from types import SimpleNamespace

import psycopg2
import pytest
import requests
from bs4 import BeautifulSoup

from src.scrapers import upcoming_events as ue

LOGGER_NAME = "src.scrapers.upcoming_events"
URL_FICTICIA = "postgresql://nadie@ninguna-parte/base-en-memoria"
PREGUNTA_DEL_332 = ("ufc.com", "ufc-332", "ufc.com")


# ------------------------------------------------------ ufc.com, en linea


def _listado(*slugs: str) -> str:
    """Una pagina del listado con una tarjeta por slug; sin slugs, la del final."""
    tarjetas = "".join(
        f"""
        <div class="c-card-event--result">
          <div class="c-card-event--result__logo"><a href="/event/{slug}"></a></div>
          <h3 class="c-card-event--result__headline">Silva vs Wang</h3>
        </div>
        """
        for slug in slugs
    )
    return f'<html><body><div id="events-list-upcoming">{tarjetas}</div></body></html>'


def _ficha(slug: str, n_combates: int) -> str:
    """La ficha de un evento con sus `n_combates` primeros combates.

    El combate i lleva siempre el mismo fmid ('<slug>-i') y la misma pareja, asi
    que una ficha con menos combates es una bajada de los ultimos de la lista.
    """
    titulo = slug.upper().replace("-", " ")  # "ufc-332" -> "UFC 332"
    tarjetas = "".join(
        f"""
        <div class="c-listing-fight" data-fmid="{slug}-{i}">
          <div class="c-listing-fight__corner-name--red">Rojo {i}</div>
          <div class="c-listing-fight__corner-name--blue">Azul {i}</div>
        </div>
        """
        for i in range(1, n_combates + 1)
    )
    return (
        f'<html><head><meta property="og:title" content="{titulo} | UFC"></head>'
        f"<body>{tarjetas}</body></html>"
    )


def _fichas_de_hoy(ufc_332=None) -> dict:
    """Un dia normal: el UFC 332 entero (14), el 335 anunciado sin combates y el
    336, que es nuevo, con 5. `ufc_332` cambia la ficha del 332: otro HTML, o
    una excepcion para un fallo HTTP."""
    return {
        "ufc-332": _ficha("ufc-332", 14) if ufc_332 is None else ufc_332,
        "ufc-335": _ficha("ufc-335", 0),
        "ufc-336": _ficha("ufc-336", 5),
    }


# ------------------------------------------------------ la base, en memoria


class BaseEnMemoria:
    """Eventos y combates, y las SQL que el bucle corre sobre ellos.

    Hace de responder del `fakedb` de conftest.py, cuyo `rowcount` es
    `len(resultado)`: cada SQL devuelve las filas que devolveria Postgres.
    """

    def __init__(self) -> None:
        self.eventos: dict[int, dict] = {}
        self.combates: dict[int, dict] = {}
        self.eventos_escritos: list[str] = []
        self._ids = iter(range(50_000, 60_000))

    def evento(self, event_id: int, slug: str, n_combates: int) -> None:
        """Un evento de ufc.com ya en la base, con los combates que da `_ficha`."""
        self.eventos[event_id] = {
            "source": "ufc.com", "source_id": slug, "status": "upcoming",
            "name": f"{slug.upper().replace('-', ' ')}: Silva vs. Wang",
        }
        for i in range(1, n_combates + 1):
            self.combates[next(self._ids)] = {
                "event_id": event_id, "source": "ufc.com", "source_id": f"{slug}-{i}",
                "rojo": f"Rojo {i}", "azul": f"Azul {i}",
                "status": None, "winner_id": None, "method": None,
            }

    def activos(self, slug: str) -> int:
        return len(self._vivos(self._id_del_evento("ufc.com", slug), "ufc.com"))

    def _id_del_evento(self, source: str, slug: str) -> int | None:
        return next(
            (i for i, e in self.eventos.items() if (e["source"], e["source_id"]) == (source, slug)),
            None,
        )

    def _vivos(self, event_id: int | None, source: str) -> list[int]:
        """El WHERE de cancel_missing_upcoming_fights, que es el de la guarda."""
        return [
            i for i, c in self.combates.items()
            if c["event_id"] == event_id and c["source"] == source
            and c["winner_id"] is None and c["method"] is None
            and c["status"] != "cancelled"
        ]

    def __call__(self, sql, params=None):
        plano = " ".join(sql.split())
        # _complete_dropped_upcoming
        if plano.startswith("SELECT id, source_id FROM events"):
            return [
                (i, e["source_id"]) for i, e in self.eventos.items()
                if e["source"] == params[0] and e["status"] == "upcoming"
            ]
        if plano.startswith("UPDATE events SET status = 'completed'"):
            return []  # los eventos de aqui no tienen fecha: nunca se cierran
        # La guarda: count_active_upcoming_fights
        if plano.startswith("SELECT COUNT(*) FROM fights"):
            source, slug, source_de_los_combates = params
            event_id = self._id_del_evento(source, slug)
            return [(len(self._vivos(event_id, source_de_los_combates)) if event_id else 0,)]
        # upsert_event_meta
        if plano.startswith("SELECT id FROM events"):
            event_id = self._id_del_evento(*params)
            return [(event_id,)] if event_id else []
        if plano.startswith("UPDATE events SET name = %s"):
            evento = self.eventos[params[-1]]
            evento.update(name=params[0], status=params[5])
            self.eventos_escritos.append(evento["source_id"])
            return []
        if plano.startswith("INSERT INTO events"):
            event_id = next(self._ids)
            self.eventos[event_id] = {
                "source": params[11], "source_id": params[12],
                "status": params[5], "name": params[0],
            }
            self.eventos_escritos.append(params[12])
            return [(event_id,)]
        # _write_event_bouts
        if plano.startswith("UPDATE fights SET source_id"):
            return self._reconcile(params)
        if plano.startswith("INSERT INTO fights"):
            return self._upsert_combate(params)
        if plano.startswith("UPDATE fights SET status = 'cancelled'"):
            event_id, source, conservados = params
            cancelados = [i for i in self._vivos(event_id, source) if i not in conservados]
            for i in cancelados:
                self.combates[i]["status"] = "cancelled"
            return [(i,) for i in cancelados]
        raise AssertionError(f"SQL que el modelo no conoce: {plano[:100]}")

    def _reconcile(self, p: dict) -> list:
        """Aqui un combate no cambia nunca de fmid, asi que no hay nada que adoptar.

        Se comprueba en vez de suponerlo: si un escenario de manana moviera un
        fmid, el modelo revienta en vez de contestar lo que Postgres no contestaria.
        """
        pareja = {p["red_name"].lower(), p["blue_name"].lower()}
        for c in self.combates.values():
            if (c["event_id"], c["source"]) == (p["event_id"], p["source"]) and (
                {c["rojo"].lower(), c["azul"].lower()} == pareja
            ):
                assert c["source_id"] == p["source_id"], f"fmid movido: {p['source_id']}"
        return []

    def _upsert_combate(self, params: tuple) -> list:
        event_id, _rojo_id, _azul_id, rojo, azul, *_resto, source, source_id, _titulo = params
        for i, c in self.combates.items():
            if (c["source"], c["source_id"]) == (source, source_id):
                c.update(event_id=event_id, rojo=rojo, azul=azul, status=None)
                return [(i,)]
        i = next(self._ids)
        self.combates[i] = {
            "event_id": event_id, "source": source, "source_id": source_id,
            "rojo": rojo, "azul": azul, "status": None, "winner_id": None, "method": None,
        }
        return [(i,)]


def _base_de_hoy() -> BaseEnMemoria:
    """Como la del 29-sep: el UFC 332 (1092) con 14 vivos y el 335 (1097) sin combates."""
    base = BaseEnMemoria()
    base.evento(1092, "ufc-332", n_combates=14)
    base.evento(1097, "ufc-335", n_combates=0)
    return base


def _cuentas(conn) -> list:
    """Los parametros de cada COUNT de la guarda, en orden."""
    return [p for cur in conn.cursors for sql, p in cur.executed if "COUNT(*)" in sql]


@pytest.fixture
def pase(monkeypatch, fakedb):
    """Un pase del cron de punta a punta: ufc.com en linea y la base en memoria."""

    def _nunca(que: str):
        def _revienta(*args, **kwargs):
            raise AssertionError(f"este test no {que}")

        return _revienta

    monkeypatch.setattr(psycopg2, "connect", _nunca("abre una base de verdad"))
    monkeypatch.setattr(requests.Session, "request", _nunca("sale a la red"))

    def _pase(base: BaseEnMemoria, fichas: dict, forzar=()):
        conn = fakedb.Connection(base)

        def _connect(url):
            assert url == URL_FICTICIA, "solo se abre la base en memoria"
            return conn

        def _get_soup(session, url, settings):
            if url == ue.EVENTS_URL:
                return BeautifulSoup(_listado(*fichas), "lxml")
            if url.startswith(ue.EVENTS_URL):  # ?page=1: no quedan mas proximos
                return BeautifulSoup(_listado(), "lxml")
            ficha = fichas[url.rsplit("/event/", 1)[1]]
            if isinstance(ficha, Exception):
                raise ficha
            return BeautifulSoup(ficha, "lxml")

        monkeypatch.setattr(
            ue,
            "get_settings",
            lambda: SimpleNamespace(
                database_url=URL_FICTICIA,
                promotion_id_ufc=1,
                request_delay_seconds=0,
                request_timeout_seconds=1,
            ),
        )
        monkeypatch.setattr(ue, "connect", _connect)
        monkeypatch.setattr(ue, "_new_session", lambda: SimpleNamespace(get=lambda *a, **k: None))
        monkeypatch.setattr(ue, "_get_soup", _get_soup)
        monkeypatch.setattr(ue, "get_all_fighters", lambda connection: [])
        return ue.scrape_upcoming_events(forzar_eventos=forzar), conn

    return _pase


# ------------------------------------------------------ un dia normal


def test_un_dia_normal_escribe_los_tres_eventos(pase):
    base = _base_de_hoy()
    counts, conn = pase(base, _fichas_de_hoy())

    assert base.eventos_escritos == ["ufc-332", "ufc-335", "ufc-336"]
    assert counts["events_written"] == 3
    assert counts["bouts_written"] == 14 + 0 + 5
    assert conn.commits == 1 + 3  # el cierre de los pasados, y uno por evento escrito
    assert base.activos("ufc-332") == 14
    assert base.activos("ufc-336") == 5  # el evento nuevo entra con su cartelera
    for clave in (
        "detail_errors", "events_skipped_detail", "cards_guarded",
        "cards_forced", "bouts_cancelled", "write_errors",
    ):
        assert counts[clave] == 0, clave
    # Cada evento se cuenta por SU slug y con la fuente de ufc.com.
    assert _cuentas(conn) == [
        PREGUNTA_DEL_332,
        ("ufc.com", "ufc-335", "ufc.com"),
        ("ufc.com", "ufc-336", "ufc.com"),
    ]


# ------------------------------------------------------ la ficha del 332, caida


@pytest.mark.parametrize(
    "ficha_caida",
    [
        requests.HTTPError("503 Server Error"),  # camino (a)
        "<html><body>Just a moment...</body></html>",  # camino (b): 200 sin og:title
    ],
    ids=["a_http_503", "b_200_sin_og_title"],
)
def test_con_la_ficha_del_332_caida_no_se_toca_el_evento(pase, ficha_caida):
    base = _base_de_hoy()
    counts, conn = pase(base, _fichas_de_hoy(ufc_332=ficha_caida))

    assert counts["detail_errors"] == 1
    assert counts["events_skipped_detail"] == 1
    assert "ufc-332" not in base.eventos_escritos
    assert base.activos("ufc-332") == 14
    assert PREGUNTA_DEL_332 not in _cuentas(conn)  # ni siquiera se pregunta a la base
    # Los otros dos si se escriben, y solo ellos llevan commit y cuentan.
    assert counts["events_written"] == 2
    assert conn.commits == 1 + 2
    assert counts["cards_guarded"] == 0 and counts["write_errors"] == 0


def test_una_ficha_del_332_sin_combates_la_retiene_la_guarda(pase, caplog):
    """Camino (c): og:title bien y ni un `.c-listing-fight`, con 14 vivos en la base."""
    base = _base_de_hoy()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        counts, conn = pase(base, _fichas_de_hoy(ufc_332=_ficha("ufc-332", 0)))

    assert counts["cards_guarded"] == 1
    assert "ufc-332" not in base.eventos_escritos
    assert base.activos("ufc-332") == 14
    assert counts["bouts_cancelled"] == 0
    assert PREGUNTA_DEL_332 in _cuentas(conn)
    assert counts["events_written"] == 2
    assert conn.commits == 1 + 2
    assert counts["write_errors"] == 0
    assert (
        "ufc-332: ufc.com da 0 combates y la base tiene 14 activos; no se toca el evento"
        in caplog.text
    )


@pytest.mark.parametrize(("leidos", "retenida"), [(13, False), (11, False), (10, True), (7, True)])
def test_una_bajada_del_332_se_mide_contra_los_activos_de_la_base(pase, leidos, retenida):
    """Hasta 3 de golpe se escriben y se cancelan; 4 o mas se retienen."""
    base = _base_de_hoy()
    counts, _ = pase(base, _fichas_de_hoy(ufc_332=_ficha("ufc-332", leidos)))

    assert counts["cards_guarded"] == int(retenida)
    assert ("ufc-332" in base.eventos_escritos) is not retenida
    assert base.activos("ufc-332") == (14 if retenida else leidos)
    assert counts["bouts_cancelled"] == (0 if retenida else 14 - leidos)
    assert counts["write_errors"] == 0


# ------------------------------------------------------ --forzar-evento, hasta la guarda


def test_forzar_el_332_aplica_una_bajada_real_que_la_guarda_retiene(pase, caplog):
    """Si el forzado no llegara a `_write_event`, esta bajada se quedaria retenida."""
    base = _base_de_hoy()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        counts, conn = pase(
            base, _fichas_de_hoy(ufc_332=_ficha("ufc-332", 10)), forzar=["ufc-332"]
        )

    assert counts["cards_forced"] == 1
    assert counts["cards_guarded"] == 0
    assert "ufc-332" in base.eventos_escritos
    assert base.activos("ufc-332") == 10
    assert counts["bouts_cancelled"] == 4
    assert counts["events_written"] == 3
    assert conn.commits == 1 + 3
    assert "FORZADO" in caplog.text


def test_forzar_no_escribe_el_332_si_su_ficha_esta_caida(pase):
    base = _base_de_hoy()
    counts, _ = pase(
        base, _fichas_de_hoy(ufc_332=requests.HTTPError("503 Server Error")), forzar=["ufc-332"]
    )

    assert counts["events_skipped_detail"] == 1
    assert counts["cards_forced"] == 0
    assert "ufc-332" not in base.eventos_escritos
    assert base.activos("ufc-332") == 14


def test_forzar_un_slug_que_no_esta_en_el_listado_lo_avisa_y_no_fuerza_nada(pase, caplog):
    base = _base_de_hoy()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        counts, _ = pase(base, _fichas_de_hoy(ufc_332=_ficha("ufc-332", 0)), forzar=["ufc-999"])

    assert "--forzar-evento ufc-999" in caplog.text
    assert counts["cards_forced"] == 0
    assert counts["cards_guarded"] == 1
    assert base.activos("ufc-332") == 14
