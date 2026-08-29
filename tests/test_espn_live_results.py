"""Fase 6 / BE1: casi-en-vivo — resultados desde el scoreboard público de ESPN.

Fixture JSON recortada de una respuesta real del endpoint
site.api.espn.com/.../mma/ufc/scoreboard?dates=20250628 (UFC 317): la misma
estructura events[].competitions[] con competitors[2] {id, winner, athlete,
linescores}, status {period, displayClock, type} y details[] con el
"Unofficial Winner <X>". Sin red y sin BD real (fakedb).

Contrato de calidad: ESPN solo RELLENA NULLs (COALESCE con el valor almacenado
ganando), escribe códigos provisionales ('Decision'/'Submission'/'KO/TKO') que
el frontend formatea y agrupa bien, y nunca escribe nada sin ganador (empates/
NC quedan para ufcstats).
"""

from collections import Counter
from datetime import date, datetime, timezone

from src.scrapers.espn import _build_exact_name_index, _build_normalized_name_index
from src.scrapers.espn_live_results import (
    ESPN_PROVISIONAL_METHODS,
    LiveEvent,
    _resolve_fighter,
    candidate_event_dates,
    default_dates_window,
    events_worth_processing,
    match_db_event,
    method_from_details,
    method_from_status,
    parse_scoreboard,
    process_events,
)
from src.scrapers.repositories.fighters import FighterMatchRecord
from src.scrapers.repositories.fights import fill_fight_result

# ------------------------------------------------------------------ fixture


def _competitor(espn_id, name, winner, with_linescores=False):
    competitor = {
        "id": espn_id,
        "order": 1 if winner else 2,
        "winner": winner,
        "athlete": {"displayName": name, "fullName": name},
    }
    if with_linescores:
        competitor["linescores"] = [
            {"value": 29.0, "linescores": [{"value": 29.0}, {"value": 29.0}, {"value": 29.0}]}
        ]
    return competitor


def _competition(comp_id, red, blue, detail_text, period, clock, completed=True, state="post"):
    return {
        "id": comp_id,
        "date": "2025-06-28T23:00Z",
        "competitors": [red, blue],
        "status": {
            "clock": 0.0,
            "displayClock": clock,
            "period": period,
            "type": {"state": state, "completed": completed, "description": "Final"},
        },
        "details": (
            [{"id": "1", "type": {"id": "9", "text": detail_text}}] if detail_text else []
        ),
    }


SCOREBOARD = {
    "leagues": [{"slug": "ufc"}],
    "events": [
        {
            "id": "600053168",
            "name": "UFC 317: Topuria vs. Oliveira",
            "date": "2025-06-28T23:00Z",
            "competitions": [
                _competition(
                    "401758683",
                    _competitor("4350812", "Ilia Topuria", True),
                    _competitor("2504169", "Charles Oliveira", False),
                    "Unofficial Winner Kotko", 1, "2:27",
                ),
                _competition(
                    "401772509",
                    _competitor("2560746", "Alexandre Pantoja", True),
                    _competitor("4243885", "Kai Kara-France", False),
                    "Unofficial Winner Submission", 3, "1:55",
                ),
                _competition(
                    "401772657",
                    _competitor("5120301", "Joshua Van", True, with_linescores=True),
                    _competitor("4239928", "Brandon Royval", False, with_linescores=True),
                    "Unofficial Winner Decision", 3, "5:00",
                ),
                # Pelea aún en curso: no debe escribirse nada de ella.
                _competition(
                    "401772650",
                    _competitor("3085551", "Beneil Dariush", False),
                    _competitor("3028863", "Renato Moicano", False),
                    None, 2, "3:12", completed=False, state="in",
                ),
            ],
        }
    ],
}

# DB ids que el responder resuelve por (source='espn', source_id).
ESPN_TO_DB_ID = {"4350812": 101, "2504169": 102, "2560746": 103, "4243885": 104,
                 "5120301": 105, "4239928": 106, "3085551": 107, "3028863": 108}

FIGHTER_ROWS = [
    (db_id, name, None, None, None, None, None, None, None)
    for db_id, name in [
        (101, "Ilia Topuria"), (102, "Charles Oliveira"), (103, "Alexandre Pantoja"),
        (104, "Kai Kara-France"), (105, "Joshua Van"), (106, "Brandon Royval"),
        (107, "Beneil Dariush"), (108, "Renato Moicano"),
    ]
]


