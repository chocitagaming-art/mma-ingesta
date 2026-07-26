"""El indice de ufcstats se recorria ENTERO por cada evento.

`_find_ufcstats_event_url` empieza en la pagina 1 y baja hasta encontrar fechas
mas antiguas que su objetivo. Para UN evento esta bien. Para el rescate del
historico son 745 eventos, y un evento de 2015 esta sobre la pagina 20: eso son
miles de peticiones al mismo listado, la mayoria repetidas.

Medido en el primer lote real: ~20 s por evento en la zona de 2025, y subiendo
segun se va hacia atras. A ese ritmo los 745 eventos son mas de seis horas, casi
todas gastadas en releer paginas ya vistas.

La solucion es obvia una vez se ve: cargar el indice UNA vez por ejecucion y
buscar en memoria. Lo que NO puede perderse por el camino es la guarda que hace
seguro el emparejamiento — dos eventos pueden compartir fecha, y colgarle a uno
la cartelera del otro corrompe los dos.
"""

from __future__ import annotations

from datetime import date

from src.scrapers.backfill_results import emparejar_en_indice


class _Ev:
    def __init__(self, name, event_date):
        self.name = name
        self.event_date = event_date


class _Rec:
    def __init__(self, name, event_date, url):
        self.event = _Ev(name, event_date)
        self.detail_url = url


D = date(2025, 6, 7)

INDICE = [
    _Rec("UFC 316: Dvalishvili vs. O'Malley 2", D, "http://x/ufc316"),
    _Rec("UFC Fight Night: Usman vs. Buckley", date(2025, 6, 14), "http://x/fnusman"),
    _Rec("UFC 317: Topuria vs. Oliveira", date(2025, 6, 28), "http://x/ufc317"),
]


def test_empareja_por_fecha_cuando_solo_hay_uno():
    assert emparejar_en_indice("UFC 317: Topuria vs. Oliveira", date(2025, 6, 28), INDICE) == "http://x/ufc317"


def test_no_inventa_nada_si_la_fecha_no_esta():
    assert emparejar_en_indice("UFC 999: Nadie vs. Nadie", date(2030, 1, 1), INDICE) is None


def test_el_nombre_puede_diferir_mientras_comparta_un_token_distintivo():
    # ufc.com y ufcstats no escriben igual la misma cartelera. Con la fecha y un
    # apellido en comun basta.
    assert emparejar_en_indice("UFC 316: Dvalishvili vs O'Malley", D, INDICE) == "http://x/ufc316"


def test_rechaza_un_mismo_dia_sin_NINGUN_token_en_comun():
    """LA GUARDA QUE NO SE PUEDE PERDER.

    Dos eventos pueden caer el mismo dia. Si se le cuelga a uno la cartelera del
    otro se corrompen LOS DOS, porque las peleas existentes se repuntan al
    event_id equivocado. Ante la duda, mejor no emparejar: un evento sin
    consolidar se reintenta; uno corrompido hay que repararlo a mano.
    """
    indice = [_Rec("Bellator 300: Otro vs. Distinto", D, "http://x/bellator")]
    assert emparejar_en_indice("UFC 316: Dvalishvili vs. O'Malley 2", D, indice) is None


def test_entre_dos_del_mismo_dia_gana_el_que_mas_comparte():
    indice = [
        _Rec("UFC Fight Night: Otro vs. Rival", D, "http://x/malo"),
        _Rec("UFC 316: Dvalishvili vs. O'Malley 2", D, "http://x/bueno"),
    ]
    assert emparejar_en_indice("UFC 316: Dvalishvili vs. O'Malley", D, indice) == "http://x/bueno"


def test_las_palabras_genericas_no_cuentan_como_parecido():
    # 'UFC', 'Fight' y 'Night' salen en casi todas: si contaran, cualquier
    # velada del mismo dia pareceria la buena.
    indice = [_Rec("UFC Fight Night: Nadie vs. Ninguno", D, "http://x/generico")]
    assert emparejar_en_indice("UFC Fight Night: Perez vs. Lopez", D, indice) is None
