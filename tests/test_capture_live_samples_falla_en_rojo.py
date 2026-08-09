"""La captura de respaldo tiene que FALLAR cuando no captura nada.

EL FALLO QUE ESTO FIJA. El 8-ago-2026, noche de velada, `capture-live-samples`
lanzó sus dos jobs y los dos se pasaron el rato pidiendo un endpoint que
respondía 403. Terminaron **en verde**, con el artifact vacío, y nadie se
enteró hasta el día siguiente.

El hueco era de dos líneas: `poll_once` devuelve **-1** cuando el scoreboard no
contesta, pero `main` solo cortaba con `== 0` (el guard de "hoy no hay
cartelera"). El -1 se colaba por ese hueco, el bucle seguía sus 235 minutos
capturando el mismo error, y el proceso salía con `exit 0`. Sumado al
`if: always()` + `if-no-files-found: warn` del workflow, el resultado era un
run verde que no probaba absolutamente nada.

    "lo que hace falta no es solo una alarma fuera, es que los jobs fallen
     cuando no producen nada"  — MMA_CONTINUACION_2026-08-08, §6.4

Las dos reglas que fija este fichero:

  1. Si la PRIMERA pasada no consigue el scoreboard, se aborta ya y en rojo, sin
     entrar en el bucle. No es mala suerte: es la fuente cerrada.
  2. Una pasada mala suelta NO mata la ventana (misma filosofía que
     `run_bounded_loop`: la velada sigue), pero una RACHA sí. Con el intervalo
     por defecto, 5 seguidas son 10 minutos sin que ESPN conteste.

Sin red: se falsea `poll_once` y se comprueba qué hace `main` con lo que
devuelve. Quien sale de verdad a la red es el smoke, y el consumidor real de
todo esto es lanzar el workflow desde Actions y contar los ficheros del
artifact — que es exactamente lo que NO se hizo el 8-ago.
"""

from __future__ import annotations

import sys
from collections import Counter

import pytest

from scripts import capture_live_samples as captura


class PollFalso:
    """Sustituye a `poll_once` y lleva la cuenta de cuántas veces lo llamaron.

    Devuelve los valores de `resultados` en orden; cuando se agotan repite el
    último, así un test puede decir "y a partir de aquí, todo mal" sin escribir
    cincuenta menos-unos.
    """

    def __init__(self, resultados: list[int]) -> None:
        self.resultados = resultados
        self.llamadas = 0

    def __call__(self, session, out_dir, dates, counts) -> int:  # noqa: ANN001
        indice = self.llamadas
        self.llamadas += 1
        valor = self.resultados[indice] if indice < len(self.resultados) else self.resultados[-1]
        if valor < 0:
            counts["scoreboard_error"] += 1
        else:
            counts["saved"] += 1
        return valor


@pytest.fixture
def ejecutar(monkeypatch, tmp_path):
    """`main()` sin red, sin esperas y con un reloj que avanza a saltos.

    El reloj falso da 5 vueltas de bucle con `--duration-minutes 1`: la primera
    llamada fija el `deadline` en 60 y las siguientes devuelven 10, 20, 30, 40,
    50 y 60. Suficiente para probar una racha de 5 sin esperar 5 minutos.

    Devuelve el `PollFalso` usado, para poder mirar cuántas pasadas hubo. Como
    `main` puede levantar `SystemExit`, el objeto se guarda antes de llamar.
    """
    monkeypatch.setattr(captura, "build_espn_session", lambda: None)
    monkeypatch.setattr(captura.time, "sleep", lambda _segundos: None)

    reloj = {"t": -10}

    def monotonic() -> float:
        reloj["t"] += 10
        return float(reloj["t"])

    monkeypatch.setattr(captura.time, "monotonic", monotonic)
    monkeypatch.setattr(sys, "argv", [
        "capture_live_samples",
        "--out", str(tmp_path),
        "--duration-minutes", "1",
        "--interval-seconds", "1",
        "--dates", "20260808",
    ])

    ultimo: dict[str, PollFalso] = {}

    def correr(resultados: list[int]) -> PollFalso:
        poll = PollFalso(resultados)
        ultimo["poll"] = poll
        monkeypatch.setattr(captura, "poll_once", poll)
        captura.main()
        return poll

    correr.ultimo = ultimo  # type: ignore[attr-defined]
    return correr


def test_primera_pasada_sin_scoreboard_sale_en_rojo(ejecutar):
    """El 403 del 8-ago: si la fuente está cerrada, el run no puede salir verde."""
    with pytest.raises(SystemExit) as salida:
        ejecutar([-1])

    assert salida.value.code == captura.EXIT_NOTHING_CAPTURED


def test_primera_pasada_mala_no_entra_en_el_bucle(ejecutar):
    """Y aborta YA: nada de ocupar el runner 235 minutos para descubrirlo."""
    with pytest.raises(SystemExit):
        ejecutar([-1])

    poll = ejecutar.ultimo["poll"]
    assert poll.llamadas == 1, (
        f"debía abortar tras la pasada de arranque y hubo {poll.llamadas}"
    )


def test_sin_cartelera_sale_en_verde(ejecutar):
    """Sábado sin evento: el guard barato de siempre, y sigue siendo verde."""
    poll = ejecutar([0])

    assert poll.llamadas == 1


def test_una_pasada_mala_suelta_no_mata_la_ventana(ejecutar):
    """Misma filosofía que `run_bounded_loop`: una pasada mala no mata la velada."""
    poll = ejecutar([3, 3, -1, 3, 3, 3])

    assert poll.llamadas > 1, "el bucle tenía que haber seguido tras el fallo suelto"


def test_racha_de_cinco_errores_aborta_en_rojo(ejecutar):
    """ESPN se cierra a mitad de velada: no se sigue hasta el final en verde."""
    with pytest.raises(SystemExit) as salida:
        ejecutar([3, -1])  # la de arranque va bien, y a partir de ahí todo mal

    assert salida.value.code == captura.EXIT_NOTHING_CAPTURED


def test_el_umbral_de_la_racha_esta_declarado():
    """Si alguien lo sube a 100, que sea a la vista y no por accidente."""
    assert captura.MAX_CONSECUTIVE_SCOREBOARD_ERRORS == 5


def test_poll_once_sigue_devolviendo_menos_uno_cuando_falla(monkeypatch, tmp_path):
    """El contrato del que depende todo lo de arriba.

    Si alguien cambia `poll_once` para que devuelva 0 en vez de -1 cuando el
    scoreboard falla, el guard de "hoy no hay cartelera" se lo tragaría y
    volveríamos al verde vacío del 8-ago. Sin este test, ese cambio pasa
    desapercibido.
    """
    monkeypatch.setattr(captura, "_fetch_json", lambda *_a, **_k: None)
    counts: Counter = Counter()

    assert captura.poll_once(None, tmp_path, "20260808", counts) == -1
    assert counts["scoreboard_error"] == 1
