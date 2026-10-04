"""Manual record corrections, and the guards that keep them from rotting.

THE BUG THIS FIXES. Imanol Rodriguez (fighters.id 7150, ESPN 5289578) is 8-0-0 on
ufc.com and UFCStats, but ESPN's "overall" record says 8-1-0 because it counts the
TUF 33 semifinal (2-ago-2025, split decision loss to Joseph Morales), which is an
exhibition. refresh_fighter_records copies ESPN overall every day (cron --days 14
and the --days 3 step of the live loop), so fixing the row by hand did nothing: the
next run wrote 8-1-0 back.

WHAT IS PROTECTED HERE:
- The correction is ANCHORED to the ESPN competition, not to a frozen record: while
  ESPN still lists that competition with that result, it is subtracted from the
  overall. After Imanol's next fight (ESPN 9-1-0) the stored record becomes 9-0-0.
- It retires itself loudly: once ESPN drops the competition, changes its result or
  stops counting it in overall, nothing is written for that fighter and the run
  exits non-zero, so notify-on-failure opens an Issue to delete the line.
- A corrected fighter never goes through the name fallback (it would find 5289578
  again and _name_change_is_safe((8,0,0), (8,1,0)) is True).
- It is NOT a general "drop every TUF bout" rule: Joseph Morales is 15-3 on ufc.com,
  UFCStats and ESPN, and such a rule would wrongly make him 14-3.

No socket, no network: SQL goes to the fakedb recorder and every ESPN call is an
injected function.
"""

import logging
import sys
from datetime import date

import pytest

from src.scrapers import refresh_fighter_records as rfr
from src.scrapers.record_correcciones import (
    APLICAR,
    CORRECCIONES,
    SIN_VERIFICAR,
    YA_NO_NECESARIA,
    CorreccionRecord,
    correcciones_por_luchador,
    evaluar_correcciones,
)
from src.scrapers.repositories.fighters import set_fighter_record_corrected

IMANOL_ID = 7150
IMANOL_ESPN = "5289578"
TUF_COMPETITION = "401811187"
TUF_UID = "s:3301~l:3359~e:600055505~c:401811187"


def _correction(**overrides) -> CorreccionRecord:
    base = dict(
        fighter_id=IMANOL_ID,
        espn_id=IMANOL_ESPN,
        competicion_espn=TUF_COMPETITION,
        resultado="L",
        motivo="test",
        fuente="https://example.test/athlete",
        desde=date(2026, 10, 4),
    )
    base.update(overrides)
    return CorreccionRecord(**base)


def _entry(result: str, *, name: str = "Regional FC", token: str = "kotko") -> dict:
    return {"name": name, "gameResult": result, "status": {"result": {"name": token}}}


def _career(wins: int, losses: int = 0, draws: int = 0, *, tuf: str | None = "L") -> dict:
    """A common/v3 athlete payload whose eventsMap tallies wins-losses-draws plus,
    when `tuf` is given, the TUF 33 semifinal with that result."""
    events_map: dict[str, dict] = {}
    for kind, count in (("W", wins), ("L", losses), ("D", draws)):
        for i in range(count):
            uid = f"s:3301~l:3359~e:7{'WLD'.index(kind)}{i:04d}~c:8{'WLD'.index(kind)}{i:04d}"
            events_map[uid] = _entry(kind)
    if tuf is not None:
        events_map[TUF_UID] = _entry(
            tuf,
            name="The Ultimate Fighter 33 Semifinal: Cormier vs. Sonnen",
            token="decision---split",
        )
    return {"events": list(events_map), "eventsMap": events_map}


# ------------------------------------------------------------ evaluar_correcciones


def test_evaluate_subtracts_the_competition_while_espn_still_counts_it():
    verdict = evaluar_correcciones([_correction()], (8, 1, 0), _career(8, tuf="L"))
    assert verdict.estado == APLICAR
    assert verdict.record == (8, 0, 0)


@pytest.mark.parametrize(
    "resultado, overall, expected",
    [("W", (9, 0, 0), (8, 0, 0)), ("D", (8, 0, 1), (8, 0, 0)), ("L", (8, 1, 0), (8, 0, 0))],
)
def test_evaluate_subtracts_the_component_of_the_result(resultado, overall, expected):
    wins = overall[0] - (1 if resultado == "W" else 0)
    payload = _career(wins, 0, 0, tuf=resultado)
    verdict = evaluar_correcciones([_correction(resultado=resultado)], overall, payload)
    assert verdict.estado == APLICAR
    assert verdict.record == expected


