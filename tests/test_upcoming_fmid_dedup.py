"""Regression (BE7b): a bout's ufc.com fmid is NOT stable across scrapes, so
keying identity on (source, source_id) alone inserts a PHANTOM duplicate when
the fmid drifts (early-prelim fallback "<event>#<order>" -> real data-fmid, or
UFC re-numbering). _write_event_bouts must reconcile by the fighter pairing and
adopt the existing row in place — one active row per pairing, no phantom.

Real Postgres via a session-local TEMP TABLE that SHADOWS public.fights: the
scraper's unqualified `fights` resolves to pg_temp, so this exercises the true
ON CONFLICT / IS DISTINCT FROM / ANY() semantics the fakedb mock cannot, while
never touching production data (temp tables are dropped on disconnect).

Skipped when DATABASE_URL is absent (e.g. CI, which is intentionally DB-less).
"""

import os
from collections import Counter

import pytest

try:  # optional: load .env for local runs; harmless if missing
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:  # pragma: no cover
    pass

psycopg2 = pytest.importorskip("psycopg2")
DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="needs DATABASE_URL (real Postgres integration test)"
)

from src.scrapers.upcoming_events import (  # noqa: E402
    ParsedBout,
    ParsedEvent,
    _write_event_bouts,
)

TEMP_FIGHTS_DDL = """
CREATE TEMP TABLE fights (
    id SERIAL PRIMARY KEY,
    event_id INTEGER,
    fighter_red_id INTEGER,
    fighter_blue_id INTEGER,
    fighter_red_name TEXT,
    fighter_blue_name TEXT,
    weight_class TEXT,
    scheduled_rounds INTEGER,
    bout_order INTEGER,
    card_segment TEXT,
    is_title_fight BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT,
    source_id TEXT,
    status TEXT,
    winner_id INTEGER,
    method TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source, source_id)
) ON COMMIT DROP
"""


@pytest.fixture
def conn():
    c = psycopg2.connect(DATABASE_URL)
    try:
        with c.cursor() as cur:
            cur.execute(TEMP_FIGHTS_DDL)
        yield c
    finally:
        c.rollback()  # discards the temp table + all rows; prod untouched
        c.close()


def _seed(conn, **cols):
    keys = list(cols)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO fights ({', '.join(keys)}) "
            f"VALUES ({', '.join(['%s'] * len(keys))}) RETURNING id",
            [cols[k] for k in keys],
        )
        return int(cur.fetchone()[0])


def _rows(conn, event_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_id, status, bout_order, fighter_red_id, "
            "fighter_blue_id FROM fights WHERE event_id = %s ORDER BY id",
            (event_id,),
        )
        return cur.fetchall()


def _bout(red, blue, fmid, order):
    return ParsedBout(
        card_segment="early_prelims",
        bout_order=order,
        weight_class="Bantamweight",
        scheduled_rounds=3,
        red_name=red,
        blue_name=blue,
        fmid=fmid,
        red_image_url=None,
        blue_image_url=None,
        is_title=False,
    )


def _event(bouts):
    return ParsedEvent(
        source_id="ufc-329",
        detail_url="https://www.ufc.com/event/ufc-329",
        headliner=None,
        event_date=None,
        start_time=None,
        location=None,
        ticket_url=None,
        bouts=bouts,
    )


def _run(conn, bouts, name_to_id):
    counts: Counter = Counter()
    _write_event_bouts(conn, lambda n: name_to_id.get(n), counts, _event(bouts), 7)
    return counts


# --------------------------------------------------------------- regressions


def test_fmid_drift_with_late_linking_adopts_in_place(conn):
    """The real 1060 bug: row first stored with the fallback fmid and NULL ids
    (fighters not yet linked); re-scrape assigns the real fmid AND links the
    fighters. Must adopt the existing row by NAME, not insert a phantom."""
    seed_id = _seed(
        conn, event_id=7, fighter_red_id=None, fighter_blue_id=None,
        fighter_red_name="Javid Basharat", fighter_blue_name="Chad Garza",
        bout_order=12, source="ufc.com", source_id="ufc-329#12",
    )
    _run(conn, [_bout("Javid Basharat", "Chad Garza", "555001", 12)],
         {"Javid Basharat": 101, "Chad Garza": 102})

    rows = _rows(conn, 7)
    assert len(rows) == 1, f"expected 1 row (no phantom), got {rows}"
    (rid, source_id, status, order, red_id, blue_id), = rows
    assert rid == seed_id              # same row adopted, id preserved
    assert source_id == "555001"       # migrated to the real fmid
    assert status is None              # active, not cancelled
    assert (red_id, blue_id) == (101, 102)  # fighters now linked


def test_fmid_drift_with_ids_already_linked_adopts_in_place(conn):
    """fmid drifts while the fighters were already linked -> match by id
    (either corner order)."""
    seed_id = _seed(
        conn, event_id=7, fighter_red_id=201, fighter_blue_id=202,
        fighter_red_name="Paulo Costa", fighter_blue_name="Kevin Durden",
        bout_order=14, source="ufc.com", source_id="ufc-329#14",
    )
    # corners swapped on the re-scrape too, to prove order-insensitivity
    _run(conn, [_bout("Kevin Durden", "Paulo Costa", "555002", 14)],
         {"Paulo Costa": 201, "Kevin Durden": 202})

    rows = _rows(conn, 7)
    assert len(rows) == 1, f"expected 1 row (no phantom), got {rows}"
    assert rows[0][0] == seed_id
    assert rows[0][1] == "555002"
    assert rows[0][2] is None


def test_genuinely_dropped_pairing_is_still_cancelled(conn):
    """A pairing that really leaves the card (not on the re-scrape) must still
    be flipped to 'cancelled' — the dedup must not swallow real cancellations."""
    kept = _seed(
        conn, event_id=7, fighter_red_id=301, fighter_blue_id=302,
        fighter_red_name="Stay Red", fighter_blue_name="Stay Blue",
        bout_order=1, source="ufc.com", source_id="700",
    )
    gone = _seed(
        conn, event_id=7, fighter_red_id=401, fighter_blue_id=402,
        fighter_red_name="Gone Red", fighter_blue_name="Gone Blue",
        bout_order=2, source="ufc.com", source_id="701",
    )
    counts = _run(conn, [_bout("Stay Red", "Stay Blue", "700", 1)],
                  {"Stay Red": 301, "Stay Blue": 302})

    by_id = {r[0]: r for r in _rows(conn, 7)}
    assert by_id[kept][2] is None          # still active
    assert by_id[gone][2] == "cancelled"   # correctly cancelled
    assert counts["bouts_cancelled"] == 1


def test_stable_fmid_updates_in_place_without_reconcile(conn):
    """When the fmid is stable, a re-scrape that only reorders the card updates
    the same row in place (the normal ON CONFLICT path); no duplicate."""
    seed_id = _seed(
        conn, event_id=7, fighter_red_id=501, fighter_blue_id=502,
        fighter_red_name="Reorder Red", fighter_blue_name="Reorder Blue",
        bout_order=10, source="ufc.com", source_id="800",
    )
    _run(conn, [_bout("Reorder Red", "Reorder Blue", "800", 3)],
         {"Reorder Red": 501, "Reorder Blue": 502})

    rows = _rows(conn, 7)
    assert len(rows) == 1
    assert rows[0][0] == seed_id
    assert rows[0][3] == 3  # bout_order updated
