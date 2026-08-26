"""El TIPO de velada: quien puede ser el evento destacado, y la regla que lo dice.

EL PROBLEMA QUE ESTO VIGILA. El 26-ago-2026 el "Road To UFC: Maheshate vs.
Flowers" (id 1094, viernes 28, 2 combates, sin sede y sin poster) desplazo al
UFC Fight Night del sabado (id 1065, 13 combates) en la portada, en /eventos, en
/en-vivo, en /ufc-hoy, en /estado y en /directo, y ademas se llevo por delante al
centinela y al vigilante del directo. Los dos son `promotion_id = 1`, asi que la
promotora NO los distingue: lo unico que los separa es como se llaman.

La regla vive en la base (columna generada `events.tier`, migracion 028) y
`src/scrapers/event_tier.py` solo dice DE QUE LADO cae cada valor.

⚠️ LA REGLA NO SE PRUEBA AQUI CONTRA UN POSTGRES DE VERDAD, y conviene decirlo
sin adornos. La migracion 028 aun no esta aplicada; la unica base que existe es
la de PRODUCCION, y en esta maquina solo estan las herramientas CLIENTE de
PostgreSQL 17 (no hay `share/postgres.bki`, asi que `initdb` no puede levantar
un cluster de usar y tirar). Lo que se fija abajo es lo que se puede fijar
leyendo el .sql: que estan las siete ramas, que estan EN ESE ORDEN —donde vive
el unico fallo silencioso posible de la regla— y que la lista del CHECK dice lo
mismo que el CASE. La clasificacion fila a fila la comprueban las cuatro guardas
del bloque DO, dentro de la transaccion, el dia que la migracion se aplique.

DONDE SI SE PODRIA PROBAR DE VERDAD, para quien venga detras: `ci.yml` levanta
un servicio Postgres y le pasa DATABASE_URL a un paso propio, con dos ficheros
citados a mano (`test_db_pool.py` y `test_upcoming_fmid_dedup.py`). Ahi cabe un
test que cargue esta migracion en la base vacia y pase por el CASE una lista de
nombres reales — "Crypto.com UFC 331", "Noche UFC: Silva vs. Delgado", "The
Ultimate Fighter 32 Finale" — que es la unica forma de fijar los regex y no solo
su orden. Pide tocar el YAML del CI, asi que no se hizo en este commit.

No tocan la red ni la base de datos: leen un fichero.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.scrapers.event_tier import TIERS_SECUNDARIOS, evento_principal_sql

RAIZ = Path(__file__).resolve().parents[1]
MIGRACION = RAIZ / "db" / "migrations" / "028_events_tier.sql"

# El gemelo de event_tier.py en el otro repo. No siempre esta al lado.
GEMELO_WEB = RAIZ.parent / "mma-app" / "src" / "lib" / "event-tier.ts"

# Los siete valores, escritos a mano a proposito: anadir uno tiene que pasar por
# aqui y por el CHECK de la migracion, no colarse solo.
TIERS = {
    "numbered",
    "fight_night",
    "tuf_finale",
    "road_to_ufc",
    "dwcs",
    "tuf_series",
    "unknown",
}


def _sql() -> str:
    """La migracion sin sus comentarios `--`.

    Se quitan porque la cabecera del fichero cita literalmente los nombres de
    los tiers al explicar el reparto de las 792 filas, y un test que buscara
    esas cadenas en el comentario daria verde con el CASE vacio.
    """
    texto = MIGRACION.read_text(encoding="utf-8")
    return "\n".join(linea.split("--")[0] for linea in texto.splitlines())


def _ramas_del_case() -> list[tuple[str, str]]:
    """(condicion, tier) de cada rama del CASE, EN ORDEN. El ELSE va el ultimo."""
    cuerpo = _sql().split("SELECT CASE", 1)[1].split("END", 1)[0]
    ramas = [
        (" ".join(cond.split()), tier)
        for cond, tier in re.findall(r"WHEN\s+(.+?)\s+THEN\s+'([a-z_]+)'", cuerpo, re.DOTALL)
    ]
    ramas += [("ELSE", tier) for tier in re.findall(r"ELSE\s+'([a-z_]+)'", cuerpo)]
    return ramas


def _tiers_del_case() -> list[str]:
    return [tier for _, tier in _ramas_del_case()]


def _lista_del_check() -> list[str]:
    trozo = _sql().split("tier_override IN", 1)[1].split(")", 1)[0]
    return re.findall(r"'([a-z_]+)'", trozo)


def _bloque_de_guardas() -> str:
    return _sql().split("DO $guardas$", 1)[1].split("$guardas$", 1)[0]


# ------------------------------------------------------------- el predicado SQL


def test_el_predicado_lleva_el_alias_por_delante():
    assert evento_principal_sql() == "e.tier NOT IN ('road_to_ufc','dwcs','tuf_series')"


def test_sin_alias_no_deja_un_punto_colgando():
    """Las consultas que no hacen JOIN pasan alias vacio, y un `.tier` suelto no
    es un filtro mal puesto: es un error de sintaxis que revienta la consulta
    entera. Como el predicado se monta con f-strings, el punto es justo lo que
    se escapa al leerlo."""
    assert evento_principal_sql("") == "tier NOT IN ('road_to_ufc','dwcs','tuf_series')"


def test_solo_estos_tres_pueden_quedarse_fuera_del_destacado():
    """El orden tambien se fija: el predicado se compara literal en los tests
    del centinela y del vigilante, y en los del repo hermano."""
    assert TIERS_SECUNDARIOS == ("road_to_ufc", "dwcs", "tuf_series")


def test_unknown_NO_es_secundario_y_es_deliberado():
    """LA DIRECCION EN QUE FALLA LA REGLA, elegida a conciencia.

    Lista negra: lo que no reconoce cae en 'unknown' y SE VE en la portada. Un
    formato nuevo sin clasificar sale destacado —molesto, visible y de un
    renglon, que es exactamente el fallo del 26-ago— y nunca se esconde. El
    fallo contrario, un UFC Fight Night que desaparece del hero, de /en-vivo y
    del centinela en silencio un sabado por la noche, es mucho peor y ademas
    sale en verde.

    Los 8 'unknown' de la base son veladas UFC completas y TODAS pasadas
    (Ultimate Japan, UFC Macao, UFC Freedom 250, Ortiz vs Shamrock 3...): la
    prueba de que este lado es el bueno. No lo "corrijas" pensando que es un
    despiste.
    """
    assert "unknown" not in TIERS_SECUNDARIOS


def test_los_tres_secundarios_existen_de_verdad_en_la_regla():
    """Un tier que el CASE no devuelve nunca es un filtro que no filtra nada, y
    no falla: la consulta sigue siendo valida y el evento sigue colandose."""
    assert set(TIERS_SECUNDARIOS) <= set(_tiers_del_case())


# ------------------------------------------------ la regla, leida del .sql
#
# Aqui no hay Postgres (ver la cabecera). Lo que se puede comprobar sin base es
# la FORMA de la regla, y da la casualidad de que ahi es donde estan sus dos
# fallos silenciosos: una rama de menos y un orden invertido. Ninguno de los dos
# rompe nada, los dos clasifican mal en silencio.


def test_el_case_devuelve_los_siete_tipos_y_solo_esos():
    tiers = _tiers_del_case()
    assert len(tiers) == 7, f"la regla ya no tiene 7 ramas, tiene {len(tiers)}: {tiers}"
    assert set(tiers) == TIERS


def test_road_to_ufc_se_clasifica_antes_que_nada():
    """La rama del incidente va la PRIMERA a proposito.

    Hoy ninguna otra la atraparia —"Road To UFC" no lleva numero ni la palabra
    "Fight Night"—, pero el dia que se anada una rama mas ancha (un `ufc` a
    secas, por ejemplo) lo unico que mantiene al torneo de cantera fuera de la
    portada es ir el primero.
    """
    assert _ramas_del_case()[0][1] == "road_to_ufc"


def test_el_programa_tuf_se_mira_antes_que_su_finale():
    """EL ORDEN QUE VALE 28 CARTELES.

    Las dos ramas de TUF preguntan por el MISMO `ultimate[ _-]*fighter`; lo
    unico que las separa es que la primera exige ademas `!~* 'final'`. Si se
    invierten, la rama ancha se traga las 28 veladas "... Finale" y las degrada
    a tuf_series: son carteles UFC completos y desaparecerian del hero y del
    bloque "Ultimo evento" sin que fallara absolutamente nada.
    """
    orden = _tiers_del_case()
    assert orden.index("tuf_series") < orden.index("tuf_finale")

    condicion = {tier: cond for cond, tier in _ramas_del_case()}["tuf_series"]
    assert "!~*" in condicion and "final" in condicion, (
        "la rama que degrada a tuf_series ya no excluye las Finale: " + condicion
    )


def test_el_check_y_el_case_dicen_exactamente_lo_mismo():
    """La lista del CHECK es la que limita `tier_override`, la valvula manual de
    la noche de velada. Si se queda corta, el UPDATE de urgencia falla justo
    cuando hace falta; si se queda larga, deja escribir un tier que la web no
    sabe pintar (`ETIQUETA_TIER` es un Record exhaustivo en mma-app)."""
    del_check = _lista_del_check()
    assert sorted(del_check) == sorted(_tiers_del_case())
    assert len(set(del_check)) == len(del_check), f"hay valores repetidos: {del_check}"


# --------------------------------------------------------------- las guardas


def test_la_migracion_lleva_las_cuatro_guardas():
    """Son lo unico que comprueba la clasificacion FILA A FILA, y por eso este
    fichero no puede dejar que desaparezcan: sin base de datos aqui, el dia que
    se aplique la 028 son la unica prueba real de que la regla acierta."""
    bloque = _bloque_de_guardas()
    assert bloque.count("RAISE EXCEPTION") == 4, (
        "la migracion ya no tiene las cuatro guardas: "
        f"cuenta {bloque.count('RAISE EXCEPTION')}"
    )
    for aguja, porque in [
        ("name ~* 'final'", "1: ninguna velada Finale puede caer del lado secundario"),
        ("status = 'upcoming'", "2: tiene que quedar un evento futuro destacable"),
        ("id = 1065", "3: la velada del sabado 29 tiene que ser principal"),
        ("id = 1094", "4: y el Road To UFC del viernes, secundario"),
    ]:
        assert aguja in bloque, f"falta la guarda {porque} (no aparece {aguja!r})"


def test_las_guardas_van_DENTRO_de_la_transaccion():
    """Si una guarda salta fuera del BEGIN/COMMIT, la columna se queda creada y
    mal clasificada, con la excepcion impresa por encima. Lo unico que deja la
    base EXACTAMENTE como estaba es que el DO viva dentro."""
    codigo = _sql()
    assert codigo.index("BEGIN;") < codigo.index("DO $guardas$") < codigo.index("COMMIT;")


# ------------------------------------------- y el gemelo del repo hermano
#
# Mismo patron que test_el_nombre_del_servicio_es_el_mismo_en_los_tres_sitios:
# dos ficheros en dos repos que dicen lo mismo y NADA los ata.
#
# El `skipif` es de test_contract_predict_response.py y NO es cosmetico: la
# primera version de este test hacia `if not GEMELO_WEB.is_file(): return`, y un
# `return` seco cuenta como PASSED. En el CI de mma-ingesta, que clona UN repo,
# el fichero hermano nunca esta — asi que la comprobacion salia verde para
# siempre sin mirar nada. Es literalmente la trampa que el expediente lleva
# persiguiendo ("mira el numero de tests ejecutados"): un skip se ve en el
# recuento, un return se disfraza de comprobacion hecha.


@pytest.mark.skipif(
    not GEMELO_WEB.is_file(), reason="mma-app no esta junto a este repo"
)
def test_la_web_esconde_exactamente_los_mismos_tres():
    """`mma-app/src/lib/event-tier.ts` tiene su propia tabla `TIER_DESTACABLE`.

    Si los dos dejan de coincidir no falla nada en ningun sitio: la web
    escondera un tipo de velada que el centinela sigue grabando, o al reves —el
    hero pintara la velada buena y el bucle grabara la otra— y eso solo se
    descubre en directo.
    """
    tabla = re.search(
        r"TIER_DESTACABLE[^{]*\{(.*?)\};", GEMELO_WEB.read_text(encoding="utf-8"), re.DOTALL
    )
    assert tabla, "mma-app ya no declara TIER_DESTACABLE: el filtro de la web se ha movido"
    escondidos = re.findall(r"(\w+):\s*false", tabla.group(1))
    assert sorted(escondidos) == sorted(TIERS_SECUNDARIOS), (
        f"la web esconde {sorted(escondidos)} y la ingesta {sorted(TIERS_SECUNDARIOS)}"
    )