def test_evaluate_retires_when_the_competition_left_the_events_map():
    verdict = evaluar_correcciones([_correction()], (8, 1, 0), _career(8, 1, tuf=None))
    assert verdict.estado == YA_NO_NECESARIA
    assert TUF_COMPETITION in verdict.detalle


@pytest.mark.parametrize("token, game_result", [("decision---split", "W"), ("no-contest", "L")])
def test_evaluate_retires_when_espn_changed_the_result(token, game_result):
    payload = _career(8, tuf="L")
    payload["eventsMap"][TUF_UID] = _entry(game_result, token=token)
    verdict = evaluar_correcciones([_correction()], (8, 1, 0), payload)
    assert verdict.estado == YA_NO_NECESARIA


def test_evaluate_retires_when_espn_overall_already_excludes_it():
    """ESPN fixed the overall but left the bout in the eventsMap: subtracting again
    would erase a bout that is no longer counted."""
    verdict = evaluar_correcciones([_correction()], (8, 0, 0), _career(8, tuf="L"))
    assert verdict.estado == YA_NO_NECESARIA


@pytest.mark.parametrize("payload", [None, {}, {"eventsMap": {}}, {"eventsMap": "broken"}])
def test_evaluate_is_unverified_without_an_events_map(payload):
    verdict = evaluar_correcciones([_correction()], (8, 1, 0), payload)
    assert verdict.estado == SIN_VERIFICAR
    assert verdict.record is None


def test_evaluate_is_unverified_when_overall_and_events_map_disagree():
    """The overall lags the eventsMap (or the other way round): we cannot tell
    whether the overall counts the competition, so nothing is applied."""
    verdict = evaluar_correcciones([_correction()], (8, 1, 0), _career(9, tuf="L"))
    assert verdict.estado == SIN_VERIFICAR


def test_evaluate_tally_ignores_scheduled_and_no_contest_entries():
    payload = _career(8, tuf="L")
    payload["eventsMap"]["s:3301~l:3321~e:1~c:1"] = {"name": "UFC 340", "gameResult": ""}
    payload["eventsMap"]["s:3301~l:3359~e:2~c:2"] = _entry("L", token="no-contest")
    verdict = evaluar_correcciones([_correction()], (8, 1, 0), payload)
    assert verdict.estado == APLICAR
    assert verdict.record == (8, 0, 0)


def test_corrections_are_grouped_by_fighter():
    a = _correction()
    b = _correction(competicion_espn="1", resultado="W")
    c = _correction(fighter_id=1, espn_id="9")
    grouped = correcciones_por_luchador([a, b, c])
    assert grouped == {IMANOL_ID: [a, b], 1: [c]}


# -------------------------------------------------- set_fighter_record_corrected


def test_corrected_writer_sql_bounds_each_component(fakedb):
    """The corrected writer may lower the stored record only by the bouts the
    correction removes, COMPONENT BY COMPONENT: stored_c <= new_c + removed_c for
    wins, losses and draws. A total-only bound (stored total <= new total +
    removed) let 9-0-0 -> 8-0-0 through, erasing a real win; and it is this SQL
    guard, not the planner, that closes the race with a concurrent run that wrote
    9-0-0 between the fetch phase and the write phase."""
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert set_fighter_record_corrected(
        conn, IMANOL_ID, wins=8, losses=0, draws=0, removed=(0, 1, 0)
    ) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "UPDATE fighters" in flat
    assert "IS DISTINCT FROM (%s, %s, %s)" in flat
    for column in ("wins", "losses", "draws"):
        assert f"AND COALESCE({column}, 0) <= %s + %s" in flat
    # The old total-only bound is gone (it was the hole).
    assert "<= (%s + %s + %s + %s)" not in flat
    assert params == (
        8, 0, 0, IMANOL_ID,   # SET ... WHERE id
        8, 0, 0,              # IS DISTINCT FROM
        8, 0,                 # wins   <= new wins   + removed wins
        0, 1,                 # losses <= new losses + removed losses
        0, 0,                 # draws  <= new draws  + removed draws
    )


