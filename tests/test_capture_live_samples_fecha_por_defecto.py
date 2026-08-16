"""Sin `--dates`, la captura tiene que pedir la VENTANA US Eastern, no hoy en UTC.

EL FALLO QUE ESTO FIJA. La noche del UFC 330 (15-ago-2026) el centinela lanzó
las dos capturas con `dates=""` (live-sentinel.yml, paso `Lanzar el directo`).
La captura B nace con la A pero ARRANCA cuando la A muere, o sea pasada la
medianoche UTC: el run 31908919300 arrancó a las 00:41:35Z del día 16, la
función `main` hizo `datetime.now(timezone.utc).strftime("%Y%m%d")` y pidió
`dates=20260816`. El scoreboard de ESPN está INDEXADO POR FECHA LOCAL DE
EE.UU. y UFC 330 vivía bajo `20260815`, así que la respuesta vino vacía:
`{"skipped": "no events today", "dates": "20260816"}`, exit 0 y un artifact de
1846 bytes. Un run VERDE que no capturó ni un fichero del main card — y esta
captura cruda es "el único plan B real" del directo (live-sentinel.yml).

`default_dates_window()` (src/scrapers/espn_live_results.py) ya resolvía esto
para el bucle desde julio: devuelve ayer+hoy en US Eastern como rango
`YYYYMMDD-YYYYMMDD`, que es justo lo que cubre el salto de medianoche UTC.

POR QUÉ NO SALTÓ NINGÚN TEST. `test_capture_live_samples_falla_en_rojo.py`
siempre llama a `main()` con `--dates 20260808` explícito, así que la rama por
defecto no tenía NI UNA línea de cobertura. Este fichero cubre exactamente esa
rama: `main()` SIN `--dates`.

Y el efecto lateral de la ventana: como pide DOS días, la respuesta puede traer
DOS veladas (un DWCS del martes y el UFC del sábado). El último test fija que
`poll_once` las guarda las dos en ficheros disjuntos, que era la duda razonable
antes de cambiar la fecha por defecto.

Sin red: se falsea `poll_once` (o `_fetch_json`) y se anota qué se pidió.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime, timezone

import pytest

from scripts import capture_live_samples as captura
from src.scrapers import espn_live_results

# El instante real de arranque de la captura B del UFC 330 (run 31908919300).
# 00:41:35Z del 16-ago = 20:41 del 15-ago en US Eastern.
ARRANQUE_CAPTURA_B = datetime(2026, 8, 16, 0, 41, 35, tzinfo=timezone.utc)
VENTANA_ESPERADA = "20260814-20260815"  # ayer+hoy US Eastern: el card está dentro
FECHA_UTC_DEL_FALLO = "20260816"  # lo que se pidió de verdad, y vino vacío


class RelojCongelado(datetime):
    """`datetime` con `now()` clavado en el arranque de la captura B."""

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return ARRANQUE_CAPTURA_B.astimezone(tz) if tz else ARRANQUE_CAPTURA_B


class PollEspia:
    """Sustituye a `poll_once` y se queda con el `dates` que le pasaron.

    Devuelve 0 ("no hay cartelera en la ventana") a propósito: a `main` le basta
    para salir en verde tras UNA pasada, que es todo lo que hace falta para leer
    el argumento. Qué pasa con el -1 y con la racha ya lo cubre
    `test_capture_live_samples_falla_en_rojo.py`.
    """

    def __init__(self) -> None:
        self.dates: list[str] = []

    def __call__(self, session, out_dir, dates, counts) -> int:  # noqa: ANN001
        self.dates.append(dates)
        return 0


@pytest.fixture
def ejecutar(monkeypatch, tmp_path):
    """`main()` sin red y sin bucle (`--once`). Devuelve el `PollEspia` usado."""
    monkeypatch.setattr(captura, "build_espn_session", lambda: None)
    monkeypatch.setattr(captura.time, "sleep", lambda _segundos: None)

    def correr(*argv_extra: str) -> PollEspia:
        poll = PollEspia()
        monkeypatch.setattr(captura, "poll_once", poll)
        monkeypatch.setattr(sys, "argv", [
            "capture_live_samples", "--out", str(tmp_path), "--once", *argv_extra,
        ])
        captura.main()
        return poll

    return correr


def test_sin_dates_pide_la_ventana_us_eastern_y_no_la_fecha_utc(ejecutar, monkeypatch):
    """El caso literal del UFC 330: a las 00:41Z hay que pedir el día ANTERIOR.

    Se congela el reloj de `espn_live_results` (donde vive
    `default_dates_window`) y NO el de `capture_live_samples`: así el test no
    depende de cómo se escriba la llamada, y si alguien devolviera la línea a
    `datetime.now(timezone.utc)` del propio script, ese reloj seguiría corriendo
    de verdad y el argumento llegaría con la fecha UTC de hoy -> rojo.
    """
    monkeypatch.setattr(espn_live_results, "datetime", RelojCongelado)

    poll = ejecutar()

    assert poll.dates == [VENTANA_ESPERADA], (
        "arrancando a las 00:41Z había que pedir la ventana US Eastern "
        f"{VENTANA_ESPERADA} (donde vive el card), y se pidió {poll.dates}"
    )
    assert FECHA_UTC_DEL_FALLO not in poll.dates[0], (
        "esto es exactamente lo que pidió el run 31908919300, y vino vacío"
    )


def test_sin_dates_el_argumento_es_un_RANGO_de_dos_dias(ejecutar):
    """Guardarraíl de formato, sin congelar nada: nunca una fecha suelta.

    El scoreboard acepta `YYYYMMDD` y `YYYYMMDD-YYYYMMDD` (docstring de
    espn_live_results). Una fecha suelta vuelve a dejar fuera el card cuando la
    captura arranca pasada la medianoche UTC, que es la avería entera.
    """
    poll = ejecutar()

    assert re.fullmatch(r"\d{8}-\d{8}", poll.dates[0]), (
        f"se esperaba un rango de dos días y llegó {poll.dates[0]!r}"
    )


def test_con_dates_explicito_manda_el_argumento(ejecutar):
    """El `-f dates=` del workflow sigue mandando: recapturar a mano no cambia."""
    poll = ejecutar("--dates", "20260808")

    assert poll.dates == ["20260808"]


def test_poll_once_guarda_los_dos_eventos_si_la_ventana_pilla_dos(monkeypatch, tmp_path):
    """El efecto lateral de pedir dos días: dos veladas en la misma respuesta.

    Medido contra ESPN el 16-ago-2026: `dates=20260810-20260817` devuelve el
    DWCS 600060732 y el UFC 330 600059185, con ids distintos y sin repetirse. La
    ventana por defecto solo junta dos eventos si hay veladas en días
    contiguos, pero cuando pasa NO puede perderse ninguna ni pisarse los
    ficheros: el id va en la etiqueta y la etiqueta va en el nombre del fichero.
    Aquí se ejecuta el `poll_once` de verdad (solo se falsea la red).
    """
    dwcs, ufc_330 = "600060732", "600059185"
    scoreboard = {
        "events": [
            {"id": dwcs, "competitions": [
                {"id": "1", "status": {"type": {"state": "post"}}},
            ]},
            {"id": ufc_330, "competitions": [
                {"id": "2", "status": {"type": {"state": "post"}}},
            ]},
        ]
    }
    pedidas: list[str] = []

    def fetch(session, url, params=None):  # noqa: ANN001
        pedidas.append(url)
        return scoreboard if url == captura.SCOREBOARD_URL else {"url": url}

    monkeypatch.setattr(captura, "_fetch_json", fetch)
    counts: Counter = Counter()

    devueltos = captura.poll_once(None, tmp_path, VENTANA_ESPERADA, counts)

    assert devueltos == 2, "los dos eventos de la ventana cuentan como cartelera"
    ficheros = sorted(p.name for p in tmp_path.iterdir())
    assert len(ficheros) == 3, (
        f"se esperaban 1 scoreboard + 2 fightcenter y hay {ficheros}"
    )
    assert counts["saved"] == 3
    for event_id in (dwcs, ufc_330):
        assert sum(f"fightcenter-{event_id}" in nombre for nombre in ficheros) == 1, (
            f"el fightcenter de {event_id} tenía que caer en su propio fichero: {ficheros}"
        )
    assert len(pedidas) == 3, "un GET al scoreboard y uno al fightcenter de cada evento"
