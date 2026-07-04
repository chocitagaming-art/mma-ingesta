"""_complete_dropped_upcoming must never flip FUTURE events to 'completed'.

ufc.com/events page 1 lists only ~8 upcoming cards, so with 9+ events announced
the 9th drops off the listing while still months away (happened to event
id=1085, Paris 2026-09-05, flipped to completed while future). The status flip
must be guarded by date in the SQL itself (event_date IS NOT NULL AND
event_date < CURRENT_DATE) and the reported count must come from
cursor.rowcount (rows really updated), not from len(stale_ids).
"""

from src.scrapers.upcoming_events import _complete_dropped_upcoming


PAST_ID = 900     # already held -> the SQL date guard matches it
FUTURE_ID = 1085  # Paris 2026-09-05 -> future, the guard must NOT match


def _responder(sql, params=None):
    normalized = " ".join(sql.split())
    if normalized.upper().startswith("SELECT"):
        return [(PAST_ID, "ufc-fight-night-old"), (FUTURE_ID, "ufc-fight-night-paris")]
    if "UPDATE events" in normalized:
        # Simulate Postgres evaluating the date guard: only the past-dated
        # event satisfies the WHERE, so only that UPDATE reports rowcount=1
        # (RecordingCursor.rowcount == len of the responder's result).
        if "event_date < CURRENT_DATE" not in normalized:
            return [(PAST_ID,), (FUTURE_ID,)]  # unguarded UPDATE would hit any id
        return [(PAST_ID,)] if params == (PAST_ID,) else []
    return []


def _update_statements(fakedb, conn):
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if "UPDATE events" in sql
    ]


def test_stale_future_event_is_not_completed_but_past_one_is(fakedb):
    conn = fakedb.Connection(_responder)

    completed = _complete_dropped_upcoming(conn, current_source_ids=set())

    # Only the past event flips; before the fix this returned len(stale_ids)=2.
    assert completed == 1
    updates = _update_statements(fakedb, conn)
    assert len(updates) == 2  # both stale ids attempted, the SQL guard decides
    for sql, _params in updates:
        assert "event_date IS NOT NULL" in sql
        assert "event_date < CURRENT_DATE" in sql


def test_events_still_listed_are_untouched(fakedb):
    conn = fakedb.Connection(_responder)

    completed = _complete_dropped_upcoming(
        conn,
        current_source_ids={"ufc-fight-night-old", "ufc-fight-night-paris"},
    )

    assert completed == 0
    assert fakedb.mutating_statements(conn) == []