def test_corrected_writer_reports_no_update_when_guard_rejects(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert set_fighter_record_corrected(
        conn, IMANOL_ID, wins=8, losses=0, draws=0, removed=(0, 1, 0)
    ) is False


@pytest.mark.parametrize(
    "wins, losses, draws, removed",
    [
        (-1, 0, 0, (0, 1, 0)),
        (8, -1, 0, (0, 1, 0)),
        (8, 0, 0, (0, -1, 0)),
        (8, 0, 0, (-1, 1, 0)),
        (8, 0, 0, (0, 1)),
    ],
)
def test_corrected_writer_rejects_bad_input_without_touching_db(fakedb, wins, losses, draws, removed):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert set_fighter_record_corrected(
        conn, IMANOL_ID, wins=wins, losses=losses, draws=draws, removed=removed
    ) is False
    assert fakedb.mutating_statements(conn) == []


# ---------------------------------------------------------- refresh_records wiring


def _responder(targets):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "FROM fighters f" in flat:
            return list(targets)
        if "UPDATE fighters" in flat:
            return [(1,)]
        return []

    return responder


def _updates(conn):
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "UPDATE fighters" in sql
    ]


def _run(conn, *, records, career, correcciones=None, fetch_by_name=None, dry_run=False):
    """refresh_records with everything injected. `records` maps espn_id -> overall;
    `career` maps espn_id -> eventsMap payload (or an Exception to raise)."""
    events_calls: list[str] = []

    def fetch_events(espn_id):
        events_calls.append(espn_id)
        value = career[espn_id]
        if isinstance(value, Exception):
            raise value
        return value

    counts = rfr.refresh_records(
        connection=conn,
        fetch_record=lambda espn_id: records.get(espn_id),
        fetch_by_name=fetch_by_name,
        fetch_events=fetch_events,
        correcciones=[_correction()] if correcciones is None else correcciones,
        days=14,
        delay=0,
        dry_run=dry_run,
    )
    return counts, events_calls


def test_correction_is_applied_before_the_unchanged_shortcut(fakedb):
    """TODAY'S CASE: stored 8-1-0 == ESPN 8-1-0. A check placed after the
    'rec == stored -> unchanged' shortcut never fires; this one must."""
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 1, 0)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: (8, 1, 0)}, career={IMANOL_ESPN: _career(8)})

    assert counts["correcciones_aplicadas"] == 1
    assert counts["updated"] == 1
    assert counts["unchanged"] == 0
    updates = _updates(conn)
    assert len(updates) == 1
    flat, params = updates[0]
    # Fewer bouts than stored (8 < 9): the monotonic bump cannot write it, so the
    # dedicated corrected writer does.
    assert "IS DISTINCT FROM" in flat
    assert params == (8, 0, 0, IMANOL_ID, 8, 0, 0, 8, 0, 0, 1, 0, 0)
    assert conn.commits == 1