def _responder(sql, params=None):
    flat = " ".join(sql.split())
    if flat.startswith("SELECT id, name, nickname"):  # get_all_fighters
        return FIGHTER_ROWS
    if "FROM fighters WHERE source" in flat:  # get_fighter_id_by_source
        db_id = ESPN_TO_DB_ID.get(params[1])
        return [(db_id,)] if db_id else []
    # get_fighter_id_by_espn_id: mira los DOS sitios donde puede vivir el id
    # (`source_id` de las filas nacidas en ESPN y la columna `espn_id` de las
    # nacidas en ufcstats), lleva el OR entre paréntesis y pasa el parámetro por
    # nombre, no por posición. Es la que usa `_resolve_fighter` desde el 29-ago.
    if "FROM fighters WHERE (source" in flat:
        db_id = ESPN_TO_DB_ID.get(params["espn_id"])
        return [(db_id,)] if db_id else []
    if flat.startswith("SELECT id FROM events"):  # get_event_id (exact) -> miss
        return []
    if flat.startswith("SELECT id, name FROM events"):  # token fallback -> hit
        return [(5, "UFC 317: Topuria vs Oliveira")]
    if "FROM fights WHERE event_id" in flat:  # find_fight_id_by_fighters
        return [(11,)]
    # last_in_progress_clock: el bucle SI vio la pelea en curso y guardo el mismo
    # reloj que trae ahora el scoreboard, o sea la cuenta atras congelada. Sin
    # esta rama el responder devolvia [] (sin serie) y desde el 16-ago-2026 eso
    # significa "no escribo hora", con lo que este test dejaba de cubrir la
    # inversion que precisamente vino a fijar.
    if "FROM live_fight_stat_samples" in flat and "state = 'in'" in flat:
        return [("2:27",)]
    if flat.startswith("UPDATE fights"):
        return [(1,)]  # rowcount 1 -> "updated"
    return []


# ------------------------------------------------------------------ parsing


def test_parse_scoreboard_structure_and_method_mapping():
    events = parse_scoreboard(SCOREBOARD)
    assert len(events) == 1
    event = events[0]
    assert event.name == "UFC 317: Topuria vs. Oliveira"
    assert event.start_utc == datetime(2025, 6, 28, 23, 0, tzinfo=timezone.utc)
    assert len(event.fights) == 4
    # Las 3 variantes de details -> códigos provisionales que el frontend pinta bien.
    assert [f.method for f in event.fights] == ["KO/TKO", "Submission", "Decision", None]
    main = event.fights[0]
    assert (main.red_name, main.blue_name) == ("Ilia Topuria", "Charles Oliveira")
    assert main.winner_espn_id == "4350812"
    assert (main.end_round, main.end_time) == (1, "2:27")
    assert main.completed is True
    live = event.fights[3]
    assert live.completed is False and live.state == "in"
    assert live.winner_espn_id is None


def test_method_from_details_ignores_noise_and_logs_unknown():
    noise = [{"type": {"text": "Takedown Attempt"}}, {"type": {"text": "Fight Over"}}]
    assert method_from_details(noise) is None
    assert method_from_details(noise + [{"type": {"text": "Unofficial Winner Kotko"}}]) == "KO/TKO"
    # Un "Unofficial Winner <nuevo>" desconocido no inventa un método.
    assert method_from_details([{"type": {"text": "Unofficial Winner Doctor Stoppage"}}]) is None


def test_espn_provisional_methods_are_the_three_written_codes():
    assert set(ESPN_PROVISIONAL_METHODS) == {"Decision", "Submission", "KO/TKO"}


# -------------------------------------------------------------------- dates


def test_candidate_event_dates_covers_the_midnight_utc_window():
    # Main card 03:00Z del domingo = sábado noche US -> se prueban ambas fechas.
    past_midnight = datetime(2025, 6, 29, 3, 0, tzinfo=timezone.utc)
    assert candidate_event_dates(past_midnight) == [date(2025, 6, 28), date(2025, 6, 29)]
    # 23:00Z: misma fecha en Eastern y UTC -> una sola candidata.
    same_day = datetime(2025, 6, 28, 23, 0, tzinfo=timezone.utc)
    assert candidate_event_dates(same_day) == [date(2025, 6, 28)]
    assert candidate_event_dates(None) == []


def test_default_dates_window_is_today_and_yesterday_us_eastern():
    # 03:00Z del 29 = 23:00 del 28 en US Eastern -> ventana 27-28.
    now = datetime(2025, 6, 29, 3, 0, tzinfo=timezone.utc)
    assert default_dates_window(now) == "20250627-20250628"


def test_events_worth_processing_guard_skips_pre_only_events():
    events = parse_scoreboard(SCOREBOARD)
    assert events_worth_processing(events) == events  # hay in/post
    # Evento de la ventana aún sin empezar (todo 'pre') -> el guard lo descarta.
    pre_only = {
        "events": [
            {
                "id": "2", "name": "UFC Future Card", "date": "2025-06-28T23:00Z",
                "competitions": [
                    _competition(
                        "401000001",
                        _competitor("1", "Fighter A", False),
                        _competitor("2", "Fighter B", False),
                        None, 0, "5:00", completed=False, state="pre",
                    )
                ],
            }
        ]
    }
    assert events_worth_processing(parse_scoreboard(pre_only)) == []
    no_fights = LiveEvent(espn_id="3", name="Empty", start_utc=None, fights=())
    assert events_worth_processing([no_fights]) == []


