"""Read-only CHECKPOINT of an event's results ingestion — the "did the data
land?" report to run the morning after a card (e.g. UFC 329 / event 1060 on
Sun 12-jul, or any event by id).

For each non-cancelled bout it shows what the pipeline has filled so far:
winner, method (flagging the 'Decision'/'Submission'/'KO/TKO' PROVISIONAL codes
ESPN writes live, which backfill_results later upgrades from ufcstats), round +
time, referee, judges' scorecards and per-fighter stats rows. A summary line
tells you at a glance how complete the event is.

STRICTLY READ-ONLY. Run with the ingesta venv and DATABASE_URL (.env loaded):

    python -m scripts.checkpoint_event                # defaults to 1060 (UFC 329)
    python -m scripts.checkpoint_event --event-id 1059
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

PROVISIONAL = ("Decision", "Submission", "KO/TKO")

EVENT_SQL = "SELECT id, name, event_date, start_time, status FROM events WHERE id = %s"

FIGHTS_SQL = """
    SELECT f.bout_order, f.status,
           COALESCE(rf.name, f.fighter_red_name) AS red,
           COALESCE(bf.name, f.fighter_blue_name) AS blue,
           wf.name AS winner, f.method, f.end_round, f.end_time, f.referee,
           (SELECT count(*) FROM fight_scorecards s
            WHERE s.fight_id = f.id) AS scorecards,
           (SELECT count(*) FROM fight_stats st
            WHERE st.fight_id = f.id) AS stats_rows
    FROM fights f
    LEFT JOIN fighters rf ON rf.id = f.fighter_red_id
    LEFT JOIN fighters bf ON bf.id = f.fighter_blue_id
    LEFT JOIN fighters wf ON wf.id = f.winner_id
    WHERE f.event_id = %s
    ORDER BY f.bout_order NULLS LAST, f.id
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--event-id", type=int, default=1060,
        help="event id (default 1060 / UFC 329)",
    )
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")

    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(EVENT_SQL, (args.event_id,))
            ev = cur.fetchone()
            if ev is None:
                raise SystemExit(f"No event with id {args.event_id}")
            cur.execute(FIGHTS_SQL, (args.event_id,))
            rows = cur.fetchall()
    finally:
        conn.close()

    eid, name, edate, start, status = ev
    print("=" * 78)
    print(f"CHECKPOINT event {eid} — {name}")
    print(f"  event_date={edate}  start_time={start}  status={status}")
    print("=" * 78)

    active = [r for r in rows if r[1] != "cancelled"]
    cancelled = len(rows) - len(active)
    with_winner = sum(1 for r in active if r[4])
    with_method = sum(1 for r in active if r[5])
    provisional = sum(1 for r in active if r[5] in PROVISIONAL)
    with_ref = sum(1 for r in active if r[8])
    with_cards = sum(1 for r in active if r[9])
    with_stats = sum(1 for r in active if r[10] >= 2)
    n = len(active)

    print(f"\n{n} active bouts ({cancelled} cancelled)")
    print(f"  winner   : {with_winner}/{n}")
    print(f"  method   : {with_method}/{n}  (provisional/ESPN: {provisional})")
    print(f"  referee  : {with_ref}/{n}")
    print(f"  scorecards: {with_cards}/{n}")
    print(f"  stats (>=2 rows): {with_stats}/{n}\n")

    for (order, st, red, blue, winner, method, rnd, etime, ref, cards, stats) in active:
        res = f"{winner} def." if winner else "— (no result)"
        meth = f"{method}{'*' if method in PROVISIONAL else ''}" if method else "—"
        print(
            f"  #{order:<2} {red} vs {blue}\n"
            f"       {res}  {meth}  R{rnd or '-'} {etime or '-'}  "
            f"ref={ref or '—'}  cards={cards}  stats={stats}"
        )

    print("\n  * = provisional ESPN method (backfill_results upgrades from ufcstats)")
    print("  (read-only; nothing written)")


if __name__ == "__main__":
    main()
