"""upsert_event / upsert_event_meta must dedup against the events_name_date_promotion_key
natural key (migration 007) via explicit ON CONFLICT targets, otherwise the historical
ufcstats events (source_id NULL) re-import as duplicates and the upcoming-events path
crashes on the new UNIQUE constraint.
"""

from datetime import date

from src.scrapers.models import EventRecord
from src.scrapers.repositories.events import (
    EventMetaRecord,
    upsert_event,
    upsert_event_meta,
)


def _event():
    return EventRecord(
        name="UFC 300: Pereira vs Hill",
        event_date=date(2026, 4, 13),
        location="Las Vegas, Nevada",
        promotion_id=1,
    )


def _meta_event():
    return EventMetaRecord(
        name="UFC 320: Doe vs Roe",
        event_date=date(2026, 9, 12),
        start_time=None,
        location="Las Vegas, Nevada",
        promotion_id=1,
        status="upcoming",
        image_url=None,
        tagline=None,
        broadcast=None,
        ticket_url=None,
        headliner="Doe vs Roe",
        source="ufc.com",
        source_id="ufc-320",
    )


def _normalize(sql):
    return " ".join(sql.split())


def test_upsert_event_uses_explicit_conflict_target(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(7,)])
    event_id = upsert_event(conn, _event())
    assert event_id == 7
    sql = _normalize(fakedb.executed_statements(conn)[0])
    assert "ON CONFLICT (name, event_date, promotion_id)" in sql


def test_upsert_event_fallback_finds_conflicting_row_without_location(fakedb):
    """DO NOTHING fired (INSERT returns no row); the fallback SELECT must locate the
    existing natural-key row even when its location differs — i.e. it must NOT filter
    on location (the regression the auditors caught)."""

    def responder(sql, params=None):
        if "INSERT INTO events" in sql:
            return []          # ON CONFLICT DO NOTHING suppressed the insert
        return [(42,)]          # fallback SELECT finds the existing row

    conn = fakedb.Connection(responder)
    event_id = upsert_event(conn, _event())
    assert event_id == 42
    fallback_sql = _normalize(fakedb.executed_statements(conn)[1])
    assert "SELECT id FROM events" in fallback_sql
    assert "location" not in fallback_sql  # must match the conflict key, not location


def test_upsert_event_meta_hardened_against_natural_key(fakedb):
    """The upcoming-events path keys on (source, source_id); its INSERT must absorb a
    collision on the new (name, event_date, promotion_id) UNIQUE instead of crashing."""

    def responder(sql, params=None):
        if "WHERE source = %s" in sql:
            return []          # not found by (source, source_id) -> INSERT path
        return [(9,)]           # INSERT ... RETURNING id

    conn = fakedb.Connection(responder)
    event_id = upsert_event_meta(conn, _meta_event())
    assert event_id == 9
    insert_sql = _normalize(fakedb.executed_statements(conn)[1])
    assert "ON CONFLICT (name, event_date, promotion_id) DO UPDATE" in insert_sql


# --------------------------------- F7 hardening: never wipe optional metadata


_OPTIONAL_META = ("image_url", "tagline", "broadcast", "ticket_url", "headliner")


def test_upsert_event_meta_update_never_wipes_optional_metadata(fakedb):
    """UPDATE branch: a partial re-scrape (NULL poster/tagline/broadcast/ticket/
    headliner) must keep the stored value — COALESCE(%s, col). Core fields (name,
    status, date...) still update unconditionally."""

    def responder(sql, params=None):
        if "WHERE source = %s" in sql:
            return [(9,)]          # found by (source, source_id) -> UPDATE branch
        return []

    conn = fakedb.Connection(responder)
    upsert_event_meta(conn, _meta_event())
    update_sql = _normalize(fakedb.executed_statements(conn)[1])
    assert "UPDATE events SET" in update_sql
    for field in _OPTIONAL_META:
        assert f"{field} = COALESCE(%s, {field})" in update_sql
    # Core fields are NOT coalesced: a status flip / corrected date must land.
    assert "status = %s" in update_sql
    assert "name = %s" in update_sql
    assert "status = COALESCE" not in update_sql


def test_upsert_event_meta_conflict_coalesces_optional_metadata(fakedb):
    """ON CONFLICT branch (ufc.com card merging onto its ufcstats twin): same
    guard — fill optional metadata when present, never blank it with NULL."""

    def responder(sql, params=None):
        if "WHERE source = %s" in sql:
            return []              # not found -> INSERT ... ON CONFLICT path
        return [(9,)]

    conn = fakedb.Connection(responder)
    upsert_event_meta(conn, _meta_event())
    insert_sql = _normalize(fakedb.executed_statements(conn)[1])
    for field in _OPTIONAL_META:
        assert f"{field} = COALESCE(EXCLUDED.{field}, events.{field})" in insert_sql