# ----------------------------------------------------------------- matching


def test_resolve_fighter_prefers_espn_source_then_fuzzy_name(fakedb):
    conn = fakedb.Connection(_responder)
    fighters = [FighterMatchRecord(7, "Viviane Araujo", None, None, None, None, None, None, None)]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    counts = Counter()
    # 1º por (source='espn', source_id).
    assert _resolve_fighter(conn, "4350812", "Ilia Topuria", exact, normalized, counts) == 101
    assert counts["fighters_by_source"] == 1
    # 2º fallback fuzzy 0.92: acento distinto, sin fila espn en fighters.
    assert _resolve_fighter(conn, "9999999", "Viviane Araújo", exact, normalized, counts) == 7
    assert counts["fighters_by_name"] == 1
    # Nombre lejano -> sin match (no se inventa identidad).
    assert _resolve_fighter(conn, "8888888", "Somebody Else", exact, normalized, counts) is None


def test_resolve_fighter_finds_the_id_wherever_the_row_keeps_it(fakedb):
    """🔴 EL CASO DE LA VELADA DEL 29-AGO, y no era un caso raro: era el normal.

    `_resolve_fighter` buscaba el id de ESPN SOLO en `(source='espn', source_id)`,
    y ahí solo lo llevan los luchadores nacidos en ESPN. La mayoría nació en
    ufcstats y lo lleva en la columna `espn_id`, puesta después por el
    enriquecimiento. Medido contra Neon el 29-ago-2026: de los **2.731** ids de
    ESPN de la base, la consulta vieja resolvía **188**. El resto —el 93 %— caía
    al emparejador de nombres.

    Y el emparejador exige 0.92 de parecido, así que se rompe en cuanto ESPN
    escribe el nombre de otra forma. Xiong Jingnan (fighters.id 9086) iba a pelear
    esa noche contra Julia Polastri, ESPN la publica como «Jingnan Xiong» —con el
    orden invertido, que en un nombre chino es lo normal— y
    ratio('jingnan xiong', 'xiong jingnan') = **0.538**. Su combate se habría
    quedado sin resolver: sin resultado en vivo y sin película minuto a minuto,
    que es lo único que no se recupera al día siguiente.

    El arreglo no fue código nuevo: `get_fighter_id_by_espn_id` ya existía y ya la
    usaba `link_upcoming_fighters` desde el duplicado de Michael Page del 2-ago.
    Este pase, simplemente, no la usaba.
    """
    conn = fakedb.Connection(_responder)
    # Nombre con el orden invertido a propósito: si el id no lo resuelve, el
    # fuzzy tampoco va a salvarlo, que es justo lo que pasó.
    fighters = [FighterMatchRecord(9086, "Xiong Jingnan", None, None, None, None, None, None, None)]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    counts = Counter()

    # El id manda, venga la fila de donde venga.
    assert _resolve_fighter(conn, "4350812", "Jingnan Xiong", exact, normalized, counts) == 101
    assert counts["fighters_by_source"] == 1

    # Y sin id, el nombre invertido NO cuela: 0.538 está muy por debajo de 0.92.
    # Se prefiere no resolver a inventar una identidad.
    assert _resolve_fighter(conn, None, "Jingnan Xiong", exact, normalized, counts) is None
    assert counts["fighters_by_name"] == 0


def test_resolve_fighter_folds_diacritics_below_fuzzy_cutoff(fakedb):
    # Caso real UFC 329 (fight 12845): ESPN escribe "Adrian Yañez" (ñ) y la BD
    # tiene "Adrian Yanez" (n). ratio("adrian yañez","adrian yanez") = 0.9166,
    # JUSTO por debajo del 0.92, así que el fuzzy lo rechazaba y la pelea quedaba
    # sin resultado (13/14). El índice normalizado debe plegar acentos (fold).
    conn = fakedb.Connection(_responder)
    fighters = [
        FighterMatchRecord(7112, "Adrian Yanez", None, None, None, None, None, None, None)
    ]
    exact = _build_exact_name_index(fighters)
    normalized = _build_normalized_name_index(fighters)
    counts = Counter()
    assert (
        _resolve_fighter(conn, "9999999", "Adrian Yañez", exact, normalized, counts) == 7112
    )
    assert counts["fighters_by_name"] == 1
    # La ñ también se pliega en el índice EXACTO no acentuado del reverso: una
    # BD acentuada ("Yáñez") debe casar con un ESPN plano ("Yanez").
    fighters2 = [
        FighterMatchRecord(9001, "Nicolás Ñañez", None, None, None, None, None, None, None)
    ]
    exact2 = _build_exact_name_index(fighters2)
    normalized2 = _build_normalized_name_index(fighters2)
    counts2 = Counter()
    assert (
        _resolve_fighter(conn, "9999998", "Nicolas Nanez", exact2, normalized2, counts2)
        == 9001
    )


