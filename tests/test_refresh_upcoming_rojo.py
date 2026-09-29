"""refresh-upcoming se pone en ROJO cuando algo sale mal, aunque no reviente.

EL FALLO QUE FIJAN ESTOS TESTS, medido el 29-sep-2026: `main` solo salia con 1
si un paso lanzaba una excepcion. `write_errors`, `detail_errors`,
`listing_pages_failed` (upcoming_events) y `event_errors` (backfill_results) se
tragan su excepcion y cuentan, y el run acababa en VERDE con ellos a 1. Y como
el resumen era un `dict(Counter)`, los ceros no salian nunca en el log:
`write_errors: 0` no se podia comprobar porque no se imprimia.

Ahora: las claves de alarma salen siempre (a 0) en el resumen y en el log, y
`main` sale con 1 DESPUES de correr todos los pasos si hay un paso fallido o una
alarma. El rojo abre el Issue de notify-on-failure y le llega un email al dueno.

NO son alarma, a proposito: `events_unmatched` sale 1 TODOS los dias por el Road
To UFC (1094), que no esta en ufcstats, y el cron se quedaria en rojo para
siempre. Tampoco `bouts_unmatched`, `stats_unmatched` ni los de link/enrich.

🪤 NUNCA se llama a `refresh()` de verdad: con el DATABASE_URL que exporta el
megatest escribiria en PRODUCCION. Se parchea `refresh_upcoming.refresh` entero
(los pasos son from-imports: parchear `upcoming_events.scrape_upcoming_events`
no serviria), o `_run_step` para que no ejecute ningun paso.
"""

import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from src.scrapers import refresh_upcoming as ru

LOGGER_NAME = "src.scrapers.refresh_upcoming"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "refresh-upcoming.yml"

PASOS = (
    "upcoming_events",
    "link_upcoming",
    "enrich_upcoming",
    "enrich_records_espn",
    "backfill_results",
)
ALARMAS_UPCOMING = (
    "write_errors",
    "detail_errors",
    "listing_pages_failed",
    "cards_guarded",
    "events_skipped_detail",
)
PASO_FALLIDO = {"status": "failed", "elapsed_s": 0.1, "error": "RuntimeError: x"}


def _resumen(**cuentas_por_paso: dict) -> dict:
    """Un resumen de `refresh()` con los cinco pasos en 'ok' y un dia normal."""
    resumen = {}
    for paso in PASOS:
        counts = {"events_found": 10} if paso == "upcoming_events" else {}
        counts.update(cuentas_por_paso.get(paso, {}))
        resumen[paso] = {"status": "ok", "elapsed_s": 0.1, "counts": counts}
    return resumen


# ------------------------------------------------------ que es alarma y que no


def test_un_dia_normal_no_da_alarma():
    assert ru._alarms(_resumen()) == []


@pytest.mark.parametrize("clave", ALARMAS_UPCOMING)
def test_cada_contador_de_error_de_upcoming_pone_el_rojo(clave):
    assert ru._alarms(_resumen(upcoming_events={clave: 1})) == [f"upcoming_events.{clave}=1"]


def test_event_errors_de_backfill_pone_el_rojo():
    assert ru._alarms(_resumen(backfill_results={"event_errors": 2})) == [
        "backfill_results.event_errors=2"
    ]


def test_un_listado_vacio_pone_el_rojo():
    """Anti-bot o pagina 0 vacia: hoy daba `events_found: 0` en verde."""
    assert ru._alarms(_resumen(upcoming_events={"events_found": 0})) == [
        "upcoming_events.events_found=0"
    ]


def test_lo_que_no_es_alarma_no_pone_el_rojo():
    resumen = _resumen(
        # El Road To UFC (1094) no esta en ufcstats: sale 1 todos los dias.
        backfill_results={"events_unmatched": 1, "bouts_unmatched": 3, "stats_unmatched": 5},
        link_upcoming={"unresolved": 4, "marcador_rechazado": 1},
        enrich_upcoming={"errors": 3, "unresolved": 2},
        enrich_records_espn={"unresolved": 7},
        # Forzar un evento a mano es intencionado, no un fallo.
        upcoming_events={"cards_forced": 1, "listing_cap_hit": 1},
    )
    assert ru._alarms(resumen) == []


