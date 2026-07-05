"""Fase 4: title-fight persistence (BE9b) + cancelled-bout reconciliation (BE7).

BE9b: upcoming_events already detected "Title Bout" in the ufc.com class text
for scheduled_rounds but threw the flag away; now it rides ParsedBout.is_title
into fights.is_title_fight (migration 010) through upsert_upcoming_fight.

BE7: the reconciliation no longer DELETEs an event's bouts before re-inserting.
Bouts upsert by fmid; the ones missing from the newest card are flipped to
status='cancelled' (results are never touched) and a bout that reappears is
reactivated (status back to NULL) by the upsert's conflict branch.

HTML inline + fakedb; no network, no real DB.
"""

from collections import Counter
from dataclasses import replace

from bs4 import BeautifulSoup

from src.scrapers.repositories.fights import (
    UpcomingFightRecord,
    cancel_missing_upcoming_fights,
    upsert_upcoming_fight,
)
from src.scrapers.upcoming_events import (
    ParsedBout,
    ParsedEvent,
    _parse_bouts,
    _write_event_bouts,
)

# ------------------------------------------------------------------- fixtures

EVENT_HTML = """
<html><body>
<div class="c-listing-fight" data-fmid="9001">
  <div class="c-listing-fight__corner-name--red">Khamzat Chimaev</div>
  <div class="c-listing-fight__corner-name--blue">Sean Strickland</div>
  <div class="c-listing-fight__class-text">Middleweight Title Bout</div>
</div>
<div class="c-listing-fight" data-fmid="9002">
  <div class="c-listing-fight__corner-name--red">Bo Nickal</div>
  <div class="c-listing-fight__corner-name--blue">Carlos Prates</div>
  <div class="c-listing-fight__class-text">Middleweight Bout</div>
</div>
</body></html>
"""


def _bouts() -> list[ParsedBout]:
    return _parse_bouts(BeautifulSoup(EVENT_HTML, "lxml"), "ufc-328")


def _record(bout: ParsedBout, event_id: int = 7) -> UpcomingFightRecord:
    return UpcomingFightRecord(
        event_id=event_id,
        fighter_red_id=None,
        fighter_blue_id=None,
        fighter_red_name=bout.red_name,
        fighter_blue_name=bout.blue_name,
        weight_class=bout.weight_class,
        scheduled_rounds=bout.scheduled_rounds,
        bout_order=bout.bout_order,
        card_segment=bout.card_segment,
        source="ufc.com",
        source_id=bout.fmid,
        is_title_fight=bout.is_title,
    )


# ------------------------------------------------------------- BE9b: parsing


def test_parse_bouts_flags_title_bout():
    bouts = _bouts()
    assert bouts[0].is_title is True
    assert bouts[1].is_title is False
    # The existing rounds heuristic is untouched (title -> 5, regular -> 3).
    assert bouts[0].scheduled_rounds == 5
    assert bouts[1].scheduled_rounds == 3


def test_parsed_bout_is_title_defaults_false_for_old_constructions():
    bout = ParsedBout(
        card_segment=None, bout_order=1, weight_class=None,
        scheduled_rounds=3, red_name="A", blue_name="B", fmid="x",
    )
    assert bout.is_title is False


# --------------------------------------------------------- BE9b: persistence


def test_upsert_title_bout_persists_is_title_fight_true(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(11,)])
    fight_id = upsert_upcoming_fight(conn, _record(_bouts()[0]))
    assert fight_id == 11
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "is_title_fight" in flat
    # Insert side: the NOT NULL column takes COALESCE(%s, FALSE).
    assert "COALESCE(%s, FALSE)" in flat
    # The scraped True is bound (insert) and re-bound (conflict update).
    assert params[9] is True
    assert params[-1] is True


def test_upsert_null_title_flag_never_overwrites_stored_value(fakedb):
    # is_title_fight=None means "unknown": the insert side falls back to FALSE
    # (NOT NULL column) and the conflict branch keeps the stored value.
    conn = fakedb.Connection(lambda sql, params=None: [(11,)])
    record = replace(_record(_bouts()[1]), is_title_fight=None)
    upsert_upcoming_fight(conn, record)
    _sql, params = conn.cursors[0].executed[0]
    assert params[9] is None
    assert params[-1] is None


