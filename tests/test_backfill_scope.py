"""A QUE EVENTOS (y a que COMBATES) vuelve el backfill, y por que dejaba de
volver a casi todos.

Medido contra la BD real el 26-jul-2026, y ninguna de las tres cosas estaba
documentada:

1. CHURN POR PELEAS CANCELADAS. El EXISTS no excluia `status='cancelled'`, y una
   pelea cancelada no tiene metodo ni stats POR DEFINICION. Resultado: el 1062
   (2 canceladas) y el 1061 (3) recalificaban en CADA pasada durante 60 dias
   para no hacer absolutamente nada, con su fetch a ufcstats incluido.

2. CHURN POR EL METODO 'KO/TKO'. `ESPN_PROVISIONAL_METHODS` incluye 'KO/TKO'
   porque es lo que el bucle escribe en directo... pero ufcstats tambien lo
   escribe tal cual cuando no detalla el golpe. Como los dos textos son
   identicos, McGregor-Holloway (12838) se reescribia una y otra vez: cinco
   veces en once horas el 26-jul. El dato no se corrompe (la escritura es
   idempotente) pero inutiliza `bouts_filled` como senal de progreso.
   La condicion sobra: una pelea a la que le falte afinar el metodo tambien
   carece de stats o de arbitro, asi que ya recalifica por esas dos.

3. EL FILTRO QUE DE VERDAD TAPABA EL HISTORICO no era la ventana de 60 dias que
   decia el plan, sino `e.source = 'ufc.com'`. En la BD hay 787 eventos: 26 con
   source 'ufc.com' y 761 con source NULL (todo lo importado por otras vias).
   Los 735 eventos con peleas sin arbitro estan TODOS en el grupo NULL, asi que
   quitar la ventana temporal no habria rescatado ni uno.

4. UN DESGLOSE POR ASALTOS A MEDIAS PARECIA COMPLETO (medido el 16-ago-2026).
   La rama de `fight_stats_rounds` solo contaba luchadores DISTINTOS, y un
   combate que guarda solo el R1 ya tiene 2. El 14895 (Njokuani-Alvarez, UFC
   330, decision unanime a 3 asaltos) se quedo con 1 de sus 3 asaltos y, peor,
   con unos totales que eran los del R1 (46/69 y 22/50 en vez de 155/229 y
   74/164): datos FALSOS publicados en la ficha. Ni el evento recalificaba ni,
   aunque lo hubiera hecho, `_fill_event` habria tocado el combate: su gate
   (`has_round_stats`) miraba lo mismo. Por eso hay dos arreglos y dos bloques
   de tests aqui abajo, y por eso se EJECUTAN contra SQLite en memoria: que la
   cadena SQL contenga un texto no demuestra a quien selecciona.

Los tests de este fichero no abren ningun socket a Neon. Los que comprueban
comportamiento montan un esquema minimo en SQLite y ejecutan el SQL de verdad:
el modo `historico` no lleva ningun `%s`, asi que corre tal cual.
"""

from __future__ import annotations

import sqlite3

from src.scrapers.backfill_results import _get_bouts, events_needing_results_sql


def _sql(**kw) -> str:
    sql, _ = events_needing_results_sql(**kw)
    return " ".join(sql.split())  # normaliza espacios para poder buscar frases


# ------------------------------------------------------------------ el churn


def test_las_peleas_canceladas_nunca_mantienen_vivo_un_evento():
    # Sin esto, el 1062 recalifica 60 dias seguidos por sus 2 canceladas.
    assert "fi.status IS DISTINCT FROM 'cancelled'" in _sql()
    assert "fi.status IS DISTINCT FROM 'cancelled'" in _sql(historico=True)


def test_el_metodo_provisional_ya_no_recalifica_por_si_solo():
    # Era la puerta por la que McGregor-Holloway volvia en cada pasada.
    assert "ESPN_PROVISIONAL" not in _sql()
    assert "fi.method = ANY" not in _sql()


def test_sigue_volviendo_por_lo_que_de_verdad_falta():
    sql = _sql()
    assert "fi.method IS NULL" in sql
    assert "fight_stats" in sql
    assert "fight_stats_rounds" in sql
    assert "fi.referee IS NULL" in sql


# ------------------------------------------------------- el alcance historico


def test_el_modo_normal_sigue_acotado():
    """El cron diario NO cambia de alcance: sigue barato y previsible.

    La ventana de 60 dias existe para que una pelea permanentemente
    inconsolidable (un nombre que no casa nunca, o una pelea que ufcstats no
    lista) no recalifique para siempre y haga crecer el trabajo del cron sin
    limite. Eso sigue valiendo.
    """
    sql = _sql()
    assert "e.source = %s" in sql
    assert "INTERVAL '1 day'" in sql


