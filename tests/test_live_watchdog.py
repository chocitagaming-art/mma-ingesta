"""El watchdog del directo: el unico que se entera de que la velada se esta
perdiendo MIENTRAS pasa.

POR QUE HACE FALTA ADEMAS DEL CENTINELA. El 1-ago-2026 la velada del 1063 se
grabo entera en blanco y los 35 workflows del repo terminaron en verde toda la
tarde. No fue mala suerte: `notify-on-failure.yml` se dispara con
`workflow_run: completed`, asi que solo puede reaccionar a un workflow que HA
CORRIDO. El bucle no corrio — y un workflow que no arranca no falla, no existe,
y no genera ningun evento al que reaccionar. Era un punto ciego estructural.

LA DIFERENCIA CON TODO LO DEMAS: este modulo no pregunta "¿ha fallado alguien?"
sino "¿esta vivo el bucle que graba?". Se contesta igual tanto si el bucle murio
a mitad como si nunca llego a lanzarse, que es justo el caso que se escapaba.

DESDE EL 17-AGO-2026 ESA PREGUNTA SE CONTESTA CON EL LATIDO, no con las
muestras. Las muestras las escribe tambien el cron de respaldo, asi que no
distinguian un bucle vivo de uno muerto — el bloque del final de este fichero
tiene los numeros medidos.

Solo hace SELECT; quien dispara es el workflow.
"""

from __future__ import annotations

from scripts.live_watchdog import estado_de_velada

# ------------------------------------------------------------ el diagnostico


def test_entran_muestras_todo_bien():
    """El caso normal de una velada bien grabada: el bucle esta escribiendo."""
    assert estado_de_velada(
        muestras_recientes=37, combates_activos=14, combates_resueltos=6
    ) == "OK"


def test_velada_en_marcha_y_cero_muestras_es_un_rescate():
    """EL CASO DEL 1-AGO. La velada llevaba horas en marcha y no entraba ni una
    muestra porque nadie habia lanzado el bucle. Nada en todo el repo era capaz
    de formular esta pregunta."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_resueltos=0
    ) == "CAIDO"


def test_el_bucle_muerto_a_mitad_tambien_es_un_rescate():
    """Un job del bucle que muere por timeout deja de escribir sin que nadie lo
    note: los combates ya resueltos siguen ahi y el reloj sigue dentro de la
    ventana. Lo unico que cambia es que las muestras se paran."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_resueltos=8
    ) == "CAIDO"


def test_velada_terminada_no_se_rescata():
    """Con todos los combates resueltos no queda nada que grabar, y relanzar el
    bucle solo gastaria un runner. Es la misma regla que el centinela usa para
    no revivir una velada acabada (hay_algo_que_grabar)."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_resueltos=14
    ) == "TERMINADA"


def test_velada_terminada_manda_aunque_sigan_entrando_muestras():
    """Orden importante: primero se mira si acabo. Al final de una velada el
    bucle sigue vivo unos minutos y sigue escribiendo; eso no es motivo para
    tratarla como viva ni para relanzar nada."""
    assert estado_de_velada(
        muestras_recientes=12, combates_activos=14, combates_resueltos=14
    ) == "TERMINADA"


def test_velada_con_un_no_contest_tambien_esta_terminada():
    """UN EMPATE Y UN NO CONTEST NO TIENEN GANADOR, Y NO SE LO VA A DAR NADIE.

    Contarlos como pendientes deja la velada eternamente sin terminar dentro de
    la ventana: en cuanto el bucle deja de escribir, `muestras_recientes` cae a
    cero y esto devolvia CAIDO, o sea un relanzamiento en falso la noche de la
    velada. Caso real: UFC 321 (Aspinall-Gane), 13 combates, 12 con ganador y
    uno anulado (method 'CNC'). Los 13 estan resueltos.
    """
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=13, combates_resueltos=13
    ) == "TERMINADA"


def test_cartelera_sin_combates_cargados_se_rescata():
    """Ante la duda, rescatar. Una cartelera que el scraper aun no ha traido no
    es una velada terminada, y una pasada de mas del bucle sale en verde en
    segundos: su primera pasada ve el scoreboard vacio y termina. Lo caro es el
    falso negativo, no el falso positivo."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=0, combates_resueltos=0
    ) == "CAIDO"


# ------------------------------------------- el latido manda sobre las muestras
#
# EL AGUJERO QUE SE CIERRA AQUI, MEDIDO. "Entran muestras" NO significa "el
# bucle esta vivo": live-results.yml (*/10) ejecuta el MISMO modulo sin
# --duration-minutes, asi que escribe muestras sin pasar nunca por
# run_bounded_loop. Cruzando los 8 runs del watchdog de la noche del UFC 330
# contra los del respaldo, LOS OCHO tenian un run de respaldo dentro de su
# ventana de 45 min: con el bucle muerto toda la noche, los ocho habrian dicho
# OK y no habria habido ni un rescate.
#
# Peor todavia: el commit que creo este watchdog (3bea1fe) anadio en el MISMO
# cambio el cron de respaldo de los sabados por el dia. O sea que el incidente
# que lo hizo nacer —la velada del 1063 perdida entera— hoy ya no se detectaria.
#
# El latido de service_heartbeats es lo unico que el respaldo NO puede fingir,
# porque solo lo escribe run_bounded_loop. Por eso MANDA sobre las muestras.


