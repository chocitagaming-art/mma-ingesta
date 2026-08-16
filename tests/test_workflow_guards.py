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

3. LA ALARMA AMORDAZADA (anadido el 16-ago-2026, seccion 4 de este fichero).
   Las autocancelaciones del centinela abrian un Issue de fallo cada hora de
   velada y ese Issue tapaba el canal entero: el #32 estuvo abierto de las
   19:18:17Z a las 21:15:23Z del 15-ago, es decir las dos horas justas antes
   del UFC 330. `notify-on-failure.yml` ya no avisa de esos 'cancelled', y
   distingue el caso comparando `github.event.workflow_run.path` con la ruta
   del fichero del centinela. Esa comparacion es una cadena literal escrita a
   mano en un YAML: si alguien renombra el fichero, o cambia el `name:` con el
   que este workflow esta vigilado, el arreglo deja de casar SIN QUE NADA
   FALLE. La seccion 4 es lo unico que ata ese cabo.

Estos tests leen el YAML, no lo ejecutan. No tocan la red ni la base de datos.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOWS = RAIZ / ".github" / "workflows"

WATCHDOG = WORKFLOWS / "live-watchdog.yml"
SENTINEL = WORKFLOWS / "live-sentinel.yml"
NOTIFY = WORKFLOWS / "notify-on-failure.yml"
CAPTURA = WORKFLOWS / "capture-live-samples.yml"

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


# ------------------ 4. la excepcion del centinela dentro de notify-on-failure
#
# POR QUE HAY QUE VIGILAR ESTO Y NO OTRA COSA. El `if:` de un job disparado por
# `workflow_run` SOLO se evalua desde la rama por defecto, asi que no existe
# forma de ensayarlo: se estrena en produccion, la noche de la velada, que es
# justo cuando importa. Y encima la excepcion del centinela cuelga de dos
# cadenas literales escritas a mano —la ruta del fichero y el `name:` con el
# que esta vigilado—, que es la clase de dato que se desincroniza sin que nada
# se ponga en rojo. Los dos modos de fallo, dichos claro:
#
#   * Se renombra el FICHERO live-sentinel.yml y no se tocan los dos `if:`:
#     vuelve el Issue falso cada sabado de velada. Ruidoso, pero se ve.
#   * Se cambia el `name:` del centinela y no se toca la lista de vigilados de
#     notify-on-failure.yml: entonces este workflow deja de dispararse por el
#     centinela ENTERO, y un fallo de verdad de "Hay velada?" (Neon caido,
#     cambio de esquema) sale en rojo sin que lo lea nadie hasta el lunes. Ese
#     agujero es exactamente el que se tapo el 1-ago-2026 metiendolo en la
#     lista. Este es el modo silencioso, y el caro.
#
# Se comprueba contra el YAML de verdad, no contra una copia de las cadenas.


RUTA_EN_EL_IF = re.compile(r"workflow_run\.path\s*==\s*'([^']*)'")
NEGACION = re.compile(r"&&\s*!\s*\(")


def _yaml(fichero: Path) -> dict:
    return yaml.safe_load(fichero.read_text(encoding="utf-8"))


def _condicion(fichero: Path, job: str) -> str:
    """El `if:` de un job, con los saltos del bloque plegado hechos espacios.

    El `if:` de `notify` es un `>-` de varias lineas y YAML conserva sus `\\n`
    (las lineas de continuacion van mas indentadas). Buscar subcadenas sin
    normalizar antes daria falsos negativos segun donde caiga el corte.
    """
    jobs = _yaml(fichero)["jobs"]
    assert job in jobs, f"{fichero.name} ya no tiene el job '{job}': {list(jobs)}"
    return " ".join(jobs[job]["if"].split())


def _rutas_comparadas(fichero: Path) -> dict[str, list[str]]:
    """Por cada job, las rutas de workflow que compara su `if:`."""
    return {
        nombre: RUTA_EN_EL_IF.findall(cuerpo.get("if", "") or "")
        for nombre, cuerpo in _yaml(fichero)["jobs"].items()
    }


def _vigilados(fichero: Path) -> list[str]:
    """Los `name:` de los workflows que disparan notify-on-failure."""
    datos = _yaml(fichero)
    # PyYAML es YAML 1.1, donde `on` es un booleano: la clave del bloque no es
    # la cadena "on" sino True. Se aceptan las dos por si algun dia se cambia
    # de parser y deja de pasar.
    disparadores = datos.get("on", datos.get(True))
    return disparadores["workflow_run"]["workflows"]