def test_el_modo_historico_abre_source_y_ventana():
    """El rescate del pasado va por lotes A MANO, nunca colgado del cron.

    Son 735 eventos y 8.210 peleas: a ~1,25 s por peticion son horas. Por eso
    se hace con --historico --limit N, midiendo lo que rinde cada lote, en vez
    de soltarlo en un cron diario que nadie mira.
    """
    sql = _sql(historico=True)
    assert "e.source = %s" not in sql
    assert "INTERVAL '1 day'" not in sql
    assert "e.event_date < CURRENT_DATE" in sql, "un evento futuro no se consolida"


def test_los_parametros_acompanan_al_sql():
    # El modo normal lleva source y ventana; el historico, ninguno de los dos.
    _, normales = events_needing_results_sql()
    _, historicos = events_needing_results_sql(historico=True)
    assert len(normales) == 2
    assert historicos == []


def test_el_orden_pone_lo_reciente_primero():
    # Si un lote se corta, que se haya rescatado lo que mas se consulta.
    assert "ORDER BY e.event_date DESC" in _sql(historico=True)


# ------------------------------------- el desglose a medias, SQL EJECUTADO


ESQUEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY, name TEXT, event_date TEXT, source TEXT
);
CREATE TABLE fights (
    id INTEGER PRIMARY KEY, event_id INTEGER, status TEXT, method TEXT,
    referee TEXT, winner_id INTEGER, end_round INTEGER,
    fighter_red_id INTEGER, fighter_blue_id INTEGER,
    fighter_red_name TEXT, fighter_blue_name TEXT
);
CREATE TABLE fighters (id INTEGER PRIMARY KEY, name TEXT, source_id TEXT);
CREATE TABLE fight_stats (fight_id INTEGER, fighter_id INTEGER);
CREATE TABLE fight_stats_rounds (fight_id INTEGER, fighter_id INTEGER, round INTEGER);
CREATE TABLE fight_scorecards (fight_id INTEGER, judge TEXT);
"""

# Los dos luchadores del 14895 real, para que se lea de donde sale cada numero.
NJOKUANI, ALVAREZ = 6956, 6511
LOS_DOS = (NJOKUANI, ALVAREZ)  # un asalto COMPLETO: fila de cada esquina


def _bd(combates, event_date="date('now','-1 day')") -> sqlite3.Connection:
    """Un evento pasado (el 1064) con los combates que se le indiquen.

    Cada combate es un dict con lo que lo distingue del caso "ya consolidado":
    `end_round` y `asaltos`, la lista de (asalto, luchadores) guardada en
    fight_stats_rounds. Todo lo demas va relleno a proposito — metodo, arbitro,
    ganador y las 2 filas de totales — para que ninguna de las ramas VIEJAS del
    OR pueda dar el evento por bueno: si el evento sale, sale por la rama nueva.
    """
    db = sqlite3.connect(":memory:")
    db.executescript(ESQUEMA)
    db.execute(
        "INSERT INTO events (id, name, event_date, source)"
        f" VALUES (1064, 'UFC 330', {event_date}, 'ufc.com')"
    )
    for combate in combates:
        fight_id = combate["id"]
        db.execute(
            """
            INSERT INTO fights (id, event_id, status, method, referee, winner_id,
                                end_round, fighter_red_id, fighter_blue_id,
                                fighter_red_name, fighter_blue_name)
            VALUES (?, 1064, ?, ?, ?, ?, ?, ?, ?, 'Chidi Njokuani', 'Rafael Alvarez')
            """,
            (
                fight_id, combate.get("status"), combate.get("method", "U-DEC"),
                combate.get("referee", "Keith Peterson"), NJOKUANI,
                combate.get("end_round"), NJOKUANI, ALVAREZ,
            ),
        )
        db.execute(
            "INSERT INTO fight_scorecards VALUES (?, 'Brent Colflesh')", (fight_id,)
        )
        for fighter_id in (NJOKUANI, ALVAREZ):
            db.execute("INSERT INTO fight_stats VALUES (?, ?)", (fight_id, fighter_id))
        for asalto, luchadores in combate.get("asaltos", []):
            for fighter_id in luchadores:
                db.execute(
                    "INSERT INTO fight_stats_rounds VALUES (?, ?, ?)",
                    (fight_id, fighter_id, asalto),
                )
    return db


def _eventos(db) -> list[int]:
    sql, params = events_needing_results_sql(historico=True)
    assert params == [], "el modo historico no lleva %s: se ejecuta tal cual"
    return [fila[0] for fila in db.execute(sql).fetchall()]


def test_un_combate_con_solo_el_r1_devuelve_su_evento_a_la_cola():
    """El caso 14895: 3 asaltos disputados, 1 guardado. Es el bug entero.

    Con la SQL anterior este evento devolvia 0 filas (2 luchadores distintos en
    fight_stats_rounds ya bastaban), asi que las pasadas de consolidacion de las
    11:00 y las 15:00 UTC salian en vacio dia tras dia.
    """
    db = _bd([{"id": 14895, "end_round": 3, "asaltos": [(1, LOS_DOS)]}])
    assert _eventos(db) == [1064]


def test_un_combate_con_todos_sus_asaltos_no_vuelve_a_la_cola():
    # Sin esto la rama nueva seria churn puro: 8.775 de las 8.776 peleas con
    # desglose cuadran, y ninguna debe recalificar su evento cada dia.
    db = _bd([{
        "id": 14895, "end_round": 3,
        "asaltos": [(1, LOS_DOS), (2, LOS_DOS), (3, LOS_DOS)],
    }])
    assert _eventos(db) == []


def test_un_asalto_con_un_solo_luchador_cuenta_como_ausente():
    """El segundo modo de fallo: `round_stats_unmatched` deja medio asalto.

    Aqui hay 2 luchadores distintos (la rama vieja calla) y 2 asaltos distintos;
    lo que falta es la fila de Alvarez en el R2. Se cuentan asaltos COMPLETOS,
    asi que 1 < 2 y el evento vuelve. Hoy no existe ningun caso asi en la BD.
    """
    db = _bd([{
        "id": 14895, "end_round": 2,
        "asaltos": [(1, LOS_DOS), (2, (NJOKUANI,))],
    }])
    assert _eventos(db) == [1064]


def test_un_combate_sin_ningun_asalto_sigue_calificando_por_la_rama_vieja():
    # Las 19 peleas de 1995-1998 que ufcstats nunca desgloso: la rama nueva las
    # deja fuera (su gate EXISTS), pero la de siempre las mantiene elegibles.
    db = _bd([{"id": 14895, "end_round": 1, "asaltos": []}])
    assert _eventos(db) == [1064]


def test_un_combate_sin_resultado_no_reclama_asaltos():
    """Sin `end_round` no hay con que comparar: 64 futuras y 10 canceladas.

    El combate lleva su R1 completo y todo lo demas relleno; lo unico que le
    falta es el `end_round`. No debe salir por la rama nueva.
    """
    db = _bd([{"id": 14895, "end_round": None, "asaltos": [(1, LOS_DOS)]}])
    assert _eventos(db) == []


def test_un_combate_cancelado_tampoco_reclama_asaltos():
    # Doble proteccion: `end_round` NULL y la guarda de 'cancelled'.
    db = _bd([{"id": 14895, "status": "cancelled", "end_round": None, "asaltos": []}])
    assert _eventos(db) == []


def test_un_evento_futuro_nunca_califica_aunque_le_falten_asaltos():
    db = _bd(
        [{"id": 14895, "end_round": 3, "asaltos": [(1, LOS_DOS)]}],
        event_date="date('now','+7 day')",
    )
    assert _eventos(db) == []


# ------------------------------ el gate por COMBATE de _fill_event (_get_bouts)


class _CursorPg:
    """Cursor con la API que usa el scraper (context manager, %s) sobre SQLite."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        self._cursor.execute(sql.replace("%s", "?"), tuple(params or ()))

    def fetchall(self):
        return self._cursor.fetchall()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ConexionPg:
    def __init__(self, db):
        self._db = db

    def cursor(self, cursor_factory=None):
        return _CursorPg(self._db.cursor())


