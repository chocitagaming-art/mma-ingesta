"""Read-only PRE-FLIGHT for the live-results estreno: would the ESPN live
updater find and match every bout of an upcoming card, days BEFORE it happens?

The cron path (espn_live_results) only acts on fights already in/post, so a
plain dry-run before fight night exits at the cheap guard and proves nothing
about matching. This reuses the SAME resolution the updater uses
(parse_scoreboard -> _resolve_fighter by espn id / fuzzy name -> match_db_event
-> find_fight_id_by_fighters) but runs it against the SCHEDULED scoreboard, so
you can confirm 1-2 days out that every bout will resolve to a DB fight and spot
any late card change (a replacement fighter not yet linked).

STRICTLY READ-ONLY (read-only transaction; only SELECTs). Run with the ingesta
venv and DATABASE_URL (.env loaded):

    python -m scripts.preflight_live_results                       # 1060, 2026-07-11
    python -m scripts.preflight_live_results --dates 20260711 --event-id 1060
"""

from __future__ import annotations

import argparse
from collections import Counter

from src.scrapers.config import get_settings
from src.scrapers.db import connect
from src.scrapers.espn import (
    _build_exact_name_index,
    _build_normalized_name_index,
    build_espn_session,
)
from src.scrapers.espn_live_results import (
    _resolve_fighter,
    fetch_scoreboard,
    match_db_event,
    parse_scoreboard,
)
from src.scrapers.repositories.fighters import get_all_fighters
from src.scrapers.repositories.fights import find_fight_id_by_fighters


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dates", default="20260711",
        help="ESPN dates filter (US local, YYYYMMDD)",
    )
    ap.add_argument(
        "--event-id", type=int, default=1060,
        help="only report the ESPN event that maps to this DB event id (0 = all)",
    )
    args = ap.parse_args()

    settings = get_settings()
    payload = fetch_scoreboard(build_espn_session(), args.dates)
    events = parse_scoreboard(payload)
    print(f"ESPN scoreboard dates={args.dates}: {len(events)} event(s)\n")

    total_ok = total = 0
    with connect(settings.database_url) as conn:
        conn.set_session(readonly=True)
        fighters = get_all_fighters(conn)
        exact = _build_exact_name_index(fighters)
        norm = _build_normalized_name_index(fighters)

        for ev in events:
            db_id = match_db_event(conn, ev, settings.promotion_id_ufc)
            if args.event_id and db_id != args.event_id:
                continue
            print("=" * 78)
            print(f"ESPN {ev.name!r} (start {ev.start_utc}) -> DB event {db_id}")
            print(f"  {len(ev.fights)} bouts on the ESPN card")
            print("=" * 78)
            matched = 0
            for f in ev.fights:
                red_id = _resolve_fighter(
                    conn, f.red_espn_id, f.red_name, exact, norm, Counter()
                )
                blue_id = _resolve_fighter(
                    conn, f.blue_espn_id, f.blue_name, exact, norm, Counter()
                )
                fid = (
                    find_fight_id_by_fighters(conn, db_id, red_id, blue_id)
                    if db_id and red_id and blue_id else None
                )
                if fid:
                    matched += 1
                    tag = f"OK -> fight {fid}"
                elif red_id is None or blue_id is None:
                    tag = f"UNRESOLVED (red_id={red_id} blue_id={blue_id})"
                else:
                    tag = "NO DB FIGHT for this pairing"
                print(f"  {f.red_name} vs {f.blue_name}: {tag}")
            print(f"\n  matched {matched}/{len(ev.fights)}\n")
            total_ok += matched
            total += len(ev.fights)

    if total:
        print(f"TOTAL matched {total_ok}/{total}")
        raise SystemExit(0 if total_ok == total else 1)
    print("No matching DB event found for the given filter.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
