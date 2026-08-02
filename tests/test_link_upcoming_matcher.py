"""El nombre de la cartelera no siempre es el nombre de la ficha.

La velada del 8-ago-2026 (evento 1087) llegó con el bout 5 sin ficha: la
cartelera dice "Jose Montanha da Silva" y ESPN guarda "José Montanha". El
combate existía en la base, pero sin `fighter_blue_id`, y el bucle del directo
empareja por id — así que ese combate no se habría escrito en toda la noche y
el job habría salido EN VERDE. Lo mismo con "Michael Venom Page" (evento 1085),
donde el apodo intercalado hunde la similitud por debajo del umbral.

Los dos fallan por motivos DISTINTOS, y por eso hacen falta los dos casos:

* "Jose Montanha da Silva" -> la búsqueda de ESPN devuelve **cero** resultados.
  No hay nada que puntuar: el umbral no interviene.
* "Michael Venom Page" -> ESPN sí devuelve "Michael Page", pero fold_ratio da
  0.80 y el umbral canónico es 0.87, así que se descarta.

Ambos valores están medidos contra la API real de ESPN el 2026-08-02.
"""

from __future__ import annotations

import pytest

from src.scrapers.enrich_ranked import search_espn_athlete_relaxed
from src.scrapers.link_upcoming_fighters import _clean_measure, _clean_stance
from src.scrapers.matching import (
    DEFAULT_THRESHOLD,
    fold_ratio,
    name_query_variants,
    token_subset_match,
)


# --------------------------------------------------------------- las variantes


def test_nombre_corto_no_genera_variantes():
    """Con uno o dos tokens no hay nada que recortar: una sola consulta."""
    assert name_query_variants("Conor McGregor") == ["Conor McGregor"]
    assert name_query_variants("Shogun") == ["Shogun"]


def test_el_nombre_entero_va_siempre_primero():
    """Un caso que hoy acierta tiene que seguir acertando en el primer intento."""
    for nombre in ("Jose Montanha da Silva", "Michael Venom Page", "Rafael dos Anjos"):
        assert name_query_variants(nombre)[0] == nombre


def test_quita_el_apodo_intercalado():
    """'Michael Venom Page' -> 'Michael Page' es la variante que resuelve el caso."""
    variantes = name_query_variants("Michael Venom Page")
    assert "Michael Page" in variantes
    # Y la de quitar el token intermedio va antes que el recorte por la derecha.
    assert variantes.index("Michael Page") < variantes.index("Michael Venom")


def test_recorta_el_apellido_de_mas_sin_dejar_particula():
    """'Jose Montanha da Silva' -> 'Jose Montanha', nunca 'Jose Montanha da'."""
    variantes = name_query_variants("Jose Montanha da Silva")
    assert "Jose Montanha" in variantes
    assert "Jose Montanha da" not in variantes
    assert all(not v.lower().endswith(" da") for v in variantes)


def test_no_repite_consultas():
    """Cada variante gasta una petición a ESPN: ninguna puede salir dos veces."""
    variantes = name_query_variants("Jose Montanha da Silva")
    assert len(variantes) == len(set(variantes))


# ----------------------------------------------------------- la guarda de tokens


def test_la_guarda_acepta_los_dos_casos_reales():
    assert token_subset_match("Jose Montanha da Silva", "José Montanha")
    assert token_subset_match("Michael Venom Page", "Michael Page")


def test_la_guarda_rechaza_a_un_luchador_distinto():
    """Sin esta guarda, una variante corta puede soldar el combate a otra persona."""
    assert not token_subset_match("Jose Montanha da Silva", "Jose Aldo")
    assert not token_subset_match("Michael Venom Page", "Michael Bisping")


def test_los_dos_casos_reales_no_llegan_al_umbral():
    """Fija por qué hace falta el arreglo: la similitud sola no basta."""
    assert fold_ratio("Jose Montanha da Silva", "José Montanha") < DEFAULT_THRESHOLD
    assert fold_ratio("Michael Venom Page", "Michael Page") < DEFAULT_THRESHOLD


# --------------------------------------------------- la búsqueda relajada entera


class _EspnFalso:
    """Imita la búsqueda de ESPN: un diccionario de consulta -> resultado."""

    def __init__(self, respuestas: dict[str, tuple[str, str]]):
        self.respuestas = respuestas
        self.consultas: list[str] = []

    def get(self, url, params, timeout):  # noqa: ARG002 - firma de requests
        self.consultas.append(params["query"])
        return self

    def raise_for_status(self):
        return None

    def json(self):
        consulta = self.consultas[-1]
        encontrado = self.respuestas.get(consulta)
        if encontrado is None:
            return {"results": []}
        athlete_id, nombre = encontrado
        return {
            "results": [
                {
                    "type": "player",
                    "contents": [
                        {"sport": "mma", "uid": f"s:3301~a:{athlete_id}", "displayName": nombre}
                    ],
                }
            ]
        }


def test_montanha_se_resuelve_con_la_variante_corta():
    """El caso del bout 5 del 1087, con los datos reales de ESPN."""
    sesion = _EspnFalso({"Jose Montanha": ("5351808", "José Montanha")})
    assert search_espn_athlete_relaxed(sesion, "Jose Montanha da Silva") == (
        "5351808",
        "José Montanha",
    )
    # El nombre entero se intenta primero, y solo entonces se recorta.
    assert sesion.consultas[0] == "Jose Montanha da Silva"


def test_michael_venom_page_se_resuelve_quitando_el_apodo():
    sesion = _EspnFalso({"Michael Page": ("3022067", "Michael Page")})
    assert search_espn_athlete_relaxed(sesion, "Michael Venom Page") == (
        "3022067",
        "Michael Page",
    )


def test_el_nombre_entero_gana_y_no_gasta_mas_peticiones():
    """Si la consulta larga acierta, no se prueba ninguna variante."""
    sesion = _EspnFalso({"Jose Montanha da Silva": ("999", "Jose Montanha da Silva")})
    assert search_espn_athlete_relaxed(sesion, "Jose Montanha da Silva") == (
        "999",
        "Jose Montanha da Silva",
    )
    assert sesion.consultas == ["Jose Montanha da Silva"]


def test_rechaza_a_un_homonimo_que_la_cartelera_no_menciona():
    """La red de seguridad: una variante corta que devuelve a OTRO luchador."""
    sesion = _EspnFalso({"Jose Montanha": ("111", "Jose Montanha Pereira")})
    assert search_espn_athlete_relaxed(sesion, "Jose Montanha da Silva") is None


def test_sin_ningun_resultado_devuelve_none():
    sesion = _EspnFalso({})
    assert search_espn_athlete_relaxed(sesion, "Jose Montanha da Silva") is None


# ------------------------------------------------------- los huecos de ESPN


@pytest.mark.parametrize("valor", [0, 0.0, None])
def test_una_medida_vacia_de_espn_se_guarda_como_null(valor):
    """Un alcance de 0 cm no es un dato: es un hueco, y alimenta el modelo."""
    assert _clean_measure(valor) is None


def test_una_medida_real_se_respeta():
    assert _clean_measure(193.04) == 193.04


@pytest.mark.parametrize("valor", ["--", "-", " -- ", "N/A", ""])
def test_una_postura_vacia_de_espn_se_guarda_como_null(valor):
    assert _clean_stance(valor) is None


@pytest.mark.parametrize("valor", ["Orthodox", "Southpaw", "Switch"])
def test_una_postura_real_se_respeta(valor):
    assert _clean_stance(valor) == valor
