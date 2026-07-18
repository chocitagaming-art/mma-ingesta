"""Tests for the monotonic fighter-record refresh (fixes the frozen palmares).

No socket, no network: bump_fighter_record's SQL is asserted against the fakedb
recorder, and refresh_records runs with an injected fetch_record + fake conn.
"""
import json

from src.scrapers import refresh_fighter_records as rfr
from src.scrapers.repositories.fighters import bump_fighter_record

# --------------------------------------------------------- bump_fighter_record


def test_bump_updates_when_incoming_total_is_greater(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert bump_fighter_record(conn, 7, wins=18, losses=7, draws=0) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # Strictly-monotonic guard on the bout count lives in the WHERE clause.
    assert "UPDATE fighters" in flat
    assert "(%s + %s + %s) > (COALESCE(wins, 0) + COALESCE(losses, 0) + COALESCE(draws, 0))" in flat
    assert params == (18, 7, 0, 7, 18, 7, 0)


def test_bump_reports_no_update_when_guard_rejects(fakedb):
    # rowcount 0 == the monotonic WHERE matched no row (not greater / race).
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert bump_fighter_record(conn, 7, wins=10, losses=2, draws=0) is False


def test_bump_rejects_negative_without_touching_db(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert bump_fighter_record(conn, 7, wins=-1, losses=2, draws=0) is False
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------------------------- refresh_records

# (id, name, espn_id, wins, losses, draws)
_TARGETS = [
    (1, "Stale Fighter", "e1", 5, 2, 0),   # ESPN 5-3-0 -> total 8 > 7  -> UPDATE
    (2, "Current Fighter", "e2", 10, 1, 0),  # ESPN 10-1-0 -> equal      -> unchanged
    (3, "Homonym Silva", "e3", 23, 13, 2),  # ESPN 15-9-2 -> 26 < 38     -> skip
    (4, "Prospect Gable", "e4", 3, 0, 0),   # ESPN None                  -> unresolved
]

_ESPN = {"e1": (5, 3, 0), "e2": (10, 1, 0), "e3": (15, 9, 2), "e4": None}


def _fetch(espn_id):
    return _ESPN[espn_id]


def _responder(update_result=((1,),)):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT") and "FROM fighters f" in flat:
            return list(_TARGETS)
        if "UPDATE fighters" in flat:
            return list(update_result)
        return []

    return responder


def test_refresh_updates_only_the_stale_fighter(fakedb):
    conn = fakedb.Connection(_responder())
    counts = rfr.refresh_records(
        connection=conn, fetch_record=_fetch, days=90, delay=0
    )
    assert counts["targets"] == 4
    assert counts["resolved"] == 3          # e4 is None
    assert counts["updated"] == 1           # only the stale fighter
    assert counts["unchanged"] == 1         # current fighter
    assert counts["not_greater_skipped"] == 1  # homonym (lower total)
    assert counts["unresolved"] == 1        # prospect ESPN can't resolve

    updates = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "UPDATE fighters" in sql
    ]
    assert len(updates) == 1
    _flat, params = updates[0]
    # Applies ESPN 5-3-0 to fighter id 1; params carry the monotonic guard args.
    assert params == (5, 3, 0, 1, 5, 3, 0)
    assert conn.commits == 1


def test_refresh_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder())
    counts = rfr.refresh_records(
        connection=conn, fetch_record=_fetch, days=90, delay=0, dry_run=True
    )
    assert counts["updated"] == 1           # reported
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_refresh_writes_backup_of_old_values(tmp_path, fakedb):
    backup = tmp_path / "records_backup.json"
    conn = fakedb.Connection(_responder())
    rfr.refresh_records(
        connection=conn, fetch_record=_fetch, days=90, delay=0,
        backup_path=str(backup),
    )
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert saved == [
        {"id": 1, "name": "Stale Fighter", "old": [5, 2, 0], "new": [5, 3, 0]}
    ]