def test_match_db_event_uses_find_existing_event_id_tokens(fakedb):
    conn = fakedb.Connection(_responder)
    event = parse_scoreboard(SCOREBOARD)[0]
    # 'UFC 317: Topuria vs. Oliveira' comparte {317, topuria, oliveira} con la
    # fila de BD 'UFC 317: Topuria vs Oliveira' -> id 5.
    assert match_db_event(conn, event, promotion_id=1) == 5


def test_event_not_found_skips_without_writes(fakedb):
    def no_events(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT id, name, nickname"):
            return FIGHTER_ROWS
        return []

    conn = fakedb.Connection(no_events)
    counts = process_events(conn, parse_scoreboard(SCOREBOARD), promotion_id=1, dry_run=False)
    assert counts["events_unmatched"] == 1
    assert counts["events_matched"] == 0
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------------------------------- writes


def test_process_events_fills_results_by_espn_source(fakedb):
    conn = fakedb.Connection(_responder)
    counts = process_events(conn, parse_scoreboard(SCOREBOARD), promotion_id=1, dry_run=False)
    assert counts["events_matched"] == 1
    assert counts["fights_updated"] == 3      # 3 completadas
    assert counts["fights_pending"] == 1      # la que sigue en curso no se toca
    updates = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if sql.strip().upper().startswith("UPDATE")
    ]
    assert len(updates) == 3
    # KO/TKO del main event: ganador 101 (Topuria), R1, sobre fight 11.
    # El scoreboard trae "2:27", que es el reloj de ESPN y va en CUENTA ATRÁS;
    # lo que se guarda es el TRANSCURRIDO, 5:00 - 2:27 = 2:33. Antes se escribía
    # el crudo, y por eso 8 de los 12 combates del 1062 salieron con la hora
    # invertida a producción (el estelar decía 2:17 habiendo sido 2:41).
    # Ver tests/test_end_time_clock.py.
    sql, params = updates[0]
    assert params[:5] == (101, "KO/TKO", 1, "2:33", 11)
    assert ("Submission" in updates[1][1]) and (103 in updates[1][1])
    assert ("Decision" in updates[2][1]) and (105 in updates[2][1])
    assert conn.commits == 1


