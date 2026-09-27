"""La via buena para rellenar un hueco de cartelera: el marcador de ESPN.

Un combate de una cartelera futura puede llegar con una esquina sin ficha. La
tentacion es buscar ese nombre en ESPN, y es una trampa: el 2-ago-2026 la
cartelera decia "Jose Montanha da Silva", la busqueda por nombre devolvia a
"Jose Montanha" (ESPN 5351808) y el que peleaba de verdad era 4389073,
"Henrique da Silva Lopes", apodo "Montanha". Dos personas distintas, y el error
salia EN VERDE en el panel.

La via que no puede equivocarse no mira nombres: mira identificadores. Si la
OTRA esquina ya tiene ficha, se busca en el marcador de ESPN el combate donde
aparece ese luchador, y el rival es quien sea que ESPN ponga enfrente. Es la
misma fuente que leera el bucle en directo la noche de la velada, asi que si
aqui casa, alli casa.
"""

from __future__ import annotations

from src.scrapers.espn_live_results import LiveFight
from src.scrapers.link_upcoming_fighters import resolver_rival_por_marcador

# Ids reales del marcador de ESPN del 8-ago-2026 (evento 600060621).
SUTHERLAND = "5080572"
EL_QUE_PELEA = "4389073"      # "Henrique da Silva Lopes", apodo "Montanha"
EL_PARECIDO = "5351808"       # "Jose Montanha": otra persona
GAMROT = "6495000"
SALKILLD = "6344000"


def _pelea(comp, rojo, azul, nombre_rojo="Rojo", nombre_azul="Azul") -> LiveFight:
    return LiveFight(
        competition_id=comp,
        red_espn_id=rojo,
        blue_espn_id=azul,
        red_name=nombre_rojo,
        blue_name=nombre_azul,
        winner_espn_id=None,
        completed=False,
        state="pre",
        method=None,
        end_round=None,
        end_time=None,
    )


CARTELERA = (
    _pelea("401897978", EL_QUE_PELEA, SUTHERLAND, "Henrique da Silva Lopes", "Louie Sutherland"),
    _pelea("401897979", GAMROT, SALKILLD, "Mateusz Gamrot", "Quillan Salkilld"),
)


# ------------------------------------------------------------- el caso real


def test_el_bout_5_se_resuelve_por_el_rival_conocido():
    """Sabemos quien es Sutherland; ESPN dice contra quien pelea. Sin adivinar."""
    assert resolver_rival_por_marcador(SUTHERLAND, CARTELERA) == EL_QUE_PELEA


def test_y_ese_id_NO_es_el_que_devolvia_la_busqueda_por_nombre():
    """El contraejemplo, fijado: la via por nombre daba a otra persona."""
    assert resolver_rival_por_marcador(SUTHERLAND, CARTELERA) != EL_PARECIDO


def test_funciona_igual_desde_la_otra_esquina():
    assert resolver_rival_por_marcador(EL_QUE_PELEA, CARTELERA) == SUTHERLAND


# ------------------------------------------------- cuando NO debe resolver nada


def test_si_el_ancla_no_esta_en_el_marcador_no_se_inventa_nadie():
    """Cartelera aun sin publicar, o el luchador cambio: hueco visible."""
    assert resolver_rival_por_marcador("9999999", CARTELERA) is None


def test_sin_ancla_no_hay_resolucion():
    """Las dos esquinas sin ficha: no hay por donde agarrar el combate."""
    assert resolver_rival_por_marcador("", CARTELERA) is None
    assert resolver_rival_por_marcador(None, CARTELERA) is None


def test_un_ancla_en_dos_combates_no_resuelve():
    """No deberia pasar nunca; si pasa, el marcador esta mal y adivinar es peor."""
    cartelera = CARTELERA + (_pelea("401897980", SUTHERLAND, GAMROT),)
    assert resolver_rival_por_marcador(SUTHERLAND, cartelera) is None


def test_un_rival_sin_id_no_resuelve():
    """ESPN publica a veces la esquina como hueco (TBD)."""
    cartelera = (_pelea("401897981", SUTHERLAND, None),)
    assert resolver_rival_por_marcador(SUTHERLAND, cartelera) is None


def test_marcador_vacio_no_resuelve():
    assert resolver_rival_por_marcador(SUTHERLAND, ()) is None



# ------------------------- el marcador puede ir por detras de un sustituto
#
# Desde el 27-sep-2026 un enlace hecho por el marcador ya no se recalcula cada
# manana: el upsert de la cartelera lo conserva mientras ufc.com no cambie el
# nombre del hueco. Por eso el rival que da el marcador tiene que PARECERSE al
# nombre del hueco. Si ufc.com ya puso a un sustituto y ESPN aun publica al que
# se retiro, enlazar soldaria al retirado para siempre (foto, record y, tras la
# velada, la victoria y las estadisticas del que si peleo).

