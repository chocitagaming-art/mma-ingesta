"""El bucle del directo siempre salia en VERDE, pasara lo que pasara.

`main()` hacia un `return` desnudo (espn_live_results.py:708-715) y
`notify-on-failure.yml` filtra por conclusion == 'failure', asi que esa alerta
era ESTRUCTURALMENTE incapaz de avisar de una noche de directo perdida. Deuda
seria: el 1-ago y el 8-ago hay eventos y no habra nadie mirando.

Y `loop_failures` NO basta como criterio, que es lo que se creia: es ciego a los
otros dos tragaderos. Hay TRES:
  1. el except de run_bounded_loop      -> loop_failures
  2. process_events, por evento         -> event_errors
  3. match_db_event, que hace `continue` -> events_unmatched, sin excepcion

Precedente real: el run 29179498181 hizo 109 pasadas con fights_updated=0 en las
109, event_errors=0, loop_failures=0 y conclusion SUCCESS. 3h56m de runner y cero
escrituras, en verde. Cualquier criterio que solo mire loop_failures volveria a
certificar esa noche como un exito.

Y con 0 fallos en 2339 pasadas de 8 runs no hay NINGUNA base para elegir un
umbral numerico hoy: se empieza avisando (::warning::) y solo se sale en rojo en
el caso que no admite discusion — que fallaran TODAS las pasadas.
"""

from collections import Counter

from src.scrapers.espn_live_results import loop_exit_code, loop_health_warnings


def _totals(**kwargs) -> Counter:
    # Por ASIGNACION, no con Counter.update(), que SUMA en vez de fijar. Es la
    # misma trampa contra la que avisa run_bounded_loop, y cayo en ella este
    # propio helper: pedir loop_iterations=40 daba 140.
    base = Counter({"loop_iterations": 100, "loop_failures": 0})
    for clave, valor in kwargs.items():
        base[clave] = valor
    return base


# ------------------------------------------------------- la noche buena, callada


def test_healthy_night_says_nothing():
    # Cifras reales del 1062: 618 pasadas, 0 fallos, 345 muestras escritas.
    totals = _totals(
        loop_iterations=618, live_events=398, events_matched=398,
        fights_updated=12, live_stats_written=345, live_stats_skipped_final=1200,
    )
    assert loop_health_warnings(totals) == []
    assert loop_exit_code(totals) == 0


def test_sealed_card_is_not_a_problem():
    # EL CASO DE ESTA MISMA NOCHE: el cron del sabado 20:45 corre sobre una
    # cartelera ya sellada. No escribe NADA y es correctisimo — se lo salta todo
    # por is_final. Confundirlo con un fallo estrenaria la alerta en falso.
    totals = _totals(live_events=235, events_matched=235, live_stats_skipped_final=2820)
    assert loop_health_warnings(totals) == []
    assert loop_exit_code(totals) == 0


def test_window_without_event_is_not_a_problem():
    # Ventana lanzada de mas: scoreboard vacio, salida barata en verde.
    assert loop_health_warnings(_totals(loop_iterations=1, scoreboard_events=0)) == []


# ---------------------------------------------- los tres tragaderos, cada uno


def test_hard_failures_are_reported():
    avisos = loop_health_warnings(_totals(loop_failures=7, live_stats_written=50))
    assert any("7" in a for a in avisos)


def test_event_errors_are_reported_even_with_zero_loop_failures():
    # El tragadero nº2: nunca llega al except del bucle.
    avisos = loop_health_warnings(_totals(event_errors=9, live_stats_written=50))
    assert avisos
    assert any("event_errors" in a or "evento" in a for a in avisos)


def test_unmatched_events_are_reported():
    # El tragadero nº3: `continue` sin excepcion y sin contador de error. Es el
    # que se dispara cuando ufc.com renombra la cartelera durante la semana.
    avisos = loop_health_warnings(_totals(events_unmatched=100, live_stats_written=50))
    assert avisos


def test_unresolved_live_fight_is_reported():
    # Una pelea que DEJO de resolver a mitad de velada.
    assert loop_health_warnings(_totals(live_stats_unresolved=30, live_stats_written=50))


# ------------------------------------------- el modo de fallo que ya ocurrio


def test_ran_all_night_and_wrote_nothing():
    # Run 29179498181: 109 pasadas, evento encontrado, cero escrituras, verde.
    totals = _totals(loop_iterations=109, live_events=109, events_matched=109)
    avisos = loop_health_warnings(totals)
    assert avisos, "el modo de fallo que ya paso tiene que avisar"
    assert any("escrib" in a for a in avisos)


def test_no_writes_but_results_already_filled_is_fine():
    # Relevo que entra con la cartelera ya resuelta por el job anterior.
    totals = _totals(live_events=50, events_matched=50, fights_already_filled=600)
    assert loop_health_warnings(totals) == []


# ---------------------------------------------------------- el codigo de salida