def test_la_excepcion_apunta_al_fichero_del_centinela_y_ese_fichero_existe():
    """EL CABO SUELTO DEL ARREGLO DEL 16-AGO. La ruta del `if:` es una cadena
    literal: si el fichero se renombra y la cadena no, la excepcion deja de
    casar y vuelve el Issue falso de cada velada."""
    esperada = SENTINEL.relative_to(RAIZ).as_posix()
    assert SENTINEL.is_file(), (
        f"no existe {esperada}. Si el centinela se ha renombrado, hay que "
        "cambiar la ruta en LOS DOS `if:` de notify-on-failure.yml."
    )

    rutas = _rutas_comparadas(NOTIFY)
    encontradas = sorted({r for lista in rutas.values() for r in lista})
    assert encontradas == [esperada], (
        f"notify-on-failure.yml compara contra {encontradas} y el centinela "
        f"vive en '{esperada}'."
    )


def test_los_dos_sitios_que_nombran_la_ruta_van_a_la_vez():
    """La ruta aparece DOS veces: en el `if:` de `notify` (que se calla) y en
    el de `centinela_autocancelado` (que deja el rastro). Cambiar solo una deja
    el peor de los dos mundos: ni Issue, ni ::notice."""
    esperada = SENTINEL.relative_to(RAIZ).as_posix()
    rutas = _rutas_comparadas(NOTIFY)

    for job in ("notify", "centinela_autocancelado"):
        assert rutas.get(job) == [esperada], (
            f"el job '{job}' de notify-on-failure.yml compara contra "
            f"{rutas.get(job)}, no contra ['{esperada}']."
        )


def test_el_centinela_sigue_vigilado_con_su_name_exacto():
    """EL MODO DE FALLO SILENCIOSO. La lista de `workflows:` casa por `name:`
    exacto. Si el titulo del centinela cambia y la lista no, notify-on-failure
    no se dispara por el y un fallo de verdad (no una autocancelacion) no abre
    ningun Issue. Es el agujero que se tapo el 1-ago-2026."""
    nombre = _yaml(SENTINEL)["name"]
    vigilados = _vigilados(NOTIFY)
    assert nombre in vigilados, (
        f"el `name:` del centinela es '{nombre}' y no esta en la lista de "
        "workflows vigilados de notify-on-failure.yml: sus fallos DE VERDAD "
        "ya no avisarian."
    )


def test_la_excepcion_solo_perdona_los_cancelled():
    """La excepcion tiene que seguir siendo del tamano exacto del problema. Un
    'failure' o un 'timed_out' del centinela son averias de las que hay que
    enterarse; solo el 'cancelled' es funcionamiento normal (el cron horario
    expulsa al run pendiente mientras otro duerme). Si alguien simplifica el
    `if:` quitando la comprobacion de la conclusion, el centinela se queda sin
    alarma para todo."""
    condicion = _condicion(NOTIFY, "notify")
    assert NEGACION.search(condicion), (
        "el `if:` de `notify` ya no niega nada: o se ha perdido la excepcion "
        "del centinela, o se ha reescrito de una forma que este test no sabe "
        "leer. Comprobalo a mano antes de tocar el test."
    )
    _, excepcion = NEGACION.split(condicion, maxsplit=1)
    assert "conclusion == 'cancelled'" in excepcion, (
        "la excepcion del centinela ya no mira la conclusion: estaria "
        "silenciando TAMBIEN sus 'failure' y 'timed_out'."
    )
    assert "workflow_run.path" in excepcion, (
        "la excepcion ya no mira de que workflow viene: estaria silenciando "
        "los 'cancelled' de todos los crons vigilados."
    )


def test_notify_sigue_avisando_de_los_tres_finales_malos():
    """'failure' no es el unico final malo (11-jul-2026: tres runs del bucle
    en 'cancelled' y ni un Issue). La excepcion del centinela recorta ese
    tercer caso para UN solo workflow, no para todos."""
    condicion = _condicion(NOTIFY, "notify")
    for final in ("failure", "timed_out", "cancelled"):
        assert f'"{final}"' in condicion, (
            f"'{final}' ha desaparecido de la lista de conclusiones que avisan."
        )


def test_la_premisa_de_la_excepcion_sigue_siendo_cierta():
    """POR QUE ES LEGITIMO CALLARSE UN 'cancelled' DEL CENTINELA: porque lo
    cancela su propia concurrency. Con `cancel-in-progress: false` el que
    duerme no se toca y el cancelado es siempre el que esperaba en la cola sin
    ejecutar ni un paso. Si algun dia se quita ese bloque o se pone en true,
    la premisa se cae y la excepcion pasa a tapar cancelaciones de verdad."""
    concurrencia = _yaml(SENTINEL)["concurrency"]
    assert concurrencia["cancel-in-progress"] is False, (
        "el centinela ha cambiado su concurrency. Con cancel-in-progress: true "
        "el cancelado seria el que duerme, y un 'cancelled' dejaria de ser "
        "inofensivo: hay que revisar la excepcion de notify-on-failure.yml."
    )