from src.scrapers import link_upcoming_fighters as link  # noqa: E402
from src.scrapers.link_upcoming_fighters import (  # noqa: E402
    nombre_compatible,
    nombre_rival_en_marcador,
)


def test_nombre_rival_en_marcador_lee_la_otra_esquina():
    assert nombre_rival_en_marcador(SUTHERLAND, EL_QUE_PELEA, CARTELERA) == "Henrique da Silva Lopes"
    assert nombre_rival_en_marcador(EL_QUE_PELEA, SUTHERLAND, CARTELERA) == "Louie Sutherland"
    assert nombre_rival_en_marcador(SUTHERLAND, GAMROT, CARTELERA) is None


def test_compatibles_los_casos_reales():
    # Montanha: comparten "Silva". Osmanli: la otra transliteracion del nombre.
    assert nombre_compatible("Jose Montanha da Silva", ["Henrique da Silva Lopes"])
    assert nombre_compatible("Mahammadali Osmanli", ["Mehemmedeli Osmanli"])
    assert nombre_compatible("Ilimbek Akylbek", ["Ilimbek Akylbek Uulu"])
    # Tina Black: la ficha dice Valesca Machado, pero su apodo es el de guerra.
    assert nombre_compatible("Tina Black", ["Valesca Machado"], ["Tina Black"])
    # Medidos en la base el 27-sep: orden invertido y una letra sin plegar.
    assert nombre_compatible("Liu Ce", ["Ce Liu"])
    assert nombre_compatible("Jan Błachowicz", ["Jan Blachowicz"])


def test_incompatible_el_retirado_que_el_marcador_aun_publica():
    assert not nombre_compatible("Bruno Bravo", ["Carl Charlie"])
    assert not nombre_compatible("Tina Black", ["Valesca Machado", None])
    # Las particulas no cuentan como parecido.
    assert not nombre_compatible("Joao da Costa", ["Pedro da Luz"])
    assert not nombre_compatible("Bruno Bravo", [None])


def _hueco_por_marcador(monkeypatch, *, nombre_marcador, existente, ficha, por_nombre=None):
    rival = "7777777"
    peleas = (_pelea("1", SUTHERLAND, rival, "Louie Sutherland", nombre_marcador),)
    monkeypatch.setattr(link, "_espn_id_de_ficha", lambda c, fid: SUTHERLAND)
    monkeypatch.setattr(link, "_peleas_del_marcador", lambda *a, **k: peleas)
    monkeypatch.setattr(link, "get_fighter_id_by_espn_id", lambda c, eid: existente)
    monkeypatch.setattr(link, "_nombres_de_ficha", lambda c, fid: ficha)
    monkeypatch.setattr(link, "_resolve", lambda session, name: por_nombre)
    return rival


def test_resolver_hueco_rechaza_al_retirado_y_cae_a_la_busqueda_por_nombre(monkeypatch):
    _hueco_por_marcador(monkeypatch, nombre_marcador="Carl Charlie", existente=30,
                        ficha=("Carl Charlie", None))
    counts = {"por_marcador": 0, "por_nombre": 0, "marcador_rechazado": 0}
    hueco = (18000, "blue", "Bruno Bravo", 1098, None, 6434)
    assert link._resolver_hueco(None, None, hueco, 1, {}, counts) == (None, None, None)
    assert counts["marcador_rechazado"] == 1
    assert counts["por_marcador"] == 0


def test_resolver_hueco_acepta_el_nombre_de_guerra_por_el_apodo(monkeypatch):
    _hueco_por_marcador(monkeypatch, nombre_marcador="Valesca Machado", existente=9132,
                        ficha=("Valesca Machado", "Tina Black"))
    counts = {"por_marcador": 0, "por_nombre": 0, "marcador_rechazado": 0}
    hueco = (16351, "blue", "Tina Black", 1091, None, 9131)
    assert link._resolver_hueco(None, None, hueco, 1, {}, counts) == (None, None, 9132)
    assert counts["por_marcador"] == 1
    assert counts["marcador_rechazado"] == 0


def test_el_nombre_de_pila_solo_no_basta():
    # Revision del 27-sep: "Michael Johnson" pasaba por "Michael Chiesa" solo
    # por compartir "Michael". El apellido si cuenta; el nombre de pila, no.
    assert not nombre_compatible("Michael Johnson", ["Michael Chiesa", "Maverick"])
    assert not nombre_compatible("Jose Delgado", ["Jose Aldo"])
    assert not nombre_compatible("Brandon Moreno", ["Brandon Royval"])
    assert nombre_compatible("Randy Brown", ["Randy Brown"])



def test_una_palabra_del_apodo_no_cuenta_como_apellido():
    # Revision del 27-sep: 'The King' (apodo de Erik Silva) hacia pasar a
    # 'Sean King III' por el. El apodo solo vale como nombre ENTERO.
    assert not nombre_compatible("Sean King III", ["Erik Silva"], ["The King"])
    assert nombre_compatible("Tina Black", ["Valesca Machado"], ["Tina Black"])
