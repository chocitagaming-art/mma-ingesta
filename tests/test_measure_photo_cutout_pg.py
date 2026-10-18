"""measure_photo_cutout against a REAL Postgres: the two queries and the upsert.

The unit tests (test_measure_photo_cutout.py) check the SQL only by its shape. A
reversed ``checked_at > NOW() - …``, a wrong JOIN or a CASE that lets a timeout
overwrite a good verdict would pass there; here they run for real.

Session-local TEMP TABLEs SHADOW hall_of_fame, fighters and photo_cutout (the
script's unqualified names resolve to pg_temp first), and every test asserts that
before writing, so nothing outside this connection is touched; temp tables are
dropped on disconnect. The photo_cutout DDL is read from migration 031 itself, so
the test breaks if the script and the table drift apart.

🪤 On purpose it does NOT load .env (unlike test_upcoming_fmid_dedup): it runs
only when DATABASE_URL is exported, as in the CI integration step.
"""

import io
import os
import pathlib
import re

import pytest
from PIL import Image

psycopg2 = pytest.importorskip("psycopg2")
DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="needs DATABASE_URL (real Postgres integration test)"
)

from src.scrapers import measure_photo_cutout as m  # noqa: E402

MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations" / "031_salon_video_recortes.sql"

AMP_URL = "https://www.ufc.com/images/styles/x/Griffin.png?VersionId=v&amp;itok=t"
LOCAL_URL = "/fighters/forrest-griffin.webp"
HEADSHOT = "https://a.espncdn.com/i/headshots/mma/players/full/1.png"
CORNER_A = "https://a.espncdn.com/i/headshots/mma/players/full/2.png"
CORNER_B = "https://a.espncdn.com/i/headshots/mma/players/full/3.png"


def _photo_cutout_ddl() -> str:
    sql = MIGRATION.read_text(encoding="utf-8")
    match = re.search(r"CREATE TABLE IF NOT EXISTS photo_cutout \((.*?)\n\);", sql, re.S)
    assert match, "photo_cutout CREATE TABLE not found in migration 031"
    return "CREATE TEMP TABLE photo_cutout (" + match.group(1) + "\n)"


def _png_cutout() -> bytes:
    image = Image.new("RGBA", (200, 150), (0, 0, 0, 0))
    image.paste((90, 60, 40, 255), (30, 100, 170, 150))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpg_opaque() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (764, 434), (120, 120, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture()
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    try:
        with connection.cursor() as cur:
            cur.execute("CREATE TEMP TABLE fighters (id INTEGER PRIMARY KEY, headshot_url TEXT)")
            cur.execute(
                """CREATE TEMP TABLE hall_of_fame (
                       id SERIAL PRIMARY KEY, wing TEXT, display_name TEXT,
                       fighter_id INTEGER, fighter_a_id INTEGER, fighter_b_id INTEGER,
                       photo_url TEXT)"""
            )
            cur.execute(_photo_cutout_ddl())
            # Safety: the three names the script uses must resolve to pg_temp.
            for table in ("fighters", "hall_of_fame", "photo_cutout"):
                cur.execute(
                    "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.oid = %s::regclass",
                    (table,),
                )
                assert cur.fetchone()[0].startswith("pg_temp"), table
        # Commit: run() rolls back its read transaction, which would otherwise
        # drop the temp tables created inside it.
        connection.commit()
        yield connection
    finally:
        connection.close()


def _seed_salon(conn):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO fighters (id, headshot_url) VALUES (%s, %s)",
            [(1, HEADSHOT), (2, CORNER_A), (3, CORNER_B), (4, "https://ignored/own-photo-wins.png"),
             (5, None), (6, "")],
        )
        cur.executemany(
            "INSERT INTO hall_of_fame (wing, display_name, fighter_id, fighter_a_id, fighter_b_id, photo_url) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                ("moderna", "Uses the fighter headshot", 1, None, None, None),
                ("moderna", "Own photo wins over the headshot", 4, None, None, AMP_URL),
                ("pionera", "Local file, no fighter", None, None, None, LOCAL_URL),
                ("combate", "A vs B", None, 2, 3, None),
                ("combate", "Same corner again (distinct)", None, 2, 5, None),
                ("contribuidor", "No photo at all", 6, None, None, None),
            ],
        )
    conn.commit()


def _row(conn, url):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_cutout, width, height, http_status, checked_at < NOW() - interval '30 days' "
            "FROM photo_cutout WHERE url = %s",
            (url,),
        )
        return cur.fetchone()