def test_exit_stays_zero_while_something_worked():
    # Politica deliberada: se AVISA, no se tumba. Sin muestra real no hay forma
    # de calibrar un umbral, y un rojo en falso quema la alerta para siempre
    # (notify-on-failure reutiliza un unico Issue y no lo cierra solo).
    assert loop_exit_code(_totals(loop_failures=50, live_stats_written=10)) == 0
    assert loop_exit_code(_totals(event_errors=99)) == 0


def test_exit_is_red_when_every_single_pass_failed():
    # El unico caso que no admite discusion: si TODAS las pasadas revientan, la
    # ventana no produjo nada y el job tiene que salir en rojo para que la
    # alerta (verificada el 25-jul con el canario) abra su Issue.
    assert loop_exit_code(_totals(loop_iterations=40, loop_failures=40)) == 1


def test_exit_is_green_if_at_least_one_pass_survived():
    assert loop_exit_code(_totals(loop_iterations=40, loop_failures=39)) == 0


def test_exit_is_red_when_the_night_produced_nothing():
    """El caso que YA ocurrio tiene que tumbar el job, no solo avisar.

    El run 29179498181 hizo 109 pasadas, encontro el evento, escribio CERO y
    salio SUCCESS. Avisar no basta: el 1-ago y el 8-ago no hay nadie leyendo
    los `::warning::`, y notify-on-failure solo mira `conclusion == failure`.

    Y este no necesita esperar a las 2-3 veladas de muestra, al reves que el
    criterio porcentual sobre loop_failures: es BINARIO. O escribio algo, o
    encontro algo ya sellado, o la noche se perdio entera. No hay umbral que
    calibrar ni distribucion que estimar, asi que se puede endurecer HOY.
    """
    totals = _totals(loop_iterations=109, live_events=109, events_matched=109)
    assert loop_health_warnings(totals), "premisa: este caso ya avisaba"
    assert loop_exit_code(totals) == 1


def test_the_two_criteria_never_diverge():
    """La condicion vive en UN solo sitio, no copiada en dos.

    Si `loop_health_warnings` y `loop_exit_code` la escribieran cada uno por su
    lado, un dia se tocaria una y no la otra, y el bucle avisaria de algo por lo
    que no sale en rojo (o peor, al reves). Este test fija la equivalencia sobre
    los casos frontera que mas cuestan.
    """
    perdida = _totals(live_events=50, events_matched=50)
    sellada = _totals(live_events=50, live_stats_skipped_final=600)
    resuelta = _totals(live_events=50, fights_already_filled=600)

    assert loop_health_warnings(perdida) and loop_exit_code(perdida) == 1
    assert loop_health_warnings(sellada) == [] and loop_exit_code(sellada) == 0
    assert loop_health_warnings(resuelta) == [] and loop_exit_code(resuelta) == 0


def test_exit_ignores_an_empty_loop():
    # Sin pasadas no hay nada que juzgar (no dividir por cero ni inventar rojo).
    assert loop_exit_code(Counter()) == 0


# ------------------------------------- que el Counter LLEGUE relleno de verdad


def test_run_bounded_loop_really_fills_the_two_keys(monkeypatch):
    """LA TRAMPA QUE ESTE TEST EXISTE PARA EVITAR: `totals` es un Counter y
    Counter.__missing__ devuelve 0, asi que totals['loop_failures'] da 0 cuando
    la clave NUNCA se escribio — indistinguible de "cero fallos". Un test que
    parchee run_bounded_loop pasaria en verde aunque la funcion real no rellenara
    nada. Por eso aqui se parchea refresh_live_results y se llama a la REAL.
    """
    from src.scrapers import espn_live_results as mod

    pasadas = {"n": 0}

    def _fake_refresh(dates=None, dry_run=False):
        pasadas["n"] += 1
        if pasadas["n"] == 2:
            raise RuntimeError("ESPN se cayo en esta pasada")
        return Counter({"scoreboard_events": 1, "live_events": 1, "live_stats_written": 3})

    monkeypatch.setattr(mod, "refresh_live_results", _fake_refresh)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    totals = mod.run_bounded_loop(
        dates=None, dry_run=True, duration_minutes=0, interval_seconds=1
    )
    # duration_minutes=0 -> el deadline vence tras la 1a pasada. Forzamos varias
    # llamando otra vez no es posible, asi que se comprueba la 1a ventana.
    assert totals["loop_iterations"] >= 1
    assert "loop_iterations" in totals  # la CLAVE existe, no es el 0 por defecto
    assert "loop_failures" in totals


def test_run_bounded_loop_counts_a_real_failure(monkeypatch):
    from src.scrapers import espn_live_results as mod

    def _always_breaks(dates=None, dry_run=False):
        raise RuntimeError("ESPN caido")

    monkeypatch.setattr(mod, "refresh_live_results", _always_breaks)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    totals = mod.run_bounded_loop(
        dates=None, dry_run=True, duration_minutes=0, interval_seconds=1
    )
    assert totals["loop_failures"] == totals["loop_iterations"] >= 1
    # Y la ventana entera reventada SI tiene que salir en rojo.
    assert loop_exit_code(totals) == 1
    assert loop_health_warnings(totals)
