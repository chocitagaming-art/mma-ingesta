"""Timeline del directo (migración 024): replay de capturas locales.

Mismo estilo que test_espn_live_stats: fakedb + ficheros mínimos con la forma
EXACTA que escribe scripts/capture_live_samples.py (scoreboard del site API y
hojas de statistics de la core API), sin red y sin BD real.

Contrato: el replay emite una muestra por poll y pelea cuando algo cambió
(dedup como el escritor en vivo), la serie empieza en el primer asalto (los
walkouts no cuentan), una cancelada descarta su serie entera, y --apply hace
replace (DELETE + INSERT con sampled_at = hora del fichero capturado).
"""

import json
from datetime import datetime, timezone

from src.scrapers.backfill_live_samples_from_capture import (
    group_capture_files,
    replay_capture,
)

# --------------------------------------------------------------- fixtures

ESPN_TO_DB = {"4686725": 201, "5074121": 202}


def _responder(sql, params=None):
    flat = " ".join(sql.split())
    if flat.startswith("SELECT id, name, nickname"):
        return [
            (201, "Cody Durden", None, None, None, None, None, None, None),
            (202, "Alessandro Costa", None, None, None, None, None, None, None),
        ]
    if "FROM fighters WHERE source" in flat:
        db_id = ESPN_TO_DB.get(params[1])
        return [(db_id,)] if db_id else []
    if flat.startswith("SELECT id FROM events"):
        return [(5,)]
    if "FROM fights WHERE event_id" in flat:
        return [(11,)]
    return []


def _board(state="in", period=1, clock="4:00", status="STATUS_IN_PROGRESS_2",
           completed=False):
    return {
        "events": [
            {
                "id": "600059148",
                "name": "UFC 329: McGregor vs. Holloway 2",
                "date": "2026-07-11T21:00Z",
                "competitions": [
                    {
                        "id": "401883599",
                        "competitors": [
                            {"id": "4686725", "winner": False,
                             "athlete": {"displayName": "Cody Durden"}},
                            {"id": "5074121", "winner": False,
                             "athlete": {"displayName": "Alessandro Costa"}},
                        ],
                        "status": {
                            "displayClock": clock, "period": period,
                            "type": {"name": status, "state": state,
                                     "completed": completed,
                                     "detail": status, "shortDetail": status},
                        },
                        "details": [],
                    }
                ],
            }
        ]
    }


def _stats_payload(landed, attempted):
    return {
        "splits": {
            "id": "0",
            "categories": [
                {
                    "name": "general",
                    "stats": [
                        {"name": "sigStrikesLanded", "value": float(landed)},
                        {"name": "sigStrikesAttempted", "value": float(attempted)},
                    ],
                }
            ],
        }
    }


def _write(dirpath, name, payload):
    (dirpath / name).write_text(json.dumps(payload), encoding="utf-8")


def _write_poll(dirpath, ts, board, red=None, blue=None):
    """Un poll de captura: scoreboard en <ts> y hojas de stats ~1 s después."""
    _write(dirpath, f"{ts}-scoreboard.json", board)
    later = ts[:-3] + f"{int(ts[-3:-1]) + 1:02d}Z"
    if red is not None:
        _write(dirpath, f"{later}-comp-401883599-stats-4686725.json", red)
    if blue is not None:
        _write(dirpath, f"{later}-comp-401883599-stats-5074121.json", blue)


# ------------------------------------------------------------------ tests


def test_group_capture_files_associates_stats_to_prior_scoreboard():
    polls = group_capture_files(
        [
            "20260711T210000Z-scoreboard.json",
            "20260711T210001Z-comp-401883599-stats-4686725.json",
            # Ruido que el replay ignora (fightcenter/status/situation/otros).
            "20260711T210001Z-fightcenter-600059148.json",
            "20260711T210001Z-comp-401883599-status.json",
            "20260711T210001Z-comp-401883599-situation.json",
            "20260711T210200Z-scoreboard.json",
            "20260711T210202Z-comp-401883599-stats-5074121.json",
            "notas.txt",
        ]
    )
    assert [p.sampled_at for p in polls] == [
        datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 11, 21, 2, 0, tzinfo=timezone.utc),
    ]
    assert polls[0].stats_names == {
        ("401883599", "4686725"): "20260711T210001Z-comp-401883599-stats-4686725.json"
    }
    assert polls[1].stats_names == {
        ("401883599", "5074121"): "20260711T210202Z-comp-401883599-stats-5074121.json"
    }