def test_el_rastro_de_la_autocancelacion_no_abre_issues():
    """A PROPOSITO, y conviene que se note al cambiarlo. Este job existe para
    que la excepcion no sea una venda: deja un ::notice y una linea en el
    resumen, pero NO manda correo (serian dos o tres por noche de velada, que
    es justo el ruido que se vino a quitar). Si algun dia se decide que la
    cancelacion REAL si avise, el dato ya esta calculado ahi; hay que borrar
    este test a sabiendas, no de refilon."""
    job = _yaml(NOTIFY)["jobs"]["centinela_autocancelado"]
    guion = " ".join(str(paso.get("run", "")) for paso in job["steps"])

    assert "gh issue create" not in guion and "gh issue comment" not in guion, (
        "el job del rastro ha empezado a abrir Issues. Es un camino que CREA "
        "cosas y que no se puede ensayar sin fusionar a main: leete el "
        "comentario de arriba antes de darlo por bueno."
    )
    assert job.get("continue-on-error") is True, (
        "el job del rastro puede tumbar el workflow que da TODAS las alarmas."
    )


# ------------------ 5. el cierre no puede cantar victoria sin haber cerrado
#
# ENCONTRADO EN LA REVISION DEL 16-AGO-2026, y es el mismo tipo de averia que
# todo este fichero persigue: silencio que parece exito.
#
# El script del job `cerrar` lleva `set -uo pipefail` pero NO `set -e`. Con el
# `gh issue close` suelto y un `echo "Issue #N cerrado."` detras, un fallo del
# `gh` (un 5xx de la API, rate limit, un token con permisos recortados) seguia
# de largo: el paso imprimia que habia cerrado y salia con 0. Y como el job
# lleva `continue-on-error: true`, el run entero se quedaba VERDE.
#
# MEDIDO ejecutando el script de HEAD con un `gh` de mentira que devuelve 502:
#   exit=0
#   gh: HTTP 502
#   Issue #42 cerrado.        <- mentira, el Issue seguia abierto
#
# Y un Issue que se queda abierto es exactamente la averia del Issue 16
# (1-ago) y la del #32 (15-ago): GitHub manda email al ABRIR, no al comentar,
# asi que el siguiente fallo de ese workflow habria sido un comentario mudo.
# Ahora el cierre se comprueba y deja un ::error si no ha salido.


def test_el_cierre_comprueba_que_el_gh_ha_funcionado():
    """El `gh issue close` no puede ir suelto seguido de un echo de exito: sin
    `set -e`, un fallo del `gh` sale con 0 y el paso miente en verde mientras
    el Issue sigue abierto amordazando el canal."""
    guion = _yaml(NOTIFY)["jobs"]["cerrar"]["steps"][0]["run"]

    assert "if gh issue close" in guion, (
        "el `gh issue close` del job `cerrar` ha vuelto a quedarse suelto. Sin "
        "`set -e`, si el `gh` falla el paso sale con 0, imprime que cerro y el "
        "Issue se queda abierto: el proximo fallo de ese workflow no manda "
        "email. Envuelvelo en un `if ... ; then ... else ... fi`."
    )
    assert "::error title=Relevo::" in guion, (
        "el cierre ya no deja constancia cuando falla. Con el "
        "`continue-on-error: true` del job, sin el `::error` no queda ni rastro."
    )


# ------------- 6. la prueba material del cierre de la captura cruda
#
# EL VOLCADO DEL SCOREBOARD NO ES PRUEBA DE NADA (revision del 16-ago-2026).
#
# `capture-live-samples.yml` cierra su propio Issue de fallo cuando un run sale
# en verde, pero solo si ademas hay PRUEBA MATERIAL de que se ha grabado algo:
# el que cierra no es el que abrio (el Issue es compartido), asi que un verde
# vacio podria apagar la alarma de una captura anterior que murio de verdad.
#
# El umbral era "al menos un fichero en samples/", y ese umbral lo cumple
# TAMBIEN el run que no captura nada: `poll_once` guarda el scoreboard ANTES de
# contar cuantos eventos trae, asi que la salida
# {"skipped": "no events in window"} deja UN fichero en samples/, no cero. Es
# medible en el propio incidente que motivo el guard: el run 31908919300 pidio
# una fecha vacia, no capturo ni un combate y aun asi subio un artifact de 1846
# bytes — el volcado del scoreboard. Con el umbral viejo, ese run habria cerrado
# el aviso de la captura A.
#
# Estos dos tests atan las dos mitades: que el guion excluye el scoreboard, y
# que la premisa (un run sin cartelera deja SOLO el scoreboard) sigue siendo
# cierta en el Python.