def _put(conn, url, is_cutout, width, height, status, age_days):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO photo_cutout (url, is_cutout, width, height, http_status, checked_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW() - make_interval(days => %s))",
            (url, is_cutout, width, height, status, age_days),
        )
    conn.commit()


def test_salon_urls_person_photo_corners_exact_and_distinct(conn):
    _seed_salon(conn)
    urls = m.salon_urls(conn)
    # Own photo_url beats the headshot; corners from both sides; NULL and '' out;
    # '&amp;' kept literally; CORNER_A appears twice in the data but once here.
    assert urls == sorted([AMP_URL, LOCAL_URL, HEADSHOT, CORNER_A, CORNER_B])
    assert "https://ignored/own-photo-wins.png" not in urls


def test_stale_urls_age_failure_and_missing_dimensions(conn):
    _put(conn, "fresh-good", True, 200, 150, 200, 1)
    _put(conn, "edge-29d", True, 200, 150, 200, 29)
    _put(conn, "old-31d", True, 200, 150, 200, 31)
    _put(conn, "fresh-503", True, 200, 150, 503, 1)
    _put(conn, "fresh-404", False, None, None, 404, 1)
    _put(conn, "fresh-200-no-image", False, None, None, 200, 1)
    urls = ["edge-29d", "fresh-200-no-image", "fresh-404", "fresh-503", "fresh-good", "never", "old-31d"]
    assert m.stale_urls(conn, urls) == ["fresh-200-no-image", "fresh-404", "fresh-503", "never", "old-31d"]


def test_failed_recheck_keeps_a_good_verdict_and_only_404_resets_it(conn):
    for url in ("timeout", "busy", "challenge", "gone", "now-opaque"):
        _put(conn, url, True, 200, 150, 200, 31)  # good cutouts due for recheck
    payloads = {
        "timeout": (None, None),
        "busy": (503, None),
        "challenge": (200, b"<!doctype html><title>Just a moment</title>"),
        "gone": (404, None),
        "now-opaque": (200, _jpg_opaque()),
        "new-cutout": (200, _png_cutout()),
        "new-blocked": (403, None),
    }
    urls = sorted(payloads)
    todo = m.stale_urls(conn, urls)
    conn.rollback()
    assert todo == urls
    m.upsert(conn, m.measure_all(todo, payloads.__getitem__, sleep=lambda _s: None))

    # Transient failures: verdict, size and checked_at untouched, status recorded.
    assert _row(conn, "timeout") == (True, 200, 150, None, True)
    assert _row(conn, "busy") == (True, 200, 150, 503, True)
    # A 200 that is not an image keeps the verdict AND stays stale (old checked_at).
    assert _row(conn, "challenge") == (True, 200, 150, 200, True)
    # Gone: framed, no size, fresh.
    assert _row(conn, "gone") == (False, None, None, 404, False)
    # A valid new measurement replaces everything.
    assert _row(conn, "now-opaque") == (False, 764, 434, 200, False)
    # New rows: a good one is stored; a failed one is born framed with no size.
    assert _row(conn, "new-cutout") == (True, 200, 150, 200, False)
    assert _row(conn, "new-blocked") == (False, None, None, 403, False)

    # Next pass retries exactly the failed ones (gone included: it costs one
    # request and catches a photo that comes back).
    assert m.stale_urls(conn, urls) == ["busy", "challenge", "gone", "new-blocked", "timeout"]


def test_run_end_to_end_on_real_tables(conn):
    _seed_salon(conn)
    _put(conn, HEADSHOT, True, 200, 150, 200, 2)  # measured recently: skipped
    requested: list[str] = []
    payloads = {
        AMP_URL: (200, _jpg_opaque()),
        LOCAL_URL: (200, _png_cutout()),
        CORNER_A: (200, _png_cutout()),
        CORNER_B: (429, None),
    }

    def fetch(url):
        requested.append(url)
        return payloads[url]

    summary = m.run(conn, fetch, sleep=lambda _s: None)
    assert requested == sorted([AMP_URL, LOCAL_URL, CORNER_A, CORNER_B])
    assert summary["salon_urls"] == 5 and summary["measured"] == 4 and summary["written"] == 4
    assert summary["fetch_errors"] == [CORNER_B] and summary["gone"] == []
    assert m.exit_code(summary) == 1
    assert _row(conn, AMP_URL)[:4] == (False, 764, 434, 200)   # key keeps '&amp;'
    assert _row(conn, LOCAL_URL)[:4] == (True, 200, 150, 200)  # key is the local path