def _capture_with_full_fight(tmp_path):
    """R1 con stats -> poll idéntico (dedup) -> R2 con stats -> Final."""
    _write_poll(tmp_path, "20260711T210000Z", _board(period=1, clock="4:00"),
                red=_stats_payload(3, 16), blue=_stats_payload(2, 10))
    _write_poll(tmp_path, "20260711T210200Z", _board(period=1, clock="4:00"),
                red=_stats_payload(3, 16), blue=_stats_payload(2, 10))
    _write_poll(tmp_path, "20260711T210400Z", _board(period=2, clock="3:00"),
                red=_stats_payload(13, 38), blue=_stats_payload(8, 20))
    # Ya en post el capturador no pide hojas de stats: la muestra final lleva
    # el último snapshot fusionado conocido.
    _write_poll(
        tmp_path, "20260711T210600Z",
        _board(state="post", period=2, clock="2:19", status="STATUS_FINAL",
               completed=True),
    )


def test_replay_emits_series_with_dedup_and_final_post(fakedb, tmp_path):
    _capture_with_full_fight(tmp_path)
    conn = fakedb.Connection(_responder)
    counts = replay_capture(conn, tmp_path, promotion_id=1, apply=False)
    assert counts["polls"] == 4
    # R1 + R2 + Final; el poll idéntico dedupea.
    assert counts["samples_emitted"] == 3
    assert counts["fights_with_samples"] == 1
    # dry-run: ni DELETE ni INSERT.
    assert fakedb.mutating_statements(conn) == []


def test_replay_apply_replaces_and_stamps_capture_time(fakedb, tmp_path):
    _capture_with_full_fight(tmp_path)
    conn = fakedb.Connection(_responder)
    replay_capture(conn, tmp_path, promotion_id=1, apply=True)
    statements = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
    ]
    delete_positions = [
        i for i, (sql, _) in enumerate(statements)
        if "DELETE FROM live_fight_stat_samples" in sql
    ]
    insert_rows = [
        (i, params) for i, (sql, params) in enumerate(statements)
        if "INSERT INTO live_fight_stat_samples" in sql
    ]
    # Replace: un DELETE de la pelea ANTES de todos los INSERTs.
    assert len(delete_positions) == 1
    assert statements[delete_positions[0]][1] == (11,)
    assert all(i > delete_positions[0] for i, _ in insert_rows)
    assert len(insert_rows) == 3
    first, last = insert_rows[0][1], insert_rows[-1][1]
    # (fight_id, state, status_name, status_detail, period, clock, json, ts)
    assert first[0] == 11 and first[1] == "in" and first[4] == 1
    assert last[1] == "post" and last[4] == 2 and last[5] == "2:19"
    # sampled_at = hora del fichero capturado, no NOW().
    assert first[7] == datetime(2026, 7, 11, 21, 0, 0, tzinfo=timezone.utc)
    assert last[7] == datetime(2026, 7, 11, 21, 6, 0, tzinfo=timezone.utc)
    # El JSON fusionado lleva los DOS lados con claves = fighters.id nuestros.
    for _, params in insert_rows:
        stats = json.loads(params[6])
        assert set(stats) == {"201", "202"}
    # La muestra final conserva el último snapshot conocido (R2).
    assert json.loads(last[6])["201"] == {"ssl": 13, "ssa": 38}


def test_prefight_walkouts_not_sampled(fakedb, tmp_path):
    _write_poll(
        tmp_path, "20260711T205800Z",
        _board(period=0, clock="-", status="STATUS_FIGHTERS_WALKING"),
        red=_stats_payload(0, 0), blue=_stats_payload(0, 0),
    )
    conn = fakedb.Connection(_responder)
    counts = replay_capture(conn, tmp_path, promotion_id=1, apply=False)
    assert counts["samples_emitted"] == 0


def test_cancelled_fight_series_dropped_and_cleaned_in_db(fakedb, tmp_path):
    _write_poll(tmp_path, "20260711T210000Z", _board(period=1, clock="4:00"),
                red=_stats_payload(3, 16), blue=_stats_payload(2, 10))
    _write_poll(
        tmp_path, "20260711T210200Z",
        _board(state="post", period=1, clock="-", status="STATUS_CANCELED"),
    )
    conn = fakedb.Connection(_responder)
    counts = replay_capture(conn, tmp_path, promotion_id=1, apply=True)
    assert counts["samples_emitted"] == 0
    assert counts["samples_dropped_cancelled"] == 1
    # Además de descartar en memoria, con apply limpia la serie en BD: una
    # pasada previa (del backfill o del escritor) pudo dejarla huérfana.
    assert counts["cancelled_series_deleted"] == 1
    mutating = fakedb.mutating_statements(conn)
    assert any("DELETE FROM live_fight_stat_samples" in sql for sql in mutating)
    assert not any("INSERT" in sql for sql in mutating)