def test_target_query_recent_is_completed_only_and_by_days(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    rfr._get_target_fighters(conn, days=14, all_fighters=False, limit=None)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # No espn_id filter: fighters WITHOUT an id are included (name fallback).
    assert "espn_id IS NOT NULL" not in flat
    assert "NOT (fi.winner_id IS NULL AND fi.method IS NULL)" in flat  # completed only
    assert "e.event_date >= (CURRENT_DATE - (%s || ' days')::interval)" in flat
    assert params == (14,)


def test_target_query_all_ignores_days(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    rfr._get_target_fighters(conn, days=14, all_fighters=True, limit=None)
    sql, _params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "espn_id IS NOT NULL" not in flat
    assert "event_date" not in flat


def test_target_offset_and_limit_slice_the_chunk(fakedb):
    all_rows = [(i, f"F{i}", None, 1, 0, 0) for i in range(10)]
    conn = fakedb.Connection(lambda sql, params=None: list(all_rows))
    got = rfr._get_target_fighters(conn, days=14, all_fighters=True, limit=3, offset=4)
    assert [r[0] for r in got] == [4, 5, 6]  # skip 4, take 3


# ------------------------------------------------ name-fallback safety guard


def test_name_change_is_safe_accepts_small_nondecreasing_bump():
    # A real "record froze one/two fights ago": each component grows, total +1..4.
    assert rfr._name_change_is_safe((9, 1, 0), (10, 1, 0), 4) is True   # +1 win
    assert rfr._name_change_is_safe((9, 1, 0), (11, 2, 0), 4) is True   # +3 total


def test_name_change_is_safe_rejects_unsafe_shapes():
    assert rfr._name_change_is_safe((9, 1, 0), (9, 1, 0), 4) is False   # no change
    assert rfr._name_change_is_safe((5, 2, 0), (20, 3, 0), 4) is False  # +16 -> homonym
    assert rfr._name_change_is_safe((8, 4, 0), (10, 3, 0), 4) is False  # a loss vanished
    assert rfr._name_change_is_safe((9, 1, 0), (14, 1, 0), 4) is False  # +5 > max_delta


# ------------------------------------------------ refresh via name fallback

# (id, name, espn_id=None, wins, losses, draws)
_NAME_TARGETS = [
    (10, "Wang Cong", None, 9, 1, 0),          # name 10-1-0 -> safe +1 -> UPDATE
    (11, "Big Jump", None, 5, 2, 0),           # name 20-3-0 -> +16 -> REJECT
    (12, "Comp Down", None, 8, 4, 0),          # name 10-3-0 -> loss vanished -> REJECT
    (13, "No Espn Prospect", None, 3, 0, 0),   # name None -> unresolved
]
_NAME_ESPN = {
    "Wang Cong": (10, 1, 0),
    "Big Jump": (20, 3, 0),
    "Comp Down": (10, 3, 0),
    "No Espn Prospect": None,
}


def _name_responder(sql, params=None):
    flat = " ".join(sql.split())
    if flat.startswith("SELECT") and "FROM fighters f" in flat:
        return list(_NAME_TARGETS)
    if "UPDATE fighters" in flat:
        return [(1,)]
    return []


def test_refresh_name_fallback_applies_only_safe_bumps(fakedb):
    conn = fakedb.Connection(_name_responder)
    counts = rfr.refresh_records(
        connection=conn,
        fetch_record=lambda espn_id: None,        # never called (espn_id is None)
        fetch_by_name=lambda name: _NAME_ESPN[name],
        days=90, delay=0,
    )
    assert counts["resolved_by_name"] == 3        # Wang Cong, Big Jump, Comp Down
    assert counts["unresolved"] == 1              # No Espn Prospect
    assert counts["updated"] == 1                 # only the safe +1 (Wang Cong)
    assert counts["name_rejected"] == 2           # Big Jump + Comp Down

    updates = [
        params for cur in conn.cursors for sql, params in cur.executed
        if "UPDATE fighters" in sql
    ]
    assert len(updates) == 1
    assert updates[0] == (10, 1, 0, 10, 10, 1, 0)  # Wang Cong -> 10-1-0
    assert conn.commits == 1


def test_refresh_without_name_fetcher_does_not_fall_back(fakedb):
    # No fetch_by_name -> a fighter without espn_id is simply unresolved (the old
    # behaviour is preserved; name fallback is opt-in / production-wired).
    conn = fakedb.Connection(_name_responder)
    counts = rfr.refresh_records(
        connection=conn, fetch_record=lambda espn_id: None, days=90, delay=0
    )
    assert counts["updated"] == 0
    assert counts["unresolved"] == 4
    assert fakedb.mutating_statements(conn) == []
