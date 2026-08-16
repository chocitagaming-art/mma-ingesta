"""Los guards anti-duplicado de los workflows del directo, leidos del YAML.

POR QUE ESTE FICHERO EXISTE. Nada en toda la suite lee un `.github/workflows/`:
`test_live_watchdog.py` y `test_live_sentinel.py` prueban las funciones puras de
Python, y la decision de disparar o no disparar vive entera en el YAML. Antes de
esto la unica red era `actionlint`, que valida sintaxis, tipos de expresion y
referencias a `steps.<id>` — pero no sabe nada de lo que significa un jq ni de
si dos dispatches viven en el mismo paso. Y el `if:` de un job no se puede
ensayar de verdad sin fusionar a main, asi que el fallo se descubre en directo.

LO QUE SE ROMPIO Y ESTO IMPIDE QUE VUELVA (noche del UFC 330, 15-ago-2026):

1. EL JQ CIEGO. Los dos guards contaban `status == "in_progress" or "queued"`.
   El relevo que la concurrency de GitHub deja esperando NO se reporta como
   'queued' sino como 'pending', asi que era invisible. El watchdog 31909928211
   imprimio a las 21:37:19Z "runs del bucle vivos: 1" habiendo DOS runs sin
   terminar: A=31908915442 en curso y B=31908916644, que estuvo 3h36 en
   'pending' (creado 21:15:06Z, arranco 00:51:28Z). Contar 1 en vez de 0 fue lo
   unico que evito que el watchdog disparase un rescate y EXPULSARA al relevo —
   que es exactamente como se perdio la velada del 11-jul-2026. De los seis
   status posibles solo 'completed' es terminal, asi que el guard correcto
   pregunta por lo terminal: `select(.status != "completed")`.

2. EL RESCATE A CIEGAS DE LA CAPTURA. `live-watchdog.yml` comprobaba los bucles
   vivos y luego disparaba, en el MISMO paso, el bucle Y la captura cruda. La
   captura nunca se miraba. `capture-live-samples.yml` tiene su propio grupo de
   concurrency, asi que ese disparo extra expulsaba a la captura B — la noche
   del UFC 330, A=31908917812 y B=31908919300 (3h26 en 'pending'). La captura
   cruda es el unico plan B: si se pierde, la serie de la velada no se
   reconstruye desde ningun sitio. Por eso ahora son dos pasos con dos guards.

Estos tests leen el YAML, no lo ejecutan. No tocan la red ni la base de datos.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

WATCHDOG = WORKFLOWS / "live-watchdog.yml"
SENTINEL = WORKFLOWS / "live-sentinel.yml"

# Los dos ficheros que deciden si se dispara un run del directo.
CON_GUARD = [WATCHDOG, SENTINEL]

# El recuento correcto, byte a byte. Se compara literal a proposito: si alguien
# lo reescribe "equivalente" tiene que pasar por aqui y leer el porque.
JQ_BUENO = 'select(.status != "completed")'

# El recuento ciego que costo la noche del 15-ago. Ni rastro de el.
JQ_CIEGO = 'select(.status == "in_progress"'


def _pasos(fichero: Path) -> list[dict]:
    """Los pasos del unico job del workflow, ya parseados."""
    datos = yaml.safe_load(fichero.read_text(encoding="utf-8"))
    jobs = datos["jobs"]
    assert len(jobs) == 1, f"{fichero.name} ya no tiene un unico job"
    (job,) = jobs.values()
    return job["steps"]


def _paso_con_id(fichero: Path, step_id: str) -> dict:
    for paso in _pasos(fichero):
        if paso.get("id") == step_id:
            return paso
    raise AssertionError(f"{fichero.name} no tiene ningun paso con id '{step_id}'")


def _pasos_que_disparan(fichero: Path, workflow: str) -> list[dict]:
    """Pasos cuyo `run` hace `gh workflow run <workflow>`."""
    return [
        paso
        for paso in _pasos(fichero)
        if f"gh workflow run {workflow}" in paso.get("run", "")
    ]


def _unico_paso_que_dispara(fichero: Path, workflow: str) -> dict:
    """El unico paso que relanza ese workflow, y falla claro si hay mas de uno.

    Que haya dos es el sintoma exacto de que alguien ha vuelto a fusionar los
    dispatches: el paso del bucle acaba disparando tambien la captura.
    """
    pasos = _pasos_que_disparan(fichero, workflow)
    nombres = [p.get("name", "(sin nombre)") for p in pasos]
    assert len(pasos) == 1, (
        f"{fichero.name} deberia relanzar {workflow} desde UN unico paso, "
        f"y lo hace desde {len(pasos)}: {nombres}"
    )
    return pasos[0]


# ------------------------------------------------- 1. el jq que ve el 'pending'


@pytest.mark.parametrize("fichero", CON_GUARD, ids=lambda f: f.name)
def test_ningun_guard_usa_el_recuento_ciego(fichero: Path):
    """EL FALLO DEL 15-AGO. `in_progress or queued` no ve el relevo en cola,
    que GitHub reporta como 'pending'. El bucle B del UFC 330 estuvo 3h36
    invisible para este jq."""
    texto = fichero.read_text(encoding="utf-8")
    codigo = "\n".join(
        linea for linea in texto.splitlines() if not linea.lstrip().startswith("#")
    )
    assert JQ_CIEGO not in codigo, (
        f"{fichero.name} ha vuelto al recuento ciego. Un run 'pending' no se "
        "cuenta como vivo y el siguiente dispatch lo EXPULSA (11-jul-2026)."
    )


@pytest.mark.parametrize("fichero", CON_GUARD, ids=lambda f: f.name)
def test_todo_guard_cuenta_lo_no_terminal(fichero: Path):
    """'completed' es el UNICO status terminal de los seis que devuelve la API.
    Preguntar por el es lo unico que no deja fuera un estado nuevo."""
    assert JQ_BUENO in fichero.read_text(encoding="utf-8"), (
        f"{fichero.name} deberia contar los runs vivos con {JQ_BUENO!r}."
    )


@pytest.mark.parametrize("fichero", CON_GUARD, ids=lambda f: f.name)
def test_ningun_guard_filtra_por_antiguedad(fichero: Path):
    """SIN `--created`, y es deliberado. La edad de un run vivo es cola mas
    ejecucion: el bucle B nacio a las 21:15:06Z y murio a las 04:47:58Z, 7h32
    de edad estando vivo. Un filtro de 6 h lo habria hecho invisible desde las
    03:15Z — la misma ceguera que este guard viene a quitar, por la puerta de
    atras. Quien mata a un run atascado es el `timeout-minutes: 300` del bucle,
    no un filtro aqui."""
    codigo = "\n".join(
        linea
        for linea in fichero.read_text(encoding="utf-8").splitlines()
        if not linea.lstrip().startswith("#")
    )
    assert "--created" not in codigo, (
        f"{fichero.name} ha ganado un filtro de antiguedad en el guard. Lee el "
        "comentario del paso: vuelve a cegar el recuento sin proteger de nada."
    )


# ------------------------------- 2. bucle y captura, dos pasos y dos recuentos


def test_el_watchdog_cuenta_las_capturas_vivas_antes_de_disparar():
    """Hasta el 16-ago-2026 el rescate disparaba la captura sin haberla mirado
    nunca, y expulsaba a la captura B en cola."""
    guard_cap = _paso_con_id(WATCHDOG, "guard_cap")
    assert "capture-live-samples.yml" in guard_cap["run"]
    assert JQ_BUENO in guard_cap["run"]


def test_bucle_y_captura_se_disparan_en_pasos_distintos():
    """EL NUCLEO DEL FALLO 4. Los dos dispatches vivian en un solo paso con un
    solo `if`, asi que la captura se lanzaba con el permiso del recuento del
    bucle. Cada uno necesita su propio guard, y un `if:` de paso no puede
    decidir por comando: tienen que ser dos pasos."""
    bucle = _unico_paso_que_dispara(WATCHDOG, "live-event-loop.yml")
    captura = _unico_paso_que_dispara(WATCHDOG, "capture-live-samples.yml")

    assert bucle["name"] != captura["name"], (
        "bucle y captura han vuelto al mismo paso: la captura se estaria "
        "disparando con el recuento del bucle y expulsaria al relevo en cola."
    )


def test_cada_dispatch_cuelga_de_su_propio_recuento():
    """El `if` de cada paso tiene que nombrar SU guard. Es lo que impide que
    dentro de seis meses alguien vuelva a mezclarlos."""
    bucle = _unico_paso_que_dispara(WATCHDOG, "live-event-loop.yml")
    captura = _unico_paso_que_dispara(WATCHDOG, "capture-live-samples.yml")

    assert "steps.guard.outputs.vivos == '0'" in bucle["if"]
    assert "steps.guard_cap.outputs.vivas" not in bucle["if"]

    assert "steps.guard_cap.outputs.vivas == '0'" in captura["if"]
    assert "steps.guard.outputs.vivos" not in captura["if"]


def test_los_dos_rescates_solo_disparan_si_el_recuento_dio_cero():
    """A prueba de fallos por construccion: si un guard se salta o revienta, su
    output es cadena vacia, que NO es '0', asi que no se dispara nada. La
    asimetria manda: no rescatar cuesta un email, rescatar de mas cuesta la
    velada entera y no se recupera."""
    for paso in (
        _unico_paso_que_dispara(WATCHDOG, "live-event-loop.yml"),
        _unico_paso_que_dispara(WATCHDOG, "capture-live-samples.yml"),
    ):
        assert "== '0'" in paso["if"], paso["name"]
        assert "inputs.dry_run != true" in paso["if"], paso["name"]


def test_el_rescate_del_plan_b_sobrevive_a_un_fallo_del_bucle():
    """`!cancelled()` en vez del `success()` implicito de Actions. Si el
    dispatch del bucle falla, la captura cruda es MAS necesaria, no menos: es
    el unico plan B. Y en el paso del bucle, por simetria, para que un fallo
    del guard nuevo de la captura no se lleve por delante el rescate que ya
    funcionaba antes del 16-ago."""
    for paso in (
        _unico_paso_que_dispara(WATCHDOG, "live-event-loop.yml"),
        _unico_paso_que_dispara(WATCHDOG, "capture-live-samples.yml"),
    ):
        assert "!cancelled()" in paso["if"], paso["name"]


# ------------------------------------------------- 3. que el ensayo pruebe algo


def test_los_dos_recuentos_se_ejecutan_tambien_en_ensayo():
    """Un `dry_run` fuera de velada devolvia SIN_VELADA y se saltaba TODO: el
    `gh run list` no llegaba a ejecutarse ni una vez y el run salia verde en
    40 s sin haber probado nada. Un error de comillas en el jq habria pasado el
    ensayo tan campante."""
    for step_id in ("guard", "guard_cap"):
        condicion = _paso_con_id(WATCHDOG, step_id)["if"]
        assert "inputs.dry_run == true" in condicion, (
            f"el paso '{step_id}' vuelve a saltarse en ensayo: el ensayo no "
            "prueba el recuento."
        )


def test_el_estado_fingido_solo_se_obedece_en_ensayo():
    """`simular_estado` es una palanca que altera el diagnostico en el camino
    critico de la noche de velada. La guarda es que solo vale con
    dry_run=true: un despiste al rellenar el formulario no puede convertir un
    ensayo en un rescate de verdad."""
    wd = _paso_con_id(WATCHDOG, "wd")
    assert '[ -n "$SIMULAR" ] && [ "$DRY_RUN" = "true" ]' in wd["run"]


def test_un_caido_fingido_no_manda_email():
    """Un simulacro no puede ser indistinguible de la alarma real. Al reves si:
    un CAIDO de verdad sigue fallando en rojo aunque el run se haya lanzado
    como ensayo."""
    ultimo = _pasos(WATCHDOG)[-1]
    assert "exit 1" in ultimo["run"], "el paso que falla en rojo ya no es el ultimo"
    assert "steps.wd.outputs.estado == 'CAIDO'" in ultimo["if"]
    assert "steps.wd.outputs.simulado != 'true'" in ultimo["if"]
