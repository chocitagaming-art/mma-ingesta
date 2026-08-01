"""El watchdog del directo: el unico que se entera de que la velada se esta
perdiendo MIENTRAS pasa.

POR QUE HACE FALTA ADEMAS DEL CENTINELA. El 1-ago-2026 la velada del 1063 se
grabo entera en blanco y los 35 workflows del repo terminaron en verde toda la
tarde. No fue mala suerte: `notify-on-failure.yml` se dispara con
`workflow_run: completed`, asi que solo puede reaccionar a un workflow que HA
CORRIDO. El bucle no corrio — y un workflow que no arranca no falla, no existe,
y no genera ningun evento al que reaccionar. Era un punto ciego estructural.

LA DIFERENCIA CON TODO LO DEMAS: este modulo no vigila procesos, vigila el DATO.
No pregunta "¿ha fallado alguien?" sino "¿hay velada en marcha y NO estan
entrando muestras?". Esa pregunta se contesta igual tanto si el bucle murio a
mitad como si nunca llegó a lanzarse, que es justo el caso que se escapaba.

Solo hace SELECT; quien dispara es el workflow.
"""

from __future__ import annotations

from scripts.live_watchdog import estado_de_velada

# ------------------------------------------------------------ el diagnostico


def test_entran_muestras_todo_bien():
    """El caso normal de una velada bien grabada: el bucle esta escribiendo."""
    assert estado_de_velada(
        muestras_recientes=37, combates_activos=14, combates_con_ganador=6
    ) == "OK"


def test_velada_en_marcha_y_cero_muestras_es_un_rescate():
    """EL CASO DEL 1-AGO. La velada llevaba horas en marcha y no entraba ni una
    muestra porque nadie habia lanzado el bucle. Nada en todo el repo era capaz
    de formular esta pregunta."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_con_ganador=0
    ) == "CAIDO"


def test_el_bucle_muerto_a_mitad_tambien_es_un_rescate():
    """Un job del bucle que muere por timeout deja de escribir sin que nadie lo
    note: los combates ya resueltos siguen ahi y el reloj sigue dentro de la
    ventana. Lo unico que cambia es que las muestras se paran."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_con_ganador=8
    ) == "CAIDO"


def test_velada_terminada_no_se_rescata():
    """Con todos los combates resueltos no queda nada que grabar, y relanzar el
    bucle solo gastaria un runner. Es la misma regla que el centinela usa para
    no revivir una velada acabada (hay_algo_que_grabar)."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=14, combates_con_ganador=14
    ) == "TERMINADA"


def test_velada_terminada_manda_aunque_sigan_entrando_muestras():
    """Orden importante: primero se mira si acabo. Al final de una velada el
    bucle sigue vivo unos minutos y sigue escribiendo; eso no es motivo para
    tratarla como viva ni para relanzar nada."""
    assert estado_de_velada(
        muestras_recientes=12, combates_activos=14, combates_con_ganador=14
    ) == "TERMINADA"


def test_cartelera_sin_combates_cargados_se_rescata():
    """Ante la duda, rescatar. Una cartelera que el scraper aun no ha traido no
    es una velada terminada, y una pasada de mas del bucle sale en verde en
    segundos: su primera pasada ve el scoreboard vacio y termina. Lo caro es el
    falso negativo, no el falso positivo."""
    assert estado_de_velada(
        muestras_recientes=0, combates_activos=0, combates_con_ganador=0
    ) == "CAIDO"
