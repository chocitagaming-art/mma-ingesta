"""Read-only audit: find PHANTOM DUPLICATE upcoming bouts across all events.

Background: before the fmid-drift fix (reconcile_upcoming_fight_source_id), a
ufc.com bout whose per-fight fmid changed between scrapes was re-inserted as a
new row while the old one was flipped to 'cancelled' — two rows for the same
pairing, usually sharing a bout_order (a phantom 'cancelled' twin + the active
one). Event 1060 was cleaned by hand; this reports any OTHER event still
carrying that pattern so it can be sanitised.

Two signatures are reported:
  A) same (event_id, bout_order) with >1 row  — the classic phantom pattern;
  B) same (event_id, fighter pairing) with >1 non-result row — catches twins
     whose bout_order also drifted.

STRICTLY READ-ONLY (read-only transaction; no writes). Run with the ingesta
venv and DATABASE_URL set (.env is loaded automatically):

    python -m scripts.audit_duplicate_fights
"""

from __future__ import annotations

import os

import psycopg2

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

SIGNATURE_A = """
    SELECT f.event_id, e.name, f.bout_order,
           count(*) AS rows,
           count(*) FILTER (WHERE f.status IS DISTINCT FROM 'cancelled') AS active,
           count(*) FILTER (WHERE f.status = 'cancelled') AS cancelled,
           array_agg(f.id ORDER BY f.id) AS ids,
           array_agg(f.source_id ORDER BY f.id) AS source_ids
    FROM fights f
    LEFT JOIN events e ON e.id = f.event_id
    WHERE f.source = 'ufc.com' AND f.bout_order IS NOT NULL
    GROUP BY f.event_id, e.name, f.bout_order
    HAVING count(*) > 1
    ORDER BY f.event_id, f.bout_order
"""

SIGNATURE_B = """
    WITH pairing AS (
        SELECT id, event_id, source_id, status,
               LEAST(
                   COALESCE(fighter_red_id::text, LOWER(fighter_red_name)),
                   COALESCE(fighter_blue_id::text, LOWER(fighter_blue_name))
               ) AS lo,
               GREATEST(
                   COALESCE(fighter_red_id::text, LOWER(fighter_red_name)),
                   COALESCE(fighter_blue_id::text, LOWER(fighter_blue_name))
               ) AS hi
        FROM fights
        WHERE source = 'ufc.com' AND winner_id IS NULL AND method IS NULL
          AND fighter_red_name <> 'TBD' AND fighter_blue_name <> 'TBD'
    )
    SELECT event_id, lo, hi, count(*) AS rows,
           array_agg(id ORDER BY id) AS ids,
           array_agg(source_id ORDER BY id) AS source_ids,
           array_agg(COALESCE(status, 'active') ORDER BY id) AS statuses
    FROM pairing
    GROUP BY event_id, lo, hi
    HAVING count(*) > 1
    ORDER BY event_id
"""


def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set (put it in .env or the environment)")
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(SIGNATURE_A)
            rows_a = cur.fetchall()
            cur.execute(SIGNATURE_B)
            rows_b = cur.fetchall()
    finally:
        conn.close()

    print("=" * 78)
    print("SIGNATURE A — rows sharing (event_id, bout_order) [source=ufc.com]")
    print("=" * 78)
    if not rows_a:
        print("  none — no bout_order collisions.")
    for event_id, name, order, n, active, cancelled, ids, source_ids in rows_a:
        print(
            f"  event {event_id} ({name}) bout_order {order}: {n} rows "
            f"({active} active, {cancelled} cancelled) "
            f"ids={ids} source_ids={source_ids}"
        )

    print("\n" + "=" * 78)
    print("SIGNATURE B — same (event, pairing) with >1 non-result row")
    print("=" * 78)
    if not rows_b:
        print("  none — no pairing-level duplicates among undecided bouts.")
    for event_id, lo, hi, n, ids, source_ids, statuses in rows_b:
        print(f"  event {event_id} pairing ({lo} vs {hi}): {n} rows "
              f"ids={ids} source_ids={source_ids} statuses={statuses}")

    total = len(rows_a) + len(rows_b)
    print("\n" + "-" * 78)
    print(f"TOTAL groups flagged: A={len(rows_a)} B={len(rows_b)}")
    print("(read-only; nothing written)" if True else "")
    # Non-zero exit if anything is flagged, so this doubles as a CI/cron check.
    raise SystemExit(1 if total else 0)


if __name__ == "__main__":
    main()