def test_corrected_fighter_never_falls_back_to_name_when_id_fetch_fails(fakedb):
    """Live-tested trap: by-id returns None, the name search finds 5289578 again
    and _name_change_is_safe((8,0,0), (8,1,0), 4) is True -> it would bump back."""
    assert rfr._name_change_is_safe((8, 0, 0), (8, 1, 0), 4) is True
    name_calls: list[str] = []

    def fetch_by_name(name):
        name_calls.append(name)
        return (8, 1, 0)

    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 0, 0)]))
    counts, events_calls = _run(
        conn, records={IMANOL_ESPN: None}, career={IMANOL_ESPN: _career(8)},
        fetch_by_name=fetch_by_name,
    )

    assert name_calls == []
    assert events_calls == []
    assert counts["unresolved"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_corrected_fighter_is_fetched_by_the_corrected_espn_id_even_without_one_stored(fakedb):
    name_calls: list[str] = []
    record_calls: list[str] = []

    def fetch_record(espn_id):
        record_calls.append(espn_id)
        return (8, 1, 0)

    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", None, 8, 1, 0)]))
    counts = rfr.refresh_records(
        connection=conn,
        fetch_record=fetch_record,
        fetch_by_name=lambda name: name_calls.append(name) or (8, 1, 0),
        fetch_events=lambda espn_id: _career(8),
        correcciones=[_correction()],
        days=14, delay=0,
    )

    assert record_calls == [IMANOL_ESPN]
    assert name_calls == []
    assert counts["correcciones_aplicadas"] == 1
    assert counts["resolved_by_name"] == 0
    assert _updates(conn)[0][1][:4] == (8, 0, 0, IMANOL_ID)


def test_once_corrected_the_record_is_left_alone(fakedb):
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 0, 0)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: (8, 1, 0)}, career={IMANOL_ESPN: _career(8)})

    assert counts["correcciones_aplicadas"] == 1
    assert counts["unchanged"] == 1
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_next_fight_writes_the_corrected_record_through_the_monotonic_bump(fakedb):
    """ESPN 9-1-0 after his next win, competition still there -> 9-0-0, written by
    the normal bump (9 > 8), not by the corrected writer."""
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 0, 0)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: (9, 1, 0)}, career={IMANOL_ESPN: _career(9)})

    assert counts["correcciones_aplicadas"] == 1
    assert counts["updated"] == 1
    updates = _updates(conn)
    assert len(updates) == 1
    flat, params = updates[0]
    assert "IS DISTINCT FROM" not in flat
    assert "(%s + %s + %s) > (COALESCE(wins, 0)" in flat
    assert params == (9, 0, 0, IMANOL_ID, 9, 0, 0)


def test_next_fight_from_the_wrong_record_still_lands_on_the_corrected_one(fakedb):
    """Stored 7-1-0 (written before the fix), ESPN now 8-1-0: same total, different
    split. The corrected 8-0-0 must win, and it does not lose a single bout that the
    correction does not explain."""
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 7, 1, 0)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: (8, 1, 0)}, career={IMANOL_ESPN: _career(8)})

    assert counts["updated"] == 1
    assert _updates(conn)[0][1] == (8, 0, 0, IMANOL_ID, 8, 0, 0, 8, 0, 0, 1, 0, 0)


def test_a_regressing_espn_read_is_never_written_for_a_corrected_fighter(fakedb):
    """The corrected writer is not monotonic, so it must not become the door for a
    bad ESPN read: 5-1-0 (6 bouts) against a stored 8-0-0 is a regression."""
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 0, 0)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: (5, 1, 0)}, career={IMANOL_ESPN: _career(5)})

    assert counts["not_greater_skipped"] == 1
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


@pytest.mark.parametrize(
    "stored, overall, career_wins",
    [
        # After his next win is stored (9-0-0), both ESPN endpoints serve the
        # pre-fight 8-1-0 (stale cache). Corrected 8-0-0 has 8 bouts vs 9 stored,
        # and the total-only check let it erase the win.
        ((9, 0, 0), (8, 1, 0), 8),
        # A coherent read one win short of what is stored: 7-1-0 -> 7-0-0 would
        # drop a real win, not the TUF loss.
        ((8, 0, 0), (7, 1, 0), 7),
    ],
    ids=["stale-read-after-the-next-win", "one-real-win-short"],
)
def test_the_corrected_writer_never_drops_a_real_bout(fakedb, stored, overall, career_wins):
    """Only the corrected bouts may disappear, component by component. The same
    read is refused for a fighter WITHOUT a correction (monotonic bump), so the
    correction must not open a door that every other fighter keeps shut."""
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, *stored)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: overall}, career={IMANOL_ESPN: _career(career_wins)})

    assert counts["updated"] == 0
    assert counts["not_greater_skipped"] == 1
    assert fakedb.mutating_statements(conn) == []

    # Control: the same stale read against an uncorrected fighter is not written.
    control = fakedb.Connection(_responder([(1, "Someone Else", "1", stored[0], 1, 0)]))
    control_counts, _ = _run(
        control, records={"1": overall}, career={}, correcciones=[],
    )
    assert control_counts["updated"] == 0
    assert fakedb.mutating_statements(control) == []


def test_competition_gone_writes_nothing_and_asks_to_retire_the_line(fakedb):
    name_calls: list[str] = []
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 0, 0)]))
    counts, _ = _run(
        conn, records={IMANOL_ESPN: (9, 1, 0)}, career={IMANOL_ESPN: _career(9, 1, tuf=None)},
        fetch_by_name=lambda name: name_calls.append(name) or (9, 1, 0),
    )

    assert counts["correcciones_ya_no_necesarias"] == 1
    assert counts["correcciones_aplicadas"] == 0
    assert counts["updated"] == 0
    assert name_calls == []
    assert fakedb.mutating_statements(conn) == []