def _has_round_stats(db) -> dict[int, bool]:
    return {bout.id: bout.has_round_stats for bout in _get_bouts(_ConexionPg(db), 1064)}


def test_get_bouts_no_da_por_completo_un_desglose_a_medias():
    """El arreglo que de verdad repara el 14895.

    De nada sirve que el evento recalifique si `_fill_event` sigue saltandose el
    combate: el 1064 YA volvio en su dia por la rama del arbitro, se le
    escribieron arbitro y tarjetas, y las stats siguieron falsas. `has_round_stats`
    pasa a significar "el desglose esta COMPLETO", no "hay algo de desglose".
    """
    db = _bd([
        {"id": 14895, "end_round": 3, "asaltos": [(1, LOS_DOS)]},
        {"id": 12885, "end_round": 3, "asaltos": [
            (1, LOS_DOS), (2, LOS_DOS), (3, LOS_DOS),
        ]},
    ])
    assert _has_round_stats(db) == {14895: False, 12885: True}


def test_get_bouts_conserva_el_comportamiento_de_las_peleas_sin_resultado():
    # end_round NULL con 0 filas de asalto seguia siendo False antes (0 < 2
    # luchadores) y lo sigue siendo ahora (0 < COALESCE(NULL, 1) asaltos).
    db = _bd([{"id": 14900, "end_round": None, "asaltos": []}])
    assert _has_round_stats(db) == {14900: False}
