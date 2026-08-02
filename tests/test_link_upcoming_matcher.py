"""Por que el enlazador de carteleras NO afloja su busqueda por nombre.

El 2-ago-2026 el bout 5 del evento 1087 (velada del sabado 8) llevaba dias sin
`fighter_blue_id`: la cartelera de ufc.com dice "Jose Montanha da Silva" y la
busqueda de ESPN no encuentra ese nombre. Se intento arreglarlo probando
variantes mas cortas del nombre, con `token_subset_match` como red de
seguridad. Parecia razonable y estaba mal:

    "Jose Montanha"  ->  ESPN 5351808  "Jose Montanha"    nacido 1996, 6-1-0
    el que pelea de verdad:
                         ESPN 4389073  "Henrique da Silva Lopes"
                                       apodo "Montanha",  nacido 1985, 5-2-0

Son dos personas. Lo zanja el calendario de ESPN: el evento 600060621 (la
velada del 8-ago) aparece en el de 4389073 y NO en el de 5351808. La ficha
creada por la via relajada paso todas las comprobaciones cosmeticas -- el panel
mostraba "22/22 luchadores con ficha" -- mientras `preflight_live_results`, que
compara contra el marcador de ESPN que es lo UNICO que lee el bucle en directo,
seguia diciendo 10/11. Ese combate se habria grabado en blanco y el job habria
salido EN VERDE.

Estos tests fijan la leccion para que nadie la deshaga sin querer.
"""

from __future__ import annotations

import inspect

import pytest

from src.scrapers import link_upcoming_fighters
from src.scrapers.link_upcoming_fighters import _clean_measure, _clean_stance
from src.scrapers.matching import (
    DEFAULT_THRESHOLD,
    IDENTITY_THRESHOLD,
    fold_ratio,
    token_subset_match,
)

# Nombre tal como lo imprime ufc.com en la cartelera, y los dos candidatos.
NOMBRE_CARTELERA = "Jose Montanha da Silva"
EL_QUE_PELEA = "Henrique da Silva Lopes"   # ESPN 4389073
EL_PARECIDO = "Jose Montanha"              # ESPN 5351808, otra persona


# ------------------------------------------------- la ruta de identidad, estricta


def test_el_enlazador_usa_la_busqueda_estricta():
    """Esta funcion CREA fichas y las suelda a un combate: es identidad.

    Si alguien vuelve a cambiarla por una busqueda relajada o por variantes del
    nombre, este test cae y el comentario de `_resolve` explica por que.
    """
    fuente = inspect.getsource(link_upcoming_fighters._resolve)
    assert "_search_espn_athlete(session, name)" in fuente
    for prohibido in ("relaxed", "variants", "variantes"):
        assert prohibido not in fuente.split('"""')[-1], (
            f"_resolve vuelve a aflojar la busqueda ({prohibido!r}). "
            "Lee el docstring: 'Jose Montanha da Silva' no es 'Jose Montanha'."
        )


def test_el_subconjunto_de_tokens_no_basta_para_identificar_a_nadie():
    """El contraejemplo real, fijado en un test.

    `token_subset_match` es cierto para el nombre de la cartelera y el del
    luchador EQUIVOCADO. Es una guarda util para descartar, pero NO es prueba
    de identidad, y usarla como tal fue el error.
    """
    assert token_subset_match(NOMBRE_CARTELERA, EL_PARECIDO) is True
    # ...y sin embargo el que pelea es el otro, cuyo nombre ni siquiera se
    # parece al de la cartelera.
    assert fold_ratio(NOMBRE_CARTELERA, EL_QUE_PELEA) < DEFAULT_THRESHOLD


def test_la_politica_de_umbrales_sigue_siendo_la_escrita():
    """Las rutas que enlazan un fighter_id piden mas que las de enriquecimiento."""
    assert IDENTITY_THRESHOLD > DEFAULT_THRESHOLD
    assert (IDENTITY_THRESHOLD, DEFAULT_THRESHOLD) == (0.92, 0.87)


def test_un_hueco_sin_resolver_se_queda_sin_resolver():
    """La respuesta correcta a "no lo encuentro" es no inventarse a nadie.

    `_resolve` devuelve None cuando la busqueda no da resultado, y
    `link_upcoming` lo cuenta como `unresolved` en vez de enlazar algo
    aproximado. Un hueco visible se arregla; una ficha equivocada, no: sale en
    verde y se descubre despues de la velada.
    """
    class SinResultados:
        def get(self, url, params, timeout):  # noqa: ARG002 - firma de requests
            return self

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    assert link_upcoming_fighters._resolve(SinResultados(), NOMBRE_CARTELERA) is None


# ------------------------------------------------------- los huecos de ESPN


@pytest.mark.parametrize("valor", [0, 0.0, None])
def test_una_medida_vacia_de_espn_se_guarda_como_null(valor):
    """Un alcance de 0 cm no es un dato: es un hueco, y alimenta el modelo.

    Habia 27 fichas con `reach_cm = 0` y 26 con `stance = '--'` cuando se
    escribio esto. Aqui solo se crean fichas NUEVAS, asi que convertir el hueco
    en NULL no puede pisar un dato bueno.
    """
    assert _clean_measure(valor) is None


def test_una_medida_real_se_respeta():
    assert _clean_measure(193.04) == 193.04


@pytest.mark.parametrize("valor", ["--", "-", " -- ", "N/A", ""])
def test_una_postura_vacia_de_espn_se_guarda_como_null(valor):
    assert _clean_stance(valor) is None


@pytest.mark.parametrize("valor", ["Orthodox", "Southpaw", "Switch"])
def test_una_postura_real_se_respeta(valor):
    assert _clean_stance(valor) == valor