def test_un_paso_fallido_no_revienta_el_calculo_de_alarmas():
    """El paso fallido ya pone el rojo por su cuenta; aqui no tiene `counts`."""
    resumen = _resumen()
    resumen["upcoming_events"] = PASO_FALLIDO
    assert ru._alarms(resumen) == []


def test_varias_alarmas_se_dicen_todas():
    resumen = _resumen(
        upcoming_events={"cards_guarded": 1, "write_errors": 2},
        backfill_results={"event_errors": 1},
    )
    assert sorted(ru._alarms(resumen)) == [
        "backfill_results.event_errors=1",
        "upcoming_events.cards_guarded=1",
        "upcoming_events.write_errors=2",
    ]


# ------------------------------------------------------ los ceros, a la vista


def test_run_step_saca_las_alarmas_de_upcoming_a_cero(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        info = ru._run_step("upcoming_events", lambda: Counter(events_found=10))

    assert info["status"] == "ok"
    for clave in ALARMAS_UPCOMING:
        assert info["counts"][clave] == 0, clave
    assert '"write_errors": 0' in caplog.text
    assert '"cards_guarded": 0' in caplog.text


def test_run_step_saca_event_errors_aunque_backfill_no_tenga_candidatos():
    """backfill devuelve `{'events_candidate': 0}` a secas si no hay eventos."""
    info = ru._run_step("backfill_results", lambda: {"events_candidate": 0})
    assert info["counts"] == {"events_candidate": 0, "event_errors": 0}


def test_run_step_no_pisa_un_contador_que_ya_viene():
    info = ru._run_step("upcoming_events", lambda: Counter(events_found=10, write_errors=2))
    assert info["counts"]["write_errors"] == 2


def test_run_step_no_anade_claves_a_los_pasos_sin_alarma():
    info = ru._run_step("link_upcoming", lambda: Counter(linked=2))
    assert info["counts"] == {"linked": 2}


# ------------------------------------------------------ main: el codigo de salida


@pytest.fixture
def lanzar(monkeypatch):
    """Ejecuta `main` con `refresh` parcheado ENTERO. Nunca toca la base ni la red."""
    monkeypatch.setattr(ru, "configure_logging", lambda: None)

    def _lanzar(resumen: dict, *argumentos: str) -> dict:
        llamada: dict = {}

        def refresh_falso(**kwargs):
            llamada.update(kwargs)
            return resumen

        monkeypatch.setattr(ru, "refresh", refresh_falso)
        monkeypatch.setattr(sys, "argv", ["refresh_upcoming", *argumentos])
        ru.main()
        return llamada

    return _lanzar


def test_main_sale_con_1_si_hay_una_alarma(lanzar, caplog, capsys):
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        with pytest.raises(SystemExit) as salida:
            lanzar(_resumen(upcoming_events={"cards_guarded": 1}))

    assert salida.value.code == 1
    assert "upcoming_events.cards_guarded=1" in caplog.text
    # El resumen se imprime ANTES de salir: el log del cron lo trae entero.
    assert json.loads(capsys.readouterr().out)["upcoming_events"]["counts"]["cards_guarded"] == 1


def test_main_sale_con_1_si_un_paso_falla(lanzar, caplog):
    resumen = _resumen()
    resumen["backfill_results"] = PASO_FALLIDO
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        with pytest.raises(SystemExit) as salida:
            lanzar(resumen)

    assert salida.value.code == 1
    assert "backfill_results" in caplog.text


def test_main_termina_en_verde_en_un_dia_normal(lanzar):
    lanzar(_resumen())  # sin SystemExit


# ------------------------------------------------------ --forzar-evento SLUG=N


def test_main_pasa_los_eventos_forzados_a_refresh(lanzar):
    llamada = lanzar(_resumen(), "--forzar-evento", "ufc-332=10", "--forzar-evento", "ufc-333=9")
    assert llamada["forzar_eventos"] == {"ufc-332": 10, "ufc-333": 9}


def test_main_sin_forzar_no_fuerza_nada(lanzar):
    llamada = lanzar(_resumen())
    assert llamada["forzar_eventos"] == {}


def test_forzar_tolera_los_espacios_del_formulario_de_github(lanzar):
    llamada = lanzar(_resumen(), "--forzar-evento", " ufc-332 = 10 ")
    assert llamada["forzar_eventos"] == {"ufc-332": 10}


@pytest.mark.parametrize(
    "valor",
    ["ufc-332", "ufc-332=", "ufc-332=0", "ufc-332=-3", "ufc-332=diez", "=10", " "],
)
def test_forzar_sin_un_numero_de_combates_valido_no_arranca(lanzar, capsys, valor):
    """Sin N no hay con que comprobar la ficha del pase forzado: no se corre nada.

    N=0 tampoco vale: una ficha sin combates es justo el fallo que para la guarda.
    """
    with pytest.raises(SystemExit) as salida:
        lanzar(_resumen(), "--forzar-evento", valor)

    assert salida.value.code == 2
    consola = capsys.readouterr()
    assert "expected SLUG=N" in consola.err
    assert consola.out == ""  # ni un paso: el resumen no llega a imprimirse


def test_forzar_el_mismo_slug_dos_veces_no_arranca(lanzar, capsys):
    with pytest.raises(SystemExit) as salida:
        lanzar(_resumen(), "--forzar-evento", "ufc-332=10", "--forzar-evento", "ufc-332=9")

    assert salida.value.code == 2
    consola = capsys.readouterr()
    assert "each slug only once" in consola.err
    assert consola.out == ""


def test_refresh_lleva_los_forzados_hasta_el_paso_de_ufc_com(monkeypatch):
    """De `refresh` a `scrape_upcoming_events`, sin ejecutar NINGUN paso de verdad.

    `_run_step` falso solo guarda cada paso; luego se llama a mano al de
    upcoming_events, con `scrape_upcoming_events` parcheado donde `refresh` lo
    busca. Si manana se anade un sexto paso, este test sigue sin ejecutarlo.
    """
    pasos: dict = {}

    def run_step_falso(nombre, paso):
        pasos[nombre] = paso
        return {"status": "ok", "elapsed_s": 0.0, "counts": {}}

    llamada: dict = {}

    def scrape_falso(**kwargs):
        llamada.update(kwargs)
        return Counter()

    monkeypatch.setattr(ru, "_run_step", run_step_falso)
    monkeypatch.setattr(ru, "scrape_upcoming_events", scrape_falso)

    ru.refresh(forzar_eventos={"ufc-332": 10})
    assert set(pasos) == set(PASOS)
    pasos["upcoming_events"]()

    assert llamada["dry_run"] is False
    assert llamada["forzar_eventos"] == {"ufc-332": 10}


# ------------------------------------------------------ el workflow


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_el_workflow_ofrece_forzar_evento_vacio_por_defecto():
    datos = _workflow()
    disparadores = datos.get("on", datos.get(True))
    entrada = disparadores["workflow_dispatch"]["inputs"]["forzar_evento"]

    assert entrada.get("required", False) is False
    assert entrada["default"] == ""
    # El cron diario sigue ahi: la entrada es solo para el boton manual.
    assert "schedule" in disparadores
    # El ejemplo que ve el dueno en el formulario tiene el formato de la CLI.
    (ejemplo,) = re.findall(r"[a-z0-9-]+=\d+", entrada["description"])
    assert ru._parse_forced_event(ejemplo) == ("ufc-332", 10)


def test_el_workflow_solo_pasa_forzar_evento_si_no_esta_vacio():
    (job,) = _workflow()["jobs"].values()
    (paso,) = [p for p in job["steps"] if "src.scrapers.refresh_upcoming" in p.get("run", "")]

    # La entrada llega por el entorno, nunca pegada en el script (inyeccion).
    assert paso["env"]["FORZAR_EVENTO"] == "${{ inputs.forzar_evento }}"
    assert "inputs.forzar_evento" not in paso["run"]
    assert '[ -n "$FORZAR_EVENTO" ]' in paso["run"]
    # La rama que fuerza es la de la entrada NO vacia, y la otra no fuerza: con
    # las ramas al reves, el cron diario pasaria --forzar-evento "" y el
    # dispatch forzado correria sin forzar nada.
    con_entrada, sin_entrada = paso["run"].split("else", 1)
    assert '--forzar-evento "$FORZAR_EVENTO"' in con_entrada
    assert "--forzar-evento" not in sin_entrada
    # Y el run sin forzar sigue siendo el de siempre.
    assert "python -m src.scrapers.refresh_upcoming" in sin_entrada
    assert paso["env"]["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