def test_suspension_skips_poll_but_keeps_series(fakedb, tmp_path):
    # Suspensión (reanudable) a mitad de captura: el poll no emite muestra
    # pero la serie NO se descarta — al reanudarse sigue acumulando.
    _write_poll(tmp_path, "20260711T210000Z", _board(period=1, clock="4:00"),
                red=_stats_payload(3, 16), blue=_stats_payload(2, 10))
    _write_poll(
        tmp_path, "20260711T210200Z",
        _board(state="in", period=1, clock="-", status="STATUS_SUSPENDED"),
    )
    _write_poll(tmp_path, "20260711T210400Z", _board(period=2, clock="3:00"),
                red=_stats_payload(13, 38), blue=_stats_payload(8, 20))
    conn = fakedb.Connection(_responder)
    counts = replay_capture(conn, tmp_path, promotion_id=1, apply=False)
    assert counts["samples_emitted"] == 2
    assert counts["samples_dropped_cancelled"] == 0


def test_replay_post_is_sticky(fakedb, tmp_path):
    # Un scoreboard rezagado ('in') tras el Final no devuelve la pelea a 'in'
    # (mismo CASE pegajoso del upsert del escritor real).
    _write_poll(tmp_path, "20260711T210000Z", _board(period=1, clock="4:00"),
                red=_stats_payload(3, 16), blue=_stats_payload(2, 10))
    _write_poll(
        tmp_path, "20260711T210200Z",
        _board(state="post", period=1, clock="2:47", status="STATUS_FINAL"),
    )
    _write_poll(tmp_path, "20260711T210400Z", _board(period=1, clock="2:22"),
                red=_stats_payload(5, 20), blue=_stats_payload(2, 10))
    conn = fakedb.Connection(_responder)
    replay_capture(conn, tmp_path, promotion_id=1, apply=True)
    inserts = [
        params for cur in conn.cursors for sql, params in cur.executed
        if "INSERT INTO live_fight_stat_samples" in sql
    ]
    # in -> post -> post (el rezagado emite porque cambian las stats, pero
    # conserva 'post').
    assert [p[1] for p in inserts] == ["in", "post", "post"]


def _responder_with_existing(n):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT COUNT(*) FROM live_fight_stat_samples"):
            return [(n,)]
        return _responder(sql, params)
    return responder


def test_apply_keeps_richer_existing_series_unless_forced(fakedb, tmp_path):
    _capture_with_full_fight(tmp_path)  # 3 muestras nuevas
    # La BD ya tiene 50 muestras (serie del escritor en vivo a 20 s).
    conn = fakedb.Connection(_responder_with_existing(50))
    counts = replay_capture(conn, tmp_path, promotion_id=1, apply=True)
    assert counts["fights_skipped_richer_series"] == 1
    mutating = fakedb.mutating_statements(conn)
    assert mutating == []  # ni DELETE ni INSERT: se conserva la serie rica
    # Con force=True sí reemplaza.
    conn2 = fakedb.Connection(_responder_with_existing(50))
    counts2 = replay_capture(conn2, tmp_path, promotion_id=1, apply=True, force=True)
    assert counts2.get("fights_skipped_richer_series", 0) == 0
    mutating2 = fakedb.mutating_statements(conn2)
    assert any("DELETE FROM live_fight_stat_samples" in sql for sql in mutating2)
    assert sum("INSERT INTO live_fight_stat_samples" in sql for sql in mutating2) == 3


def test_corrupt_capture_file_is_skipped(fakedb, tmp_path):
    _write_poll(tmp_path, "20260711T210000Z", _board(period=1, clock="4:00"),
                red=_stats_payload(3, 16), blue=_stats_payload(2, 10))
    # Scoreboard truncado (job de captura matado a mitad de write_text).
    (tmp_path / "20260711T210200Z-scoreboard.json").write_text(
        '{"events": [', encoding="utf-8"
    )
    conn = fakedb.Connection(_responder)
    counts = replay_capture(conn, tmp_path, promotion_id=1, apply=False)
    assert counts["files_corrupt"] == 1
    assert counts["samples_emitted"] == 1
