"""Sanitise PHANTOM DUPLICATE upcoming bouts left by the pre-fix fmid drift.

A phantom is a status='cancelled', undecided ufc.com bout that shares its
(event_id, bout_order) with an ACTIVE row for the same pairing — the stale twin
created when the fmid changed and the old row was cancelled instead of updated
(see reconcile_upcoming_fight_source_id). Event 1060 was cleaned by hand; this
does the same, safely, for any remaining event (e.g. 1064 / UFC 330).

Safety:
  - only rows that are 'cancelled', source='ufc.com', winner_id/method NULL,
    and that have an ACTIVE sibling with the same (event_id, bout_order);
  - every table with a fight_id FK is discovered from the catalog and checked;
    a candidate with ANY child row is SKIPPED (never cascade-deleted);
  - DRY-RUN by default — prints what it would delete and touches nothing;
  - --apply runs a single transaction with a hard guard on the row count.

    python -m scripts.clean_duplicate_fights           # dry-run
    python -m scripts.clean_duplicate_fights --apply    # execute
"""

from __future__ import annotations

import argparse
import os

import psycopg2

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

# Phantom cancelled twins: a cancelled undecided row whose (event, bout_order)
# also has an active row. array of ids is what we would delete.
CANDIDATES = """
    SELECT ph.id, ph.event_id, ph.bout_order, ph.source_id,
           ph.fighter_red_name, ph.fighter_blue_name
    FROM fights ph
    WHERE ph.source = 'ufc.com'
      AND ph.status = 'cancelled'
      AND ph.winner_id IS NULL AND ph.method IS NULL
      AND ph.bout_order IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM fights act
          WHERE act.event_id = ph.event_id
            AND act.bout_order = ph.bout_order
            AND act.id <> ph.id
            AND act.status IS DISTINCT FROM 'cancelled'
      )
    ORDER BY ph.event_id, ph.bout_order
"""

# Tables with a FK to fights(id), discovered from the catalog.
FK_TABLES = """
    SELECT tc.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
    JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name
    WHERE tc.constraint_type = 'FOREIGN KEY'
      AND ccu.table_name = 'fights'
      AND ccu.column_name = 'id'
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--apply", action="store_true",
        help="execute the deletes (default: dry-run)",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=not args.apply)
    try:
        with conn.cursor() as cur:
            cur.execute(FK_TABLES)
            child_tables = cur.fetchall()  # [(table, column), ...]
            cur.execute(CANDIDATES)
            candidates = cur.fetchall()

            print(f"Child tables with a fights.id FK: "
                  f"{[t for t, _ in child_tables] or '(none)'}")
            print(f"Phantom candidates found: {len(candidates)}\n")

            deletable: list[int] = []
            for fid, event_id, order, source_id, red, blue in candidates:
                refs = []
                for table, column in child_tables:
                    cur.execute(
                        f"SELECT count(*) FROM {table} WHERE {column} = %s", (fid,)
                    )
                    n = cur.fetchone()[0]
                    if n:
                        refs.append(f"{table}={n}")
                tag = (
                    "SKIP (has children: " + ", ".join(refs) + ")"
                    if refs else "DELETE"
                )
                print(f"  id={fid} event={event_id} bout_order={order} "
                      f"src={source_id} [{red} vs {blue}] -> {tag}")
                if not refs:
                    deletable.append(fid)

            print(f"\nWould delete {len(deletable)} rows: {deletable}")

            if not args.apply:
                print("\nDRY-RUN — nothing written. Re-run with --apply to execute.")
                return

            if not deletable:
                print("Nothing to delete.")
                return

            # Guarded delete in the open transaction.
            cur.execute(
                "DELETE FROM fights WHERE id = ANY(%s) "
                "AND source = 'ufc.com' AND status = 'cancelled' "
                "AND winner_id IS NULL AND method IS NULL",
                (deletable,),
            )
            deleted = cur.rowcount
            if deleted != len(deletable):
                conn.rollback()
                raise SystemExit(
                    f"GUARD TRIPPED: expected to delete {len(deletable)} but "
                    f"matched {deleted}; rolled back, nothing changed."
                )
            conn.commit()
            print(f"\nDeleted {deleted} phantom rows. Committed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
