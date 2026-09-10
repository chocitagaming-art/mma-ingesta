"""Las correcciones manuales del ranking, y las salvaguardas que impiden que se pudran.

EL FALLO QUE ESTO FIJA. El 5-sep-2026 Valentina Shevchenko vació el título de peso
mosca femenino por lesión (ESPN), y Natalia Silva vs Wang Cong pasaron a pelear por el
vacante en UFC 332 — cosa que nuestra propia tabla `fights` ya reflejaba con
`is_title_fight = true`. Pero ufc.com/rankings siguió publicando a Shevchenko como
campeona durante días, y nuestro scraper la copiaba fiel: el fallo era de la fuente, no
del scraper. Borrarlo a mano de la tabla no servía de nada, porque el cron nocturno lo
volvía a escribir a la mañana siguiente.

LA REGLA QUE ESTO PROTEGE, y es la que importa: una corrección manual es deuda. Se
aplica sola, pero **tiene que retirarse sola** en cuanto la fuente se pone al día, y
tiene que chillar si se queda ahí para siempre. Por eso cada corrección lleva una
guarda (`solo_si_campeon_es`) y una caducidad, y por eso hay un test que se pone rojo
cuando una corrección lleva caducada demasiado tiempo.
"""

from collections import Counter
from datetime import date

import pytest

from src.scrapers.rankings_correcciones import (
    CORRECCIONES,
    Correccion,
    aplicar_correcciones,
    correcciones_vigentes,
)


def _correccion(**kwargs) -> Correccion:
    base = dict(
        motivo="prueba",
        fuente="https://example.test/noticia",
        desde=date(2026, 9, 5),
        caduca=date(2026, 11, 1),
        division="womens_flyweight",
        accion="sin_campeon",
        solo_si_campeon_es="Valentina Shevchenko",
    )
    base.update(kwargs)
    return Correccion(**base)


# --------------------------------------------------------------- vigencia


def test_una_correccion_no_se_aplica_antes_de_su_fecha():
    vigentes = correcciones_vigentes([_correccion()], hoy=date(2026, 9, 1))
    assert vigentes == []


def test_una_correccion_se_aplica_dentro_de_su_ventana():
    vigentes = correcciones_vigentes([_correccion()], hoy=date(2026, 9, 12))
    assert len(vigentes) == 1


def test_una_correccion_caducada_deja_de_aplicarse():
    """Que caduque es el punto: si nadie la renueva, el scraper vuelve a la fuente."""
    vigentes = correcciones_vigentes([_correccion()], hoy=date(2026, 11, 2))
    assert vigentes == []


# --------------------------------------------------------------- la guarda


def test_la_correccion_se_retira_sola_cuando_la_fuente_se_pone_al_dia():
    """LA SALVAGUARDA PRINCIPAL. Si ufc.com ya publica a otra campeona, la corrección
    no debe tocar nada: la fuente ya es correcta y pisarla sería introducir el error
    que veníamos a evitar."""
    filas = [
        {"division": "womens_flyweight", "rank_position": 0, "is_champion": True,
         "fighter_name": "Natalia Silva"},
        {"division": "womens_flyweight", "rank_position": 1, "is_champion": False,
         "fighter_name": "Manon Fiorot"},
    ]
    counts: Counter = Counter()

    resultado = aplicar_correcciones(filas, [_correccion()], hoy=date(2026, 9, 12), counts=counts)

    assert len(resultado) == 2, "la campeona nueva no se toca"
    assert counts["correcciones_ya_no_necesarias"] == 1
    assert counts["correcciones_aplicadas"] == 0


def test_la_correccion_quita_a_la_campeona_cuando_la_fuente_sigue_desactualizada():
    filas = [
        {"division": "womens_flyweight", "rank_position": 0, "is_champion": True,
         "fighter_name": "Valentina Shevchenko"},
        {"division": "womens_flyweight", "rank_position": 1, "is_champion": False,
         "fighter_name": "Natalia Silva"},
        {"division": "flyweight", "rank_position": 0, "is_champion": True,
         "fighter_name": "Joshua Van"},
    ]
    counts: Counter = Counter()

    resultado = aplicar_correcciones(filas, [_correccion()], hoy=date(2026, 9, 12), counts=counts)

    nombres = [f["fighter_name"] for f in resultado]
    assert "Valentina Shevchenko" not in nombres
    assert "Natalia Silva" in nombres, "los clasificados de la división NO se tocan"
    assert "Joshua Van" in nombres, "otras divisiones NO se tocan"
    assert counts["correcciones_aplicadas"] == 1


def test_no_toca_a_la_luchadora_si_ademas_esta_clasificada_en_otra_division():
    """Quitar 'la campeona' no puede convertirse en 'borrar a esa persona del ranking'."""
    filas = [
        {"division": "womens_flyweight", "rank_position": 0, "is_champion": True,
         "fighter_name": "Valentina Shevchenko"},
        {"division": "womens_pound_for_pound", "rank_position": 1, "is_champion": False,
         "fighter_name": "Valentina Shevchenko"},
    ]
    counts: Counter = Counter()

    resultado = aplicar_correcciones(filas, [_correccion()], hoy=date(2026, 9, 12), counts=counts)

    assert len(resultado) == 1
    assert resultado[0]["division"] == "womens_pound_for_pound", (
        "sigue siendo la nº1 libra por libra aunque ya no tenga cinturón"
    )