def test_fill_fight_result_never_overwrites_ufcstats(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert fill_fight_result(conn, 11, 101, "KO/TKO", 1, "2:27") is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # COALESCE con el valor ALMACENADO ganando: ESPN nunca pisa a ufcstats.
    for column in ("winner_id", "method", "end_round", "end_time"):
        assert f"{column} = COALESCE({column}, %s)" in flat
    # Guard: sin nada que rellenar no hay churn de fila.
    assert "(winner_id IS NULL AND %s IS NOT NULL)" in flat
    assert params[:5] == (101, "KO/TKO", 1, "2:27", 11)


def test_process_events_dry_run_never_writes(fakedb):
    conn = fakedb.Connection(_responder)
    counts = process_events(conn, parse_scoreboard(SCOREBOARD), promotion_id=1, dry_run=True)
    assert counts["fights_updated"] == 3
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_draw_or_unscored_fight_writes_nothing(fakedb):
    draw = _competition(
        "401999999",
        _competitor("4350812", "Ilia Topuria", False),
        _competitor("2504169", "Charles Oliveira", False),
        "Unofficial Winner Decision", 3, "5:00",
    )
    scoreboard = {
        "events": [
            {
                "id": "1", "name": "UFC 317: Topuria vs. Oliveira",
                "date": "2025-06-28T23:00Z", "competitions": [draw],
            }
        ]
    }
    conn = fakedb.Connection(_responder)
    counts = process_events(conn, parse_scoreboard(scoreboard), promotion_id=1, dry_run=False)
    # Sin ganador señalado no se escribe método: un empate/NC lo decide ufcstats.
    assert counts["fights_no_winner"] == 1
    assert fakedb.mutating_statements(conn) == []


# --------------------------------------------------- bounded loop (T3-A)


def _patch_clock(monkeypatch, module, step_seconds):
    """time.monotonic avanza step_seconds por consulta; time.sleep no duerme."""
    clock = {"now": 0.0}
    sleeps = []

    def fake_monotonic():
        clock["now"] += step_seconds
        return clock["now"]

    def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(module.time, "sleep", fake_sleep)
    return sleeps


def test_run_bounded_loop_stops_at_deadline(monkeypatch):
    from src.scrapers import espn_live_results as module

    sleeps = _patch_clock(monkeypatch, module, step_seconds=60.0)
    calls = []

    def fake_refresh(dates=None, dry_run=False):
        calls.append((dates, dry_run))
        return Counter({"scoreboard_events": 1, "live_events": 1})

    monkeypatch.setattr(module, "refresh_live_results", fake_refresh)
    totals = module.run_bounded_loop(
        dates="20260711", dry_run=True, duration_minutes=5, interval_seconds=120
    )
    # Deadline en monotonic=360 (60 de arranque + 5*60); una consulta por
    # vuelta => ~4-5 pasadas, todas con los args intactos y agregadas.
    assert len(calls) >= 2
    assert all(call == ("20260711", True) for call in calls)
    assert sleeps and all(seconds == 120 for seconds in sleeps)
    assert totals["scoreboard_events"] == len(calls)


def test_run_bounded_loop_exits_early_on_empty_scoreboard(monkeypatch):
    from src.scrapers import espn_live_results as module

    sleeps = _patch_clock(monkeypatch, module, step_seconds=60.0)
    calls = []

    def fake_refresh(dates=None, dry_run=False):
        calls.append(1)
        return Counter({"scoreboard_events": 0})

    monkeypatch.setattr(module, "refresh_live_results", fake_refresh)
    module.run_bounded_loop(
        dates=None, dry_run=False, duration_minutes=235, interval_seconds=120
    )
    # Guard: sin eventos en la ventana, una sola pasada y sin dormir.
    assert len(calls) == 1
    assert sleeps == []


def test_run_bounded_loop_survives_iteration_errors(monkeypatch):
    from src.scrapers import espn_live_results as module

    _patch_clock(monkeypatch, module, step_seconds=60.0)
    calls = []

    def flaky_refresh(dates=None, dry_run=False):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("ESPN hiccup")
        return Counter({"scoreboard_events": 1, "live_events": 1})

    monkeypatch.setattr(module, "refresh_live_results", flaky_refresh)
    totals = module.run_bounded_loop(
        dates=None, dry_run=False, duration_minutes=5, interval_seconds=120
    )
    # El fallo de la segunda pasada no tumba la ventana: se sigue iterando.
    assert len(calls) >= 3
    assert totals["scoreboard_events"] == len(calls) - 1


# --------------------------------------- el latido propio del bucle (026)
#
# EL PROBLEMA QUE RESUELVE: el bucle del directo y el cron de respaldo son EL
# MISMO PROGRAMA. Lo unico que los distingue es `--duration-minutes`, que es lo
# que hace entrar en run_bounded_loop. Los dos escriben en live_fight_stats, asi
# que el "pulso" que mira el panel no puede demostrar cual de los dos vive: con
# el bucle muerto y el respaldo escribiendo sus 2-6 muestras por hora, el panel
# aguanta en verde hasta 60 min.
#
# Por eso el latido va DENTRO de run_bounded_loop y solo ahi. El tercer test de
# este bloque es el que da todo el valor al arreglo: si alguien mueve la llamada
# a refresh_live_results —la ruta que SI comparten— el respaldo tambien latiria
# y esto valdria cero PARECIENDO que funciona.


def _bucle_de_prueba(monkeypatch, *, pasadas_ok=True, paso_reloj=60.0):
    from src.scrapers import espn_live_results as module

    _patch_clock(monkeypatch, module, step_seconds=paso_reloj)
    latidos = []
    monkeypatch.setattr(module, "escribir_latido_del_bucle", lambda d: latidos.append(d))

    def fake_refresh(dates=None, dry_run=False):
        if not pasadas_ok:
            raise RuntimeError("ESPN caido")
        return Counter({"scoreboard_events": 1, "live_events": 1})

    monkeypatch.setattr(module, "refresh_live_results", fake_refresh)
    return module, latidos


def test_el_bucle_deja_su_propio_latido_en_cada_pasada(monkeypatch):
    module, latidos = _bucle_de_prueba(monkeypatch)
    module.run_bounded_loop(dates=None, dry_run=False, duration_minutes=5, interval_seconds=20)
    assert latidos, "el bucle no dejo ni un latido: el panel no puede demostrar que vive"
    assert all("live-event-loop" in d for d in latidos)


def test_el_latido_no_abre_una_conexion_por_pasada(monkeypatch):
    """A 20 s de intervalo serian 705 conexiones a Neon por noche solo para
    esto. El acelerador de 60 s las baja a ~235, y contra el umbral de 10 min
    del panel deja 10 escrituras de margen.

    El reloj falso avanza 1 s por consulta (dos consultas por vuelta), asi que
    hacen falta ~30 pasadas para juntar los 60 s del acelerador. La cuenta que
    importa es la relacion: sin acelerador saldria un latido POR PASADA."""
    from src.scrapers import espn_live_results as module

    _patch_clock(monkeypatch, module, step_seconds=1.0)
    latidos, pasadas = [], []
    monkeypatch.setattr(module, "escribir_latido_del_bucle", lambda d: latidos.append(d))
    monkeypatch.setattr(
        module, "refresh_live_results",
        lambda dates=None, dry_run=False: (pasadas.append(1), Counter({"scoreboard_events": 1}))[1],
    )
    module.run_bounded_loop(dates=None, dry_run=False, duration_minutes=5, interval_seconds=20)

    assert len(pasadas) > 30, f"solo {len(pasadas)} pasadas: el test no mide nada"
    assert 0 < len(latidos) <= len(pasadas) / 10, (
        f"{len(latidos)} latidos en {len(pasadas)} pasadas: el acelerador no frena. "
        "Una noche entera serian ~705 conexiones a Neon solo para latir."
    )


def test_el_cron_de_respaldo_no_late_jamas(monkeypatch):
    """EL TEST QUE DA TODO EL VALOR AL ARREGLO.

    `live-results.yml` ejecuta este MISMO modulo sin --duration-minutes, o sea
    por refresh_live_results directamente, sin pasar por run_bounded_loop. Si el
    latido se moviera a la ruta compartida, el respaldo lo escribiria y "el
    bucle esta vivo" volveria a ser indemostrable — pero el panel se veria
    igual de verde, que es el peor final posible."""
    from src.scrapers import espn_live_results as module
    from src.scrapers.config import Settings

    latidos = []
    monkeypatch.setattr(module, "escribir_latido_del_bucle", lambda d: latidos.append(d))
    monkeypatch.setattr(
        module, "process_events", lambda *a, **k: Counter({"events_matched": 1})
    )
    monkeypatch.setattr(module, "fetch_scoreboard", lambda session, dates: {"events": []})
    monkeypatch.setattr(module, "build_espn_session", lambda: object())
    # 🪤 get_settings tambien, o este test solo pasa donde haya un .env con
    # DATABASE_URL. En CI no lo hay y reventaba con RuntimeError — el megatest
    # local NO caza esto, porque en local el .env existe. Con el scoreboard
    # vacio la guarda barata de refresh_live_results sale antes de conectar,
    # asi que la URL falsa no se usa nunca.
    monkeypatch.setattr(
        module, "get_settings",
        lambda: Settings(database_url="postgresql://falsa/no-se-usa", anthropic_api_key=None),
    )

    module.refresh_live_results(dates="20260815", dry_run=True)
    assert latidos == [], (
        "el cron de respaldo ha escrito el latido del BUCLE. El arreglo vale "
        "cero y encima lo parece todo lo contrario."
    )


def test_escribir_el_latido_se_traga_cualquier_fallo(monkeypatch):
    """LA PRIMERA RED. El latido corre DENTRO del bucle que graba la velada, asi
    que el codigo que existe para demostrar que el bucle vive seria lo ultimo
    que deberia matarlo.

    Se llama a la funcion REAL a proposito: el fixture autouse de conftest la
    tiene parcheada para toda la suite, asi que hay que pedirla explicitamente.
    Se rompe `connect`, que es el primer sitio real por el que pasa, y se fija
    `get_settings` para que el fallo sea el de connect en los dos entornos —sin
    eso, en CI (sin .env) reventaria antes en get_settings y el test aprobaria
    por un motivo distinto del que dice probar."""
    from src.scrapers import espn_live_results as module
    from src.scrapers.config import Settings

    def conexion_rota(url):
        raise RuntimeError("Neon dice que no")

    monkeypatch.setattr(
        module, "get_settings",
        lambda: Settings(database_url="postgresql://falsa/no-se-usa", anthropic_api_key=None),
    )
    monkeypatch.setattr(module, "connect", conexion_rota)

    # No levanta. Si algun dia alguien estrecha ese except, esto se pone rojo.
    module.escribir_latido_del_bucle("una prueba")


def test_un_latido_que_revienta_no_mata_la_grabacion(monkeypatch):
    """LA SEGUNDA RED, por si alguien estrecha la primera. Aunque el latido
    llegara a levantar una excepcion, la ventana sigue dando pasadas."""
    from src.scrapers import espn_live_results as module

    _patch_clock(monkeypatch, module, step_seconds=60.0)
    pasadas = []

    def latido_que_levanta(detalle):
        raise RuntimeError("y encima no se traga el fallo")

    monkeypatch.setattr(module, "escribir_latido_del_bucle", latido_que_levanta)
    monkeypatch.setattr(
        module, "refresh_live_results",
        lambda dates=None, dry_run=False: (pasadas.append(1), Counter({"scoreboard_events": 1}))[1],
    )
    module.run_bounded_loop(
        dates=None, dry_run=False, duration_minutes=5, interval_seconds=20
    )
    assert len(pasadas) >= 2, "la grabacion se paro por culpa del latido"


def test_en_ensayo_no_se_escribe_latido(monkeypatch):
    # Un dry-run no graba nada, asi que tampoco puede afirmar que el bucle de
    # verdad esta vivo.
    module, latidos = _bucle_de_prueba(monkeypatch)
    module.run_bounded_loop(dates=None, dry_run=True, duration_minutes=5, interval_seconds=20)
    assert latidos == []


# ------------------------------------- plan B del método: el leaf `status`
#
# details[] viene TOPADO A 10 entradas y la del "Unofficial Winner" se cae por
# abajo: medido el 15-ago-2026 sobre el evento ESPN 600059185, las 12 peleas
# traen exactamente 10 details y en 2 no hay entrada de método (401909737, el
# twister, y 401905373, una decisión). Esas 2 se sellaron con method NULL.
#
# Los payloads de abajo son los `result` REALES de esas peleas, copiados del
# leaf .../competitions/{id}/status (444-476 bytes contra los 165 KB del
# fightcenter). Los tres tokens que ESPN usó en las 12: submission, kotko y
# decision---unanimous.


def test_method_from_status_maps_the_three_real_espn_tokens():
    # Nombres tal cual los sirve ESPN, incluidos los tres guiones de la decisión.
    assert method_from_status(
        {"result": {"name": "submission", "displayName": "Submission", "description": "Twister"}}
    ) == "Submission"
    assert method_from_status(
        {"result": {"name": "kotko", "displayName": "KO/TKO", "description": "Punches"}}
    ) == "KO/TKO"
    assert method_from_status(
        {"result": {"name": "decision---unanimous", "displayName": "Decision - Unanimous"}}
    ) == "Decision"


def test_method_from_status_writes_nothing_before_the_fight():
    # Pre-evento REAL del 1086 (comp 401887543, 367 B): STATUS_SCHEDULED y sin
    # `result`. Es el guard que impide sellar un método antes de la campana.
    assert method_from_status({"type": {"name": "STATUS_SCHEDULED"}, "clock": 0.0}) is None
    assert method_from_status({"result": {}}) is None
    assert method_from_status({}) is None
    assert method_from_status(None) is None


def test_method_from_status_ignores_tokens_it_does_not_know():
    # Una descalificación NO es un KO/TKO. Ante un token que no está en la lista
    # blanca se calla y lo deja para ufcstats, en vez de adivinar una familia.
    assert method_from_status({"result": {"name": "dq"}}) is None
    assert method_from_status({"result": {"name": "no-contest"}}) is None
    assert method_from_status({"result": {"name": "draw"}}) is None


def test_method_from_status_only_ever_writes_provisional_codes():
    """El contrato que impide el fallo caro.

    ufcstats solo puede corregir un método si vale NULL o uno de los tres
    códigos de ESPN_PROVISIONAL_METHODS (backfill_results.py:557). Si este
    camino escribiera el detalle ('SUB - Twister', 'U-DEC'), ese valor quedaría
    CONGELADO para siempre: el UPDATE de consolidación no lo tocaría y
    post_event_review tampoco lo vería. Este test es la valla.
    """
    tokens = [
        "submission", "kotko", "ko", "tko",
        "decision---unanimous", "decision---split", "decision---majority",
    ]
    for token in tokens:
        method = method_from_status({"result": {"name": token, "description": "Twister"}})
        assert method in ESPN_PROVISIONAL_METHODS, f"{token} -> {method!r} congelaría el dato"


class _FakeStatusSession:
    """Sirve el leaf `status` por competition_id y apunta lo que se le pide."""

    def __init__(self, by_comp):
        self.by_comp = by_comp
        self.requested = []

    def get(self, url, timeout=None, params=None):
        self.requested.append(url)
        for comp_id, payload in self.by_comp.items():
            if f"/competitions/{comp_id}/status" in url:
                return _FakeResponse(payload)
        return _FakeResponse(None, ok=False)


class _FakeResponse:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload


def _completed_fight_without_method():
    """La pelea del twister: completada, con ganador y con details[] vacío."""
    scoreboard = {
        "leagues": [{"slug": "ufc"}],
        "events": [
            {
                "id": "600059185",
                "name": "UFC 317: Topuria vs. Oliveira",
                "date": "2025-06-28T23:00Z",
                "competitions": [
                    _competition(
                        "401909737",
                        _competitor("4350812", "Ilia Topuria", True),
                        _competitor("2504169", "Charles Oliveira", False),
                        None, 3, "1:36",  # sin detail_text -> details[] vacío
                    )
                ],
            }
        ],
    }
    return parse_scoreboard(scoreboard)[0].fights[0]


def _process_one(fight, session, fakedb):
    from src.scrapers.espn_live_results import _process_fight

    conn = fakedb.Connection(_responder)
    counts: Counter = Counter()
    fighters = [FighterMatchRecord(*row) for row in FIGHTER_ROWS]
    _process_fight(
        conn, 5, fight,
        _build_exact_name_index(fighters), _build_normalized_name_index(fighters),
        counts, dry_run=False,
        status_session=session, event_espn_id="600059185",
    )
    return conn, counts


def test_completed_fight_without_details_falls_back_to_the_status_leaf(fakedb):
    fight = _completed_fight_without_method()
    assert fight.method is None, "el scoreboard no da método: es el caso que arreglamos"
    session = _FakeStatusSession(
        {"401909737": {"result": {"name": "submission", "description": "Twister"}}}
    )
    conn, counts = _process_one(fight, session, fakedb)

    assert len(session.requested) == 1
    updates = [
        params for cur in conn.cursors for sql, params in cur.executed
        if sql.strip().upper().startswith("UPDATE")
    ]
    assert len(updates) == 1
    # Se escribe el código GENÉRICO, que ufcstats podrá sustituir esa misma
    # noche por 'SUB - Twister'.
    assert "Submission" in updates[0]
    assert counts["fights_updated"] == 1


def test_the_status_leaf_is_never_requested_when_details_already_gave_the_method(fakedb):
    # El camino feliz: 10 de cada 12 peleas. Ni un byte de red de más.
    fight = parse_scoreboard(SCOREBOARD)[0].fights[0]
    assert fight.method == "KO/TKO"
    session = _FakeStatusSession({})
    _process_one(fight, session, fakedb)
    assert session.requested == []


def test_a_dead_status_leaf_leaves_the_fight_exactly_as_it_was(fakedb):
    # Si el leaf no contesta, la pelea se sella con method NULL igual que hoy:
    # el plan B solo puede añadir, nunca empeorar.
    fight = _completed_fight_without_method()
    conn, counts = _process_one(fight, _FakeStatusSession({}), fakedb)
    updates = [
        params for cur in conn.cursors for sql, params in cur.executed
        if sql.strip().upper().startswith("UPDATE")
    ]
    assert len(updates) == 1
    assert None in updates[0]
    assert counts["fights_updated"] == 1


def test_recovering_a_decision_also_recovers_its_exact_end_time(fakedb):
    """Regalo del plan B: la decisión deja de pasar por la heurística del reloj.

    elapsed_end_time devuelve 5:00 en cuanto el método empieza por 'decision'
    (un asalto de decisión se agota por definición). Hoy la 401905373 llega sin
    método, así que su hora sale de comparar hipótesis contra el último reloj
    visto. Con el método recuperado, sale exacta.
    """
    scoreboard = {
        "leagues": [{"slug": "ufc"}],
        "events": [{
            "id": "600059185", "name": "UFC 317: Topuria vs. Oliveira",
            "date": "2025-06-28T23:00Z",
            "competitions": [_competition(
                "401905373",
                _competitor("4350812", "Ilia Topuria", True),
                _competitor("2504169", "Charles Oliveira", False),
                None, 3, "0:00",  # reloj agotado y sin details[]
            )],
        }],
    }
    fight = parse_scoreboard(scoreboard)[0].fights[0]
    session = _FakeStatusSession(
        {"401905373": {"result": {"name": "decision---unanimous"}}}
    )
    conn, _ = _process_one(fight, session, fakedb)
    params = [
        p for cur in conn.cursors for sql, p in cur.executed
        if sql.strip().upper().startswith("UPDATE")
    ][0]
    assert "Decision" in params
    assert "5:00" in params


def test_without_a_session_the_plan_b_is_a_no_op(fakedb):
    # Las llamadas antiguas (tests, invocaciones sin stats) se comportan igual
    # que antes: sin sesión no hay plan B y nada revienta.
    from src.scrapers.espn_live_results import _process_fight

    fight = _completed_fight_without_method()
    conn = fakedb.Connection(_responder)
    counts: Counter = Counter()
    fighters = [FighterMatchRecord(*row) for row in FIGHTER_ROWS]
    _process_fight(
        conn, 5, fight,
        _build_exact_name_index(fighters), _build_normalized_name_index(fighters),
        counts, dry_run=False,
    )
    assert counts["fights_updated"] == 1
