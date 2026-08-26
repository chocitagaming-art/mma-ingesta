"""El panel de mano del directo: sobre QUE velada informa cuando nadie se lo dice.

POR QUE ESTE FICHERO EXISTE. De los tres scripts del directo que cambio el
arreglo del 26-ago-2026, `live_watch.py` era el unico sin una sola prueba
propia: `test_scripts_importables.py` comprueba que importa y se acaba ahi. Y
resulta que es justo el que `OPERACIONES.md` llama "la comprobacion que no
admite discusion" — el que se abre a mano, en mitad de la velada, para ver con
los ojos si entran muestras. Un script que solo se usa bajo presion y que nadie
prueba es la peor combinacion posible.

EL FALLO QUE VIGILA. Su ventana es `CURRENT_DATE - 1 .. CURRENT_DATE + 7` y
ordena por fecha a secas, asi que el sabado 29 el "Road To UFC" del viernes 28
(id 1094, 2 combates, `promotion_id = 1` igual que la velada buena) seguia
dentro de la ventana y salia el PRIMERO. Sin `--event-id`, el panel habria dicho
"0 muestras" con el UFC Fight Night del sabado (id 1065) grabandose
perfectamente — y esa lectura es con la que se decide si hay que rescatar el
bucle a mano. Un falso CAIDO a las once de la noche cuesta un rescate en falso.

No se abre ningun socket: el cursor es el `RecordingCursor` de conftest, que
contesta a partir del SQL que se le manda. Aqui ademas ese SQL ES lo que se
juzga, porque el responder imita a Postgres y filtra —o no— segun el predicado
que traiga la consulta.
"""

from __future__ import annotations

from datetime import date

from scripts.live_watch import _resolve_event_id

# Las dos filas del incidente, en el orden que las devolvia el ORDER BY: el
# viernes va antes que el sabado, y eso era todo lo que decidia.
ROAD_TO_UFC = (1094, "Road To UFC: Maheshate vs. Flowers", date(2026, 8, 28))
FIGHT_NIGHT = (1065, "UFC Fight Night: Imavov vs. Borralho", date(2026, 8, 29))


def _postgres_de_mentira(sql, params=None):
    """Contesta como contestaria la base: aplica el filtro SI el SQL lo trae."""
    if params:  # la rama de --event-id, que no filtra por tier a proposito
        return [(int(params[0]), "el que ha pedido el operador", date(2026, 8, 28))]
    filas = [ROAD_TO_UFC, FIGHT_NIGHT]
    if "tier NOT IN" in " ".join(sql.split()):
        filas = [f for f in filas if f[0] != 1094]
    return filas[:1]  # LIMIT 1


def test_sin_event_id_el_panel_mira_la_velada_del_sabado(fakedb):
    """EL CRITERIO DE ACEPTACION, y se pone rojo con el codigo de antes.

    Sin el predicado el responder devuelve la lista sin filtrar y el panel se
    queda con la primera fila, que es el Road To UFC del viernes: exactamente lo
    que pasaba el 26-ago-2026.
    """
    cur = fakedb.Cursor(_postgres_de_mentira)

    event_id, nombre, _fecha = _resolve_event_id(cur, None)

    assert event_id == 1065, (
        f"el panel del directo se ha ido a mirar {event_id} ({nombre}): sin el "
        "filtro por tier diria '0 muestras' con la velada grabando."
    )


def test_la_consulta_sin_alias_no_deja_un_punto_colgando(fakedb):
    """LA TRAMPA DEL ALIAS VACIO, y este es el UNICO sitio de la ingesta que la pisa.

    Esta consulta hace `FROM events` a secas, sin alias, asi que llama a
    `evento_principal_sql('')`. Si esa funcion no contemplara el alias vacio, lo
    que se concatenaria seria `AND .tier NOT IN (...)`, y eso Postgres no lo
    acepta: `syntax error at or near "."`. No seria el evento equivocado, seria
    la consulta REVENTADA — y el panel se abre a mitad de velada, cuando no hay
    tiempo de leer un traceback. El gemelo de la web
    (`mma-app/src/lib/event-tier.ts`) tuvo exactamente este fallo.
    """
    cur = fakedb.Cursor(_postgres_de_mentira)
    _resolve_event_id(cur, None)

    sql = " ".join(cur.executed[0][0].split())
    assert "tier NOT IN" in sql, "la consulta ha perdido el filtro por tier"
    assert ".tier" not in sql, f"punto colgando delante de tier:\n{sql}"


def test_con_event_id_manda_el_operador_y_no_el_filtro(fakedb):
    """LA REGRESION CONTRARIA, que es la facil de meter "por coherencia".

    `--event-id` existe para mirar UNA velada concreta, y a veces esa velada es
    justo una secundaria (un Road To UFC que si se quiera grabar, una prueba).
    Si alguien pusiera el filtro tambien en esta rama, el panel contestaria "no
    existe" a un id que el operador acaba de leer en pantalla. El filtro va
    donde la pregunta es "¿cual es EL evento?", nunca donde ya se ha dicho cual.
    """
    cur = fakedb.Cursor(_postgres_de_mentira)

    event_id, _nombre, _fecha = _resolve_event_id(cur, 1094)

    assert event_id == 1094
    sql = " ".join(cur.executed[0][0].split())
    assert "tier" not in sql, f"la rama de --event-id no debe filtrar:\n{sql}"
