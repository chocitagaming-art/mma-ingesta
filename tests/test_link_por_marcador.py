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
