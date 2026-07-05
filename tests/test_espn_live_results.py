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
    if flat.startswith("SELECT id FROM events"):  # get_event_id (exact) -> miss
        return []
    if flat.startswith("SELECT id, name FROM events"):  # token fallback -> hit
        return [(5, "UFC 317: Topuria vs Oliveira")]
    if "FROM fights WHERE event_id" in flat:  # find_fight_id_by_fighters
        return [(11,)]
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
    # KO/TKO del main event: ganador 101 (Topuria), R1 2:27, sobre fight 11.
    sql, params = updates[0]
    assert params[:5] == (101, "KO/TKO", 1, "2:27", 11)
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