def test_upsert_update_branch_coalesces_with_stored_column(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(11,)])
    upsert_upcoming_fight(conn, _record(_bouts()[1]))
    flat = " ".join(conn.cursors[0].executed[0][0].split())
    # NOT EXCLUDED.is_title_fight (already coalesced to FALSE on the insert
    # side): the raw argument is re-bound so NULL keeps the stored value.
    assert "is_title_fight = COALESCE(%s, fights.is_title_fight)" in flat


# ------------------------------------------------- BE7: upsert reactivation


def test_upsert_reactivates_reappearing_bout(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(11,)])
    upsert_upcoming_fight(conn, _record(_bouts()[0]))
    flat = " ".join(conn.cursors[0].executed[0][0].split())
    # A bout present on the scraped card is by definition not cancelled: the
    # conflict branch resets status, reactivating a previously-cancelled row.
    assert "status = NULL" in flat


# ---------------------------------------------------- BE7: cancel the missing


def test_cancel_missing_flags_only_absent_fights(fakedb):
    # Simulate Postgres: fight 13 (not among the kept ids) is the only row the
    # guarded UPDATE touches; 11/12 (still on the card) stay intact.
    def responder(sql, params=None):
        if "SET status = 'cancelled'" in sql:
            return [(13,)]
        return []

    conn = fakedb.Connection(responder)
    flipped = cancel_missing_upcoming_fights(conn, 7, "ufc.com", [11, 12])
    assert flipped == 1
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "SET status = 'cancelled'" in flat
    assert "NOT (id = ANY(%s))" in flat
    # Bouts with a result are never touched; cancelled rows are not re-flipped.
    assert "winner_id IS NULL AND method IS NULL" in flat
    assert "status IS DISTINCT FROM 'cancelled'" in flat
    assert params == (7, "ufc.com", [11, 12])


def test_cancel_missing_empty_card_uses_typed_sentinel(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    cancel_missing_upcoming_fights(conn, 7, "ufc.com", [])
    _sql, params = conn.cursors[0].executed[0]
    # psycopg2 cannot adapt an empty list; -1 never matches a real id, so an
    # empty parse cancels every still-upcoming bout (the old delete's scope).
    assert params == (7, "ufc.com", [-1])


# ------------------------------------------- BE7 end to end (_write_event_bouts)


def _event(bouts: list[ParsedBout]) -> ParsedEvent:
    return ParsedEvent(
        source_id="ufc-328",
        detail_url="https://www.ufc.com/event/ufc-328",
        headliner=None,
        event_date=None,
        start_time=None,
        location=None,
        ticket_url=None,
        bouts=bouts,
    )


def test_write_event_bouts_upserts_then_cancels_the_missing(fakedb):
    upserted_ids = iter([11, 12])

    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("INSERT INTO fights"):
            return [(next(upserted_ids),)]
        if "SET status = 'cancelled'" in flat:
            return [(13,)]  # the dropped pairing: the only row Postgres flips
        return []

    conn = fakedb.Connection(responder)
    counts: Counter = Counter()
    _write_event_bouts(conn, lambda name: None, counts, _event(_bouts()), 7)

    assert counts["bouts_cancelled"] == 1
    statements = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
    ]
    inserts = [s for s in statements if s[0].startswith("INSERT INTO fights")]
    cancels = [s for s in statements if "SET status = 'cancelled'" in s[0]]
    assert len(inserts) == 2  # both scraped bouts upserted (no DELETE anywhere)
    assert not any("DELETE" in flat.upper() for flat, _p in statements)
    # The title bout carries True; the regular bout False (never NULL here).
    assert inserts[0][1][9] is True
    assert inserts[1][1][9] is False
    # The cancel runs AFTER the upserts and keeps exactly the upserted ids.
    (cancel_flat, cancel_params), = cancels
    assert statements.index(cancels[0]) > statements.index(inserts[1])
    assert cancel_params == (7, "ufc.com", [11, 12])
