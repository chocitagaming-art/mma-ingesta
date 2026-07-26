"""A QUE EVENTOS vuelve el backfill, y por que dejaba de volver a casi todos.

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
"""

from __future__ import annotations

from src.scrapers.backfill_results import events_needing_results_sql


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
