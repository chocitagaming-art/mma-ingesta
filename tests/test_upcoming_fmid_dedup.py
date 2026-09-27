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
    UNIQUE (source, source_id),
    -- The two CHECKs of public.fights: a corner rule that breaks them rolls
    -- the whole event back in production, so the tests must see them too.
    CONSTRAINT fights_red_ne_blue CHECK (fighter_red_id <> fighter_blue_id),
    CONSTRAINT fights_winner_is_participant CHECK (
        winner_id IS NULL OR winner_id = fighter_red_id OR winner_id = fighter_blue_id
    )
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


def _bout(red, blue, fmid, order, card_segment="early_prelims", weight_class="Bantamweight"):
    return ParsedBout(
        card_segment=card_segment,
        bout_order=order,
        weight_class=weight_class,
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


# ----------------------------------------- corners survive an unmatched re-scrape
#
# Bout 16352 (event 1091): linked by hand to 9130/9129 because the name matcher
# cannot resolve ufc.com's "Mahammadali Osmanli" / "Ilimbek Akylbek". The next
# refresh-upcoming pass (ufc.com still listed the finished event) wrote the
# matcher's NULLs over both ids: photos, records, flags and the winner mark
# vanished from the web, and ufcstats could not store the per-fighter stats.


def _fight(conn, fight_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fighter_red_id, fighter_blue_id, fighter_red_name, "
            "fighter_blue_name, card_segment, weight_class, source_id "
            "FROM fights WHERE id = %s",
            (fight_id,),
        )
        return cur.fetchone()


def _ids(conn, fight_id):
    return _fight(conn, fight_id)[:2]


def test_decided_bout_keeps_a_manual_link_through_an_unmatched_rescrape(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=9130, fighter_blue_id=9129,
        fighter_red_name="Mahammadali Osmanli", fighter_blue_name="Ilimbek Akylbek",
        bout_order=4, source="ufc.com", source_id="13102", winner_id=9129, method="DQ",
    )
    _run(conn, [_bout("Mahammadali Osmanli", "Ilimbek Akylbek", "13102", 4)], {})
    assert _ids(conn, fid) == (9130, 9129)


def test_upcoming_bout_keeps_a_manual_link_through_an_unmatched_rescrape(conn):
    # The dangerous case for fight night: a link made on Friday must still be
    # there when the live updater looks the bout up by corner ids.
    fid = _seed(
        conn, event_id=7, fighter_red_id=9130, fighter_blue_id=9129,
        fighter_red_name="Mahammadali Osmanli", fighter_blue_name="Ilimbek Akylbek",
        bout_order=4, source="ufc.com", source_id="13102",
    )
    _run(conn, [_bout("Mahammadali Osmanli", "Ilimbek Akylbek", "13102", 4)], {})
    assert _ids(conn, fid) == (9130, 9129)


def test_one_matched_corner_keeps_the_other_stored_link(conn):
    # 16351: Amaya resolves, "Tina Black" does not (fighter 9132 is named
    # Valesca Machado). The stored blue link must not blink to NULL daily.
    fid = _seed(
        conn, event_id=7, fighter_red_id=9131, fighter_blue_id=9132,
        fighter_red_name="Melissa Amaya", fighter_blue_name="Tina Black",
        bout_order=5, source="ufc.com", source_id="13101",
    )
    _run(conn, [_bout("Melissa Amaya", "Tina Black", "13101", 5)], {"Melissa Amaya": 9131})
    assert _ids(conn, fid) == (9131, 9132)


def test_unmatched_substitute_under_the_same_fmid_never_inherits_the_old_id(conn):
    # THE trap of a plain COALESCE: a new name in the slot is a new person.
    fid = _seed(
        conn, event_id=7, fighter_red_id=7130, fighter_blue_id=7131,
        fighter_red_name="Mickey Gall", fighter_blue_name="Sedriques Dumas",
        bout_order=3, source="ufc.com", source_id="13152",
    )
    _run(conn, [_bout("Luis Hernandez", "Sedriques Dumas", "13152", 3)],
         {"Sedriques Dumas": 7131})
    assert _ids(conn, fid) == (None, 7131)


def test_matched_substitute_under_the_same_fmid_takes_the_new_id(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=7130, fighter_blue_id=7131,
        fighter_red_name="Mickey Gall", fighter_blue_name="Sedriques Dumas",
        bout_order=3, source="ufc.com", source_id="13152",
    )
    _run(conn, [_bout("Luis Hernandez", "Sedriques Dumas", "13152", 3)],
         {"Luis Hernandez": 9200, "Sedriques Dumas": 7131})
    assert _ids(conn, fid) == (9200, 7131)


def test_a_resolved_incoming_id_wins_over_the_stored_one_before_the_fight(conn):
    # ufc.com is the corner authority while the bout has no result.
    fid = _seed(
        conn, event_id=7, fighter_red_id=11, fighter_blue_id=12,
        fighter_red_name="Same Name", fighter_blue_name="Other Name",
        bout_order=1, source="ufc.com", source_id="900",
    )
    _run(conn, [_bout("Same Name", "Other Name", "900", 1)],
         {"Same Name": 21, "Other Name": 12})
    assert _ids(conn, fid) == (21, 12)


def test_corner_swap_with_both_unmatched_follows_the_names(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=1, fighter_blue_id=2,
        fighter_red_name="Ana Uno", fighter_blue_name="Bea Dos",
        bout_order=1, source="ufc.com", source_id="901",
    )
    _run(conn, [_bout("Bea Dos", "Ana Uno", "901", 1)], {})
    assert _ids(conn, fid) == (2, 1)


def test_corner_swap_with_one_matched_does_not_break_red_ne_blue(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=1, fighter_blue_id=2,
        fighter_red_name="Ana Uno", fighter_blue_name="Bea Dos",
        bout_order=1, source="ufc.com", source_id="902",
    )
    _run(conn, [_bout("Bea Dos", "Ana Uno", "902", 1)], {"Bea Dos": 2})
    assert _ids(conn, fid) == (2, 1)


def test_decided_bout_freezes_its_pair_even_if_the_matcher_disagrees(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=31, fighter_blue_id=32,
        fighter_red_name="Red Done", fighter_blue_name="Blue Done",
        bout_order=1, source="ufc.com", source_id="903", winner_id=32, method="KO/TKO",
    )
    _run(conn, [_bout("Red Done", "Blue Done", "903", 1)], {"Red Done": 99})
    assert _ids(conn, fid) == (31, 32)


def test_decided_bout_half_linked_never_duplicates_the_stored_id(conn):
    # Freezing corner by corner would give (5, 5) here and violate
    # fights_red_ne_blue, rolling back the whole event. The pair freezes.
    fid = _seed(
        conn, event_id=7, fighter_red_id=5, fighter_blue_id=None,
        fighter_red_name="Half Red", fighter_blue_name="Half Blue",
        bout_order=1, source="ufc.com", source_id="904", winner_id=5, method="DQ",
    )
    _run(conn, [_bout("Half Red", "Half Blue", "904", 1)], {"Half Blue": 5})
    assert _ids(conn, fid) == (5, None)


def test_tbd_is_never_identity_evidence(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=41, fighter_blue_id=42,
        fighter_red_name="Leaves Card", fighter_blue_name="Stays On",
        bout_order=1, source="ufc.com", source_id="905",
    )
    _run(conn, [_bout("TBD", "Stays On", "905", 1)], {})
    assert _ids(conn, fid) == (None, 42)

    fid2 = _seed(
        conn, event_id=7, fighter_red_id=None, fighter_blue_id=None,
        fighter_red_name="TBD", fighter_blue_name="TBD",
        bout_order=2, source="ufc.com", source_id="906",
    )
    _run(conn, [_bout("TBD", "Stays On", "905", 1), _bout("TBD", "TBD", "906", 2)], {})
    assert _ids(conn, fid2) == (None, None)


def test_name_comparison_ignores_case(conn):
    fid = _seed(
        conn, event_id=7, fighter_red_id=51, fighter_blue_id=52,
        fighter_red_name="Ilimbek Akylbek", fighter_blue_name="Rival Name",
        bout_order=1, source="ufc.com", source_id="907",
    )
    _run(conn, [_bout("ILIMBEK AKYLBEK", "Rival Name", "907", 1)], {})
    assert _ids(conn, fid) == (51, 52)


def test_fmid_drift_keeps_a_manual_link(conn):
    # reconcile adopts the row by NAME, then the upsert must not drop the ids.
    fid = _seed(
        conn, event_id=7, fighter_red_id=9130, fighter_blue_id=9129,
        fighter_red_name="Mahammadali Osmanli", fighter_blue_name="Ilimbek Akylbek",
        bout_order=4, source="ufc.com", source_id="ufc-329#4",
    )
    _run(conn, [_bout("Mahammadali Osmanli", "Ilimbek Akylbek", "13102", 4)], {})
    rows = _rows(conn, 7)
    assert len(rows) == 1
    assert rows[0][0] == fid and rows[0][1] == "13102"
    assert _ids(conn, fid) == (9130, 9129)


def test_card_segment_and_weight_class_are_never_wiped_by_null(conn):
    # t1-0-0: an unsplit "Fight Card" template yields card_segment NULL.
    fid = _seed(
        conn, event_id=7, fighter_red_id=61, fighter_blue_id=62,
        fighter_red_name="Seg Red", fighter_blue_name="Seg Blue",
        bout_order=1, source="ufc.com", source_id="908",
        card_segment="main", weight_class="Lightweight",
    )
    _run(conn, [_bout("Seg Red", "Seg Blue", "908", 1, card_segment=None, weight_class=None)],
         {"Seg Red": 61, "Seg Blue": 62})
    assert _fight(conn, fid)[4:6] == ("main", "Lightweight")

    _run(conn, [_bout("Seg Red", "Seg Blue", "908", 1, card_segment="prelims",
                      weight_class="Catch Weight")],
         {"Seg Red": 61, "Seg Blue": 62})
    assert _fight(conn, fid)[4:6] == ("prelims", "Catch Weight")


def test_new_bout_with_null_segment_is_inserted_as_null(conn):
    _run(conn, [_bout("New Red", "New Blue", "909", 1, card_segment=None)], {})
    (row,) = _rows(conn, 7)
    assert _fight(conn, row[0])[4] is None


# ------------------------------------------ findings of the adversarial review


def test_same_incoming_name_in_both_corners_never_duplicates_a_stored_id(conn):
    # Without the "incoming names differ" guard on the swap branches, red kept
    # 11 by same-corner and blue took 11 by swap: (11, 11), CheckViolation,
    # whole event rolled back on every run.
    fid = _seed(
        conn, event_id=7, fighter_red_id=11, fighter_blue_id=12,
        fighter_red_name="Bruno Silva", fighter_blue_name="Other Guy",
        bout_order=1, source="ufc.com", source_id="910",
    )
    _run(conn, [_bout("Bruno Silva", "bruno silva", "910", 1)], {})
    assert _ids(conn, fid) == (11, None)

    mirror = _seed(
        conn, event_id=8, fighter_red_id=21, fighter_blue_id=22,
        fighter_red_name="Other Guy", fighter_blue_name="Bruno Silva",
        bout_order=1, source="ufc.com", source_id="911",
    )
    counts: Counter = Counter()
    _write_event_bouts(conn, lambda n: None, counts,
                       _event([_bout("Bruno Silva", "Bruno Silva", "911", 1)]), 8)
    assert _ids(conn, mirror) == (None, 22)


def test_decided_half_linked_bout_accepts_the_missing_corner(conn):
    # The pair freeze must not block a legitimate fill: the empty corner takes
    # a resolved id for the same slot name that agrees with the winner.
    fid = _seed(
        conn, event_id=7, fighter_red_id=5, fighter_blue_id=None,
        fighter_red_name="Half Red", fighter_blue_name="Half Blue",
        bout_order=1, source="ufc.com", source_id="912", winner_id=5, method="KO/TKO",
    )
    _run(conn, [_bout("Half Red", "Half Blue", "912", 1)], {"Half Blue": 77})
    assert _ids(conn, fid) == (5, 77)


def test_decided_half_linked_fill_must_agree_with_the_winner(conn):
    # winner 88 is the unlinked blue fighter: a resolved 77 for that slot
    # contradicts it, so the corner stays empty (and no CHECK fires).
    fid = _seed(
        conn, event_id=7, fighter_red_id=5, fighter_blue_id=None,
        fighter_red_name="Half Red", fighter_blue_name="Half Blue",
        bout_order=1, source="ufc.com", source_id="913", winner_id=88, method="SUB",
    )
    _run(conn, [_bout("Half Red", "Half Blue", "913", 1)], {"Half Blue": 77})
    assert _ids(conn, fid) == (5, None)


def test_decided_bout_relisted_the_other_way_round_keeps_names_with_ids(conn):
    # backfill_results matches stats by fighter_*_name: if the names flipped
    # while the ids stayed, each fighter's stats would go to the other.
    fid = _seed(
        conn, event_id=7, fighter_red_id=9130, fighter_blue_id=9129,
        fighter_red_name="Mahammadali Osmanli", fighter_blue_name="Ilimbek Akylbek",
        bout_order=4, source="ufc.com", source_id="13102", winner_id=9129, method="DQ",
    )
    _run(conn, [_bout("Ilimbek Akylbek", "Mahammadali Osmanli", "13102", 4)], {})
    assert _fight(conn, fid)[:4] == (9130, 9129, "Mahammadali Osmanli", "Ilimbek Akylbek")


def test_decided_bout_without_any_link_still_follows_the_card_names(conn):
    # No stored id: nothing to protect, the names keep following ufc.com.
    fid = _seed(
        conn, event_id=7, fighter_red_id=None, fighter_blue_id=None,
        fighter_red_name="Old Spelling", fighter_blue_name="Other One",
        bout_order=1, source="ufc.com", source_id="914", method="U-DEC",
    )
    _run(conn, [_bout("New Spelling", "Other One", "914", 1)], {})
    assert _fight(conn, fid)[:4] == (None, None, "New Spelling", "Other One")