def _guion_del_cierre() -> str:
    for paso in _pasos(CAPTURA):
        if paso.get("name") == "Cerrar el aviso si la captura ha ido bien":
            return paso["run"]
    raise AssertionError("no esta el paso `Cerrar el aviso si la captura ha ido bien`")


def test_la_prueba_material_no_cuenta_el_volcado_del_scoreboard():
    """Contar `find samples -type f` a secas es contar siempre >= 1."""
    guion = _guion_del_cierre()

    assert "! -name '*-scoreboard.json'" in guion, (
        "el cierre de la captura ha vuelto a contar TODOS los ficheros de "
        "samples/. El volcado del scoreboard se guarda tambien cuando la "
        "ventana viene vacia, asi que ese recuento nunca da 0 y el guard deja "
        "de guardar: un run que no grabo la velada cierra el aviso de uno que "
        "murio de verdad. Cuenta solo los ficheros de evento."
    )
    assert "DE_CARTELERA" in guion and '"$DE_CARTELERA" -eq 0' in guion, (
        "la decision de cerrar tiene que colgar del recuento de ficheros de "
        "EVENTO, no del recuento total."
    )


def test_la_premisa_del_umbral_sigue_siendo_cierta(tmp_path, monkeypatch):
    """Un poll sin cartelera deja el scoreboard y NADA MAS; con cartelera deja
    ademas un `fightcenter-*` por evento, y lo deja SIN exigir combate en curso.

    Las dos mitades importan. Si algun dia `poll_once` dejara de guardar el
    scoreboard en el caso vacio, el umbral viejo volveria a valer y este test
    sobraria. Y si el `fightcenter` pasara a pedirse solo con `state == "in"`,
    el umbral nuevo dejaria el Issue abierto cada vez que la captura corriese
    entre combates: eso desgasta la alarma y hay que enterarse aqui."""
    from collections import Counter

    from scripts import capture_live_samples as captura

    def poll(scoreboard: dict, destino) -> list[str]:
        monkeypatch.setattr(
            captura, "_fetch_json",
            lambda sesion, url, params=None: (
                scoreboard if url == captura.SCOREBOARD_URL else {"ok": True}
            ),
        )
        destino.mkdir()
        captura.poll_once(None, destino, "20260821-20260822", Counter())
        return sorted(p.name for p in destino.iterdir())

    vacio = poll({"events": []}, tmp_path / "sin_cartelera")
    assert len(vacio) == 1 and vacio[0].endswith("-scoreboard.json"), (
        f"un poll sin cartelera tenia que dejar solo el scoreboard y dejo {vacio}"
    )

    # Card entero ya TERMINADO: ni una competicion en 'in'. Es el caso que el
    # umbral nuevo tiene que seguir dando por bueno.
    con_card = poll(
        {"events": [{"id": "600059185", "competitions": [
            {"id": "1", "status": {"type": {"state": "post"}}},
        ]}]},
        tmp_path / "con_cartelera",
    )
    de_evento = [n for n in con_card if not n.endswith("-scoreboard.json")]
    assert any("fightcenter-600059185" in n for n in de_evento), (
        "con una velada en la ventana tiene que quedar su `fightcenter-*` "
        f"aunque no haya ningun combate en curso, y quedo {con_card}"
    )


CITA_POR_LINEA = re.compile(r"notify-on-failure\.?y?m?l?:\d+")


def test_nadie_cita_notify_on_failure_por_numero_de_linea():
    """notify-on-failure.yml es el fichero que mas cambia (tres veces solo el
    16-ago-2026) y el que mas se cita desde otros workflows. Diez citas suyas
    por numero de linea estaban CADUCADAS a la vez: apuntaban a :45, :69,
    :78-88, :127, :133, :161 y :198 cuando el job `notify` esta en la :109, el
    `TITULO=` en la :187 y el job `cerrar` en la :294.

    Una cita caducada no rompe nada, y por eso es peligrosa: manda al que esta
    depurando por que no le llego un email al sitio equivocado, justo cuando
    tiene prisa. Se citan por NOMBRE de job o de paso, que sobrevive al
    siguiente commit."""
    culpables = []
    for fichero in sorted(WORKFLOWS.glob("*.yml")):
        for n, linea in enumerate(fichero.read_text(encoding="utf-8").splitlines(), 1):
            if CITA_POR_LINEA.search(linea):
                culpables.append(f"{fichero.name}:{n}: {linea.strip()}")

    assert not culpables, (
        "hay citas a notify-on-failure.yml por numero de linea, y caducan solas:\n"
        + "\n".join(culpables)
        + "\nCitalo por nombre: el job `notify`, el job `cerrar`, el paso "
        "`Abrir o comentar Issue de fallo`, su `on: workflow_run: workflows:`..."
    )
