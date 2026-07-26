"""El centinela del directo: quien decide a que hora hay que despertar.

EL PROBLEMA QUE RESUELVE: de los 8 eventos futuros con hora, los crons del repo
solo cubren los que caen en sus dos ventanas de sabado. El 1063 (1-ago) y el
1087 (8-ago) no los cubre nada, y esta medido que el scheduler de GitHub da
~1 run/hora pidas lo que pidas (peor caso observado: 3h33 de retraso). Un cron
de un solo uso no es opcion; los workflow_dispatch arrancan al instante.

TRES TRAMPAS QUE ESTE MODULO EXISTE PARA NO PISAR, las tres medidas contra la BD
real el 26-jul-2026:

1. `start_time` NO es el principio de la velada. 7 de los 8 eventos futuros
   tienen `prelims_time` NULL, y anclar en `start_time` habria perdido las 3 h
   de prelims del 25-jul.

2. `event_date` MIENTE hasta en 24 h. El 1087 tiene event_date sabado 8-ago y
   start_time domingo 9-ago 00:00Z. Aqui se razona SIEMPRE en hora UTC absoluta.

3. La duracion NO se fija, se calcula desde la hora real de arranque. El 25-jul
   el plan decia 185 min (pensado para las 12:45), se lanzo a las 11:46 y hubo
   que recalcular a 244 para que el relevo cayera igual en el hueco muerto. Con
   185 el job A habria muerto EN MITAD de los prelims.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.live_sentinel import ancla_de_evento, plan_de_arranque

UTC = timezone.utc


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


# --------------------------------------------------------------- el ancla


def test_ancla_prefiere_las_prelims_tempranas():
    assert ancla_de_evento(
        early_prelims_time=_t("2026-07-25T12:00"),
        prelims_time=_t("2026-07-25T13:00"),
        start_time=_t("2026-07-25T16:00"),
    ) == _t("2026-07-25T12:00")


def test_ancla_cae_a_las_prelims_normales():
    # El caso del 1063 (1-ago): prelims 14:00Z, estelar 17:00Z.
    assert ancla_de_evento(None, _t("2026-08-01T14:00"), _t("2026-08-01T17:00")) == _t(
        "2026-08-01T14:00"
    )


def test_ancla_sin_prelims_resta_cuatro_horas():
    # 7 de los 8 eventos futuros estan asi. El 1087: estelar 9-ago 00:00Z.
    assert ancla_de_evento(None, None, _t("2026-08-09T00:00")) == _t("2026-08-08T20:00")


def test_ancla_sin_ninguna_hora_no_existe():
    assert ancla_de_evento(None, None, None) is None


# ------------------------------------------------- a quien se despierta y cuando


def test_evento_lejano_no_despierta_a_nadie():
    # A 6 dias vista no hay nada que hacer: el run sale verde en segundos, que
    # es el 95 % de las ejecuciones.
    plan = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"),
        start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-07-26T13:00"),
        lookahead_horas=5,
    )
    assert plan is None


def test_evento_dentro_de_la_ventana_duerme_hasta_t_menos_15():
    plan = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"),
        start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-08-01T11:00"),
        lookahead_horas=5,
    )
    assert plan is not None
    # Arranca a las 13:45Z, o sea duerme 2 h 45 min.
    assert plan.arranque == _t("2026-08-01T13:45")
    assert plan.dormir_segundos == 165 * 60


def test_el_relevo_cae_ANTES_del_estelar_no_en_mitad():
    """La leccion cara del 25-jul, convertida en test.

    Con el 1063: arranque 13:45Z y estelar a las 17:00Z. Si A durase los 235 min
    por defecto moriria a las 17:40Z, o sea EN MITAD del combate estelar, que es
    exactamente lo que se quiso evitar. Tiene que morir justo antes de que
    empiece, para que el fallo (si lo hay) se vea con la velada aun por delante.
    """
    plan = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"),
        start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-08-01T11:00"),
        lookahead_horas=5,
    )
    fin_de_a = plan.arranque + timedelta(minutes=plan.minutos_a)
    assert fin_de_a <= _t("2026-08-01T17:00"), "A no puede morir dentro del estelar"
    assert fin_de_a >= _t("2026-08-01T16:30"), "ni tan pronto que deje un hueco largo"


def test_la_duracion_se_calcula_desde_el_arranque_real():
    # Mismo evento, pero el centinela llega tarde y arranca ya empezadas las
    # prelims: la duracion de A tiene que ENCOGER, no mantenerse.
    pronto = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"), start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-08-01T11:00"), lookahead_horas=5,
    )
    tarde = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"), start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-08-01T15:30"), lookahead_horas=5,
    )
    assert tarde is not None
    assert tarde.minutos_a < pronto.minutos_a


def test_si_el_ancla_ya_paso_se_dispara_YA_sin_dormir():
    # Nunca dormir un numero negativo de segundos.
    plan = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"),
        start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-08-01T15:00"),
        lookahead_horas=5,
    )
    assert plan is not None
    assert plan.dormir_segundos == 0


def test_no_se_despierta_por_una_velada_ya_terminada():
    # 4 h despues del estelar la velada esta muerta: no tiene sentido grabar.
    plan = plan_de_arranque(
        ancla=_t("2026-08-01T14:00"),
        start_time=_t("2026-08-01T17:00"),
        ahora=_t("2026-08-01T21:30"),
        lookahead_horas=5,
    )
    assert plan is None


def test_el_sueno_nunca_agota_el_tope_de_360_minutos_del_job():
    """Tope DURO de GitHub: un job muere a los 360 min. Si el centinela durmiera
    mas de la cuenta, moriria antes de disparar y la velada se perderia sin que
    nadie lo notara — el peor fallo posible, porque sale en verde.
    """
    plan = plan_de_arranque(
        ancla=_t("2026-08-09T00:00"),
        start_time=_t("2026-08-09T04:00"),
        ahora=_t("2026-08-08T19:00"),
        lookahead_horas=6,
    )
    assert plan is not None
    margen = plan.dormir_segundos / 60 + plan.minutos_a
    assert plan.dormir_segundos / 60 <= 300, "el sueno solo, ya se pasaria del tope"
    assert margen  # el job del bucle es OTRO run: aqui solo importa el sueno


def test_el_1087_no_se_planifica_por_event_date():
    """El 1087 tiene event_date sabado 8-ago y start_time domingo 9-ago 00:00Z.

    Quien planifique por la fecha se equivoca en 20 h y lanza el bucle sobre un
    scoreboard vacio, que ademas rompe el guard de la primera pasada y termina
    en verde en segundos.
    """
    ancla = ancla_de_evento(None, None, _t("2026-08-09T00:00"))
    plan = plan_de_arranque(
        ancla=ancla, start_time=_t("2026-08-09T00:00"),
        ahora=_t("2026-08-08T17:00"), lookahead_horas=5,
    )
    assert plan is not None
    assert plan.arranque == _t("2026-08-08T19:45")