@pytest.mark.parametrize("career", [None, RuntimeError("ESPN 503")])
def test_events_map_failure_writes_nothing(fakedb, career):
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 1, 0)]))
    counts, _ = _run(conn, records={IMANOL_ESPN: (8, 1, 0)}, career={IMANOL_ESPN: career})

    assert counts["correcciones_sin_verificar"] == 1
    assert counts["correcciones_ya_no_necesarias"] == 0
    assert counts["updated"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_without_an_events_fetcher_a_corrected_fighter_is_not_verified(fakedb):
    """Injected fetch_record but no fetch_events (old-style callers/tests): no
    network, no write."""
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 1, 0)]))
    counts = rfr.refresh_records(
        connection=conn, fetch_record=lambda espn_id: (8, 1, 0),
        correcciones=[_correction()], days=14, delay=0,
    )
    assert counts["correcciones_sin_verificar"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_morales_without_a_correction_is_untouched(fakedb):
    """Joseph Morales is 15-3 on ufc.com, UFCStats AND ESPN, and also fought that
    TUF semifinal (he won it). Only the fighter named in a correction is touched:
    his eventsMap is not even fetched."""
    targets = [
        (6288, "Joseph Morales", "4238229", 15, 3, 0),
        (IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 1, 0),
    ]
    conn = fakedb.Connection(_responder(targets))
    counts, events_calls = _run(
        conn,
        records={"4238229": (15, 3, 0), IMANOL_ESPN: (8, 1, 0)},
        career={"4238229": _career(14, 3, tuf="W"), IMANOL_ESPN: _career(8)},
    )

    assert events_calls == [IMANOL_ESPN]
    assert counts["unchanged"] == 1           # Morales
    assert counts["updated"] == 1             # Imanol only
    assert [p[3] for _, p in _updates(conn)] == [IMANOL_ID]


def test_dry_run_reports_the_correction_and_writes_nothing(fakedb, caplog):
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 1, 0)]))
    with caplog.at_level(logging.INFO, logger=rfr.LOGGER.name):
        counts, _ = _run(
            conn, records={IMANOL_ESPN: (8, 1, 0)}, career={IMANOL_ESPN: _career(8)},
            dry_run=True,
        )

    assert counts["updated"] == 1
    assert counts["correcciones_aplicadas"] == 1
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    planned = [r.getMessage() for r in caplog.records if "[dry-run]" in r.getMessage()]
    assert len(planned) == 1
    assert "Imanol Rodriguez: 8-1-0 -> 8-0-0" in planned[0]
    assert TUF_COMPETITION in planned[0]


def test_the_counters_are_always_in_the_summary(fakedb):
    conn = fakedb.Connection(_responder([]))
    counts = rfr.refresh_records(connection=conn, fetch_record=lambda espn_id: None, delay=0)
    for key in ("correcciones_aplicadas", "correcciones_ya_no_necesarias", "correcciones_sin_verificar"):
        assert counts[key] == 0