def test_el_respaldo_ya_no_puede_fingir_que_el_bucle_vive():
    """EL TEST QUE JUSTIFICA TODO EL CAMBIO. Bucle muerto (sin latir desde hace
    40 min) y el cron de respaldo escribiendo sus 2-6 muestras/h. Hoy esto es
    OK; con el latido mandando es CAIDO, que es la verdad."""
    assert estado_de_velada(
        muestras_recientes=4, combates_activos=13, combates_resueltos=5, latido_min=40.0
    ) == "CAIDO"


def test_los_paseillos_dejan_de_dar_un_caido_falso():
    """EL UNICO CAIDO DE LA HISTORIA DEL WATCHDOG FUE ESTE, Y ERA FALSO. Run
    31909928211, 15-ago 21:37:17Z, siete minutos despues del ancla:
    combates_activos 12, combates_resueltos 0, muestras_recientes 0. No podia
    haber muestras — el escritor tiene PROHIBIDO guardarlas hasta la campana del
    asalto 1. Con el bucle latiendo, esto pasa a ser OK."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=12, combates_resueltos=0, latido_min=0.7
    ) == "OK"


def test_sin_fila_de_latido_se_decide_exactamente_como_siempre():
    """Salvaguarda, no camino normal: hoy la fila existe. Cubre que alguien
    renombre el servicio o vacie la tabla — ahi se vuelve al criterio viejo en
    vez de dar CAIDO ocho veces seguidas."""
    assert estado_de_velada(
        muestras_recientes=37, combates_activos=14, combates_resueltos=6, latido_min=None
    ) == "OK"
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_resueltos=0, latido_min=None
    ) == "CAIDO"


def test_una_velada_terminada_manda_sobre_el_latido():
    """El orden de las comprobaciones no cambia: TERMINADA sigue siendo lo
    primero. Al acabar el cartel el bucle deja de latir en cuanto muere, y sin
    esto la cola de la noche daria CAIDO cada hora — medido el 15-ago: el bucle
    sello la ultima pelea a las 04:19:42Z y la velada siguio 'en marcha' 27 min
    mas."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=13, combates_resueltos=13, latido_min=90.0
    ) == "TERMINADA"


def test_el_umbral_del_latido_aguanta_el_relevo():
    """Medido en el UFC 330: el bucle A termino a las 00:51:26Z y el relevo B
    arranco a las 00:51:28Z (2 s de concurrency), pero antes de su primera
    pasada hay ~90 s de arranque del runner. El umbral tiene que dejar pasar ese
    hueco sin gritar."""
    from scripts.live_watchdog import UMBRAL_LATIDO_MINUTOS

    assert UMBRAL_LATIDO_MINUTOS >= 10, "no deja sitio al relevo A->B"
    assert estado_de_velada(
        muestras_recientes=20, combates_activos=13, combates_resueltos=6,
        latido_min=UMBRAL_LATIDO_MINUTOS - 0.1,
    ) == "OK"
    assert estado_de_velada(
        muestras_recientes=20, combates_activos=13, combates_resueltos=6,
        latido_min=UMBRAL_LATIDO_MINUTOS + 0.1,
    ) == "CAIDO"


def test_el_latido_no_cambia_el_criterio_de_disparo():
    """LA INVARIANTE ANTI-11-JUL, fijada aqui para que no se pierda.

    El latido entra en la DETECCION, nunca en el permiso para disparar. Ese
    permiso sigue siendo `steps.guard.outputs.vivos == '0'`, y por eso un CAIDO
    falso no puede costar una velada: si el bucle esta vivo el guard cuenta 1 y
    bloquea, asi que lo unico que cuesta es un correo.

    Y el caso que parecia el nudo —bucle A muerto con relevo B en cola— se cura
    solo: medido, B arranca 4 s despues de morir A, incluso cuando A muere de
    forma anomala. Rescatar ahi EXPULSARIA a B, que es el fallo del 11-jul."""
    from pathlib import Path

    yaml = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "live-watchdog.yml"
    ).read_text(encoding="utf-8")
    for linea in yaml.splitlines():
        if "gh workflow run" in linea:
            continue
        if linea.lstrip().startswith("if:") and "steps.wd.outputs.estado == 'CAIDO'" in linea:
            assert "latido" not in linea and "mudo" not in linea, (
                "el latido se ha colado en la condicion de un disparo: "
                f"{linea.strip()}"
            )