def test_una_correccion_que_no_encuentra_nada_que_corregir_se_registra():
    """Si la división no está en el volcado, hay que enterarse: o cambió el slug o el
    scraper dejó de traer esa división."""
    filas = [{"division": "flyweight", "rank_position": 0, "is_champion": True,
              "fighter_name": "Joshua Van"}]
    counts: Counter = Counter()

    resultado = aplicar_correcciones(filas, [_correccion()], hoy=date(2026, 9, 12), counts=counts)

    assert len(resultado) == 1
    assert counts["correcciones_sin_efecto"] == 1


def test_sin_correcciones_las_filas_pasan_intactas():
    filas = [{"division": "flyweight", "rank_position": 0, "is_champion": True,
              "fighter_name": "Joshua Van"}]
    counts: Counter = Counter()

    assert aplicar_correcciones(filas, [], hoy=date(2026, 9, 12), counts=counts) == filas
    assert counts["correcciones_aplicadas"] == 0


def test_una_accion_desconocida_falla_ruidosamente():
    """Una corrección mal escrita no puede pasar en silencio dejando el ranking mal."""
    mala = _correccion(accion="haz_lo_que_puedas")
    with pytest.raises(ValueError, match="haz_lo_que_puedas"):
        aplicar_correcciones([], [mala], hoy=date(2026, 9, 12), counts=Counter())


# --------------------------------------------------------------- higiene


def test_las_correcciones_del_repo_estan_bien_formadas():
    for c in CORRECCIONES:
        assert c.motivo.strip(), "toda corrección explica por qué existe"
        assert c.fuente.startswith("http"), f"{c.division}: hace falta una fuente citable"
        assert c.desde < c.caduca, f"{c.division}: la ventana va al revés"
        assert (c.caduca - c.desde).days <= 120, (
            f"{c.division}: una corrección manual no puede durar más de cuatro meses. "
            "Si hace falta más, es que el arreglo va en otro sitio."
        )


def test_no_se_acumulan_correcciones_zombis():
    """Las correcciones caducadas hace mucho se borran, no se dejan de recuerdo.

    Sin este test, el fichero se llena de excepciones muertas y nadie sabe cuáles
    siguen vivas. Se da un mes de gracia tras la caducidad para retirarlas con calma.
    """
    hoy = date.today()
    zombis = [c for c in CORRECCIONES if (hoy - c.caduca).days > 30]
    assert not zombis, (
        "correcciones caducadas hace más de 30 días, hay que borrarlas: "
        + ", ".join(f"{c.division} (caducó {c.caduca})" for c in zombis)
    )


# --------------------------------------------------------------- integración real


def test_funciona_con_el_RankingRecord_de_verdad():
    """El scraper no pasa dicts, pasa dataclasses. Si esto se rompe, el cron escribe
    la campeona equivocada aunque los tests de arriba estén verdes."""
    from src.scrapers.repositories.rankings import RankingRecord

    def record(division, pos, campeon, nombre):
        return RankingRecord(
            fighter_id=None, promotion_id=1, division=division, rank_position=pos,
            snapshot_date=date(2026, 9, 12), is_champion=campeon,
            fighter_name=nombre, rank_change=None,
        )

    filas = [
        record("womens_flyweight", 0, True, "Valentina Shevchenko"),
        record("womens_flyweight", 1, False, "Natalia Silva"),
    ]
    counts: Counter = Counter()

    resultado = aplicar_correcciones(filas, [_correccion()], hoy=date(2026, 9, 12), counts=counts)

    assert [f.fighter_name for f in resultado] == ["Natalia Silva"]
    assert counts["correcciones_aplicadas"] == 1


def test_la_correccion_del_repo_limpia_el_ranking_real_de_hoy():
    """La comprobación que vale: la corrección que está EN EL FICHERO, sobre el volcado
    que ufc.com sirve hoy, deja la división de peso mosca femenino sin campeona."""
    from src.scrapers.repositories.rankings import RankingRecord

    hoy = date(2026, 9, 12)
    filas = [
        RankingRecord(fighter_id=6260, promotion_id=1, division="womens_flyweight",
                      rank_position=0, snapshot_date=hoy, is_champion=True,
                      fighter_name="Valentina Shevchenko", rank_change=None),
        RankingRecord(fighter_id=7003, promotion_id=1, division="womens_flyweight",
                      rank_position=1, snapshot_date=hoy, is_champion=False,
                      fighter_name="Natalia Silva", rank_change=None),
        RankingRecord(fighter_id=6260, promotion_id=1, division="womens_pound_for_pound",
                      rank_position=1, snapshot_date=hoy, is_champion=False,
                      fighter_name="Valentina Shevchenko", rank_change=None),
    ]
    counts: Counter = Counter()

    resultado = aplicar_correcciones(filas, hoy=hoy, counts=counts)  # sin pasar correcciones: usa las del repo

    campeonas = [f for f in resultado if f.division == "womens_flyweight" and f.is_champion]
    assert campeonas == [], "el título está vacante: no puede haber campeona"
    p4p = [f for f in resultado if f.division == "womens_pound_for_pound"]
    assert len(p4p) == 1, "sigue siendo la nº1 libra por libra"