def test_the_repo_correction_fixes_imanol_with_the_real_espn_shape(fakedb):
    """The check that matters: the correction that is IN THE FILE, against the
    eventsMap ESPN serves today (uids and results as read on 4-oct-2026)."""
    real_events = {
        "s:3301~l:3321~e:600061182~c:401912276": _entry("W", name="UFC 332: Silva vs. Wang"),
        "s:3301~l:3321~e:600057330~c:401847040": _entry("W", name="UFC Fight Night: Moreno vs. Kavanagh"),
        "s:3301~l:3321~e:600055054~c:401828741": _entry("W", name="Dana White's Contender Series: Season 9, Week 9"),
        TUF_UID: _entry("L", name="The Ultimate Fighter 33 Semifinal: Cormier vs. Sonnen", token="decision---split"),
        "s:3301~l:3359~e:600050286~c:401777785": _entry("W", name="Fury Fighting Championship 96"),
        "s:3301~l:3359~e:600048672~c:401777786": _entry("W", name="Fury Fighting Championship 93"),
        "s:3301~l:3359~e:600054395~c:401777784": _entry("W", name="Combate Global: USA vs. Ecuador", token="submission-rear-naked-choke"),
        "s:3301~l:3359~e:600054394~c:401777783": _entry("W", name="Budo Sento Championship 15"),
        "s:3301~l:3359~e:600054393~c:401777782": _entry("W", name="Budo Sento Championship 11"),
    }
    payload = {"events": list(real_events), "eventsMap": real_events}
    conn = fakedb.Connection(_responder([(IMANOL_ID, "Imanol Rodriguez", IMANOL_ESPN, 8, 1, 0)]))

    counts = rfr.refresh_records(  # no `correcciones`: uses the ones in the repo
        connection=conn,
        fetch_record=lambda espn_id: (8, 1, 0) if espn_id == IMANOL_ESPN else None,
        fetch_events=lambda espn_id: payload if espn_id == IMANOL_ESPN else None,
        days=3, delay=0,
    )

    assert counts["correcciones_aplicadas"] == 1
    assert counts["updated"] == 1
    assert _updates(conn)[0][1][:4] == (8, 0, 0, IMANOL_ID)


# ------------------------------------------------------------------- exit code


def _main_with(monkeypatch, counts):
    monkeypatch.setattr(rfr, "refresh_records", lambda **kwargs: dict(counts))
    monkeypatch.setattr(sys, "argv", ["refresh_fighter_records", "--days", "14"])
    rfr.main()


def test_main_fails_red_when_a_correction_is_no_longer_needed(monkeypatch, capsys):
    """Red run -> notify-on-failure ('Refresh fighter records' is watched) opens
    an Issue to delete the line. The JSON summary still comes out first."""
    with pytest.raises(SystemExit) as exc:
        _main_with(monkeypatch, {"updated": 3, "correcciones_ya_no_necesarias": 1})
    assert exc.value.code == 1
    assert '"correcciones_ya_no_necesarias": 1' in capsys.readouterr().out


@pytest.mark.parametrize(
    "counts",
    [
        {"updated": 3, "correcciones_ya_no_necesarias": 0, "correcciones_sin_verificar": 0},
        # A transient ESPN failure must not open an Issue every morning.
        {"updated": 3, "correcciones_ya_no_necesarias": 0, "correcciones_sin_verificar": 1},
    ],
)
def test_main_stays_green_otherwise(monkeypatch, counts):
    _main_with(monkeypatch, counts)  # no SystemExit


# --------------------------------------------------------------------- hygiene


def test_repo_corrections_are_well_formed():
    assert CORRECCIONES, "the Imanol Rodriguez correction must be in the file"
    seen: set[tuple[int, str]] = set()
    espn_by_fighter: dict[int, str] = {}
    for c in CORRECCIONES:
        assert c.motivo.strip(), "every correction explains why it exists"
        assert c.fuente.startswith("http"), f"{c.fighter_id}: needs a citable source"
        assert c.resultado in ("W", "L", "D"), f"{c.fighter_id}: unknown result {c.resultado!r}"
        assert c.espn_id.isdigit() and c.competicion_espn.isdigit(), (
            f"{c.fighter_id}: ESPN ids are plain digits (no 'c:' prefix)"
        )
        assert c.desde <= date.today(), f"{c.fighter_id}: 'desde' is in the future"
        key = (c.fighter_id, c.competicion_espn)
        assert key not in seen, f"{key}: duplicated correction"
        seen.add(key)
        assert espn_by_fighter.setdefault(c.fighter_id, c.espn_id) == c.espn_id, (
            f"{c.fighter_id}: one fighter, one ESPN athlete"
        )


def test_the_imanol_line_is_the_verified_one():
    """fighters.id 7150 = ESPN 5289578, competition 401811187 is the TUF 33 semifinal
    he lost. A typo in any of the three silently disables the fix (it would retire
    itself as 'no longer needed' and stop the cron in red)."""
    (imanol,) = [c for c in CORRECCIONES if c.fighter_id == IMANOL_ID]
    assert (imanol.espn_id, imanol.competicion_espn, imanol.resultado) == (
        IMANOL_ESPN, TUF_COMPETITION, "L",
    )
    assert "ufc.com" in imanol.fuente
