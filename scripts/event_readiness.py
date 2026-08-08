"""Read-only GO / NO-GO for the night before a live event.

`preflight_live_results` answers ONE question -- will every ESPN bout resolve to
a DB fight? -- and that is the most important one, but not the only way the
night can silently produce nothing. This wraps it and adds the checks that only
bite on event day:

  A. EVENT NAME ALIGNMENT. The loop finds the event through `match_db_event`
     (name + date). If the main event changes during fight week, ufc.com renames
     the card, our row keeps the old name and the match can fail -- and a failed
     match raises NO error: the loop just writes nothing, all night, in green.
     This is the single highest-value check of the week.
  B. CARD DIFF, both directions. Bouts ESPN lists that we cannot map, and bouts
     we hold that ESPN no longer lists (a pulled fighter).
  C. DUPLICATE / CANCELLED rows for the event. `find_fight_id_by_fighters` does
     not filter `status='cancelled'`, so a cancelled twin with a lower id could
     swallow the writes.
  D. SAMPLE BASELINE. How many rows the time series holds right now, so that
     during the event "is it growing?" has something to be compared against.

Exit code 0 = GO, 1 = something needs a human. STRICTLY READ-ONLY (read-only
transaction; only SELECTs). Run with the ingesta venv and DATABASE_URL:

    python -m scripts.event_readiness --event-id 1062 --dates 20260725
"""

from __future__ import annotations

import argparse
import sys
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

OK = "  [ OK ]"
WARN = "  [WARN]"
BAD = "  [FAIL]"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace(".", " ").replace(":", " ").split())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", type=int, required=True)
    ap.add_argument("--dates", required=True, help="ESPN dates filter, YYYYMMDD")
    args = ap.parse_args()

    problems: list[str] = []
    warnings: list[str] = []

    settings = get_settings()
    payload = fetch_scoreboard(build_espn_session(), args.dates)
    espn_events = parse_scoreboard(payload)
    print(f"\nESPN scoreboard dates={args.dates}: {len(espn_events)} evento(s)")

    with connect(settings.database_url) as conn:
        conn.set_session(readonly=True)
        cur = conn.cursor()

        cur.execute(
            "SELECT name, event_date, prelims_time, start_time, location"
            " FROM events WHERE id = %s",
            (args.event_id,),
        )
        row = cur.fetchone()
        if row is None:
            print(f"{BAD} El evento {args.event_id} no existe en la BD.")
            raise SystemExit(1)
        db_name, db_date, prelims, start, location = row

        print("\n" + "=" * 74)
        print(f"  EVENTO {args.event_id}: {db_name}")
        print(f"  {db_date} · {location}")
        print(f"  prelims {prelims} · main card {start}")
        print("=" * 74)

        # --- A. Alineacion de nombres: el chequeo que mas silencio produce ---
        print("\nA. EL BUCLE ENCUENTRA EL EVENTO")
        target = None
        for ev in espn_events:
            if match_db_event(conn, ev, settings.promotion_id_ufc) == args.event_id:
                target = ev
                break
        if target is None:
            print(f"{BAD} match_db_event NO casa ningun evento de ESPN con {args.event_id}.")
            print("       El bucle correria toda la noche sin escribir nada, EN VERDE.")
            for ev in espn_events:
                print(f"       ESPN ofrece: {ev.name!r} ({ev.start_utc})")
            print(f"       Nuestra fila: {db_name!r}")
            problems.append("el bucle no encontraria el evento")
        else:
            print(f"{OK} ESPN {target.name!r} -> evento {args.event_id}")
            if _norm(target.name) != _norm(db_name):
                print(f"{WARN} Los nombres NO son identicos (el match aguanta, pero ojo):")
                print(f"       ESPN: {target.name!r}")
                print(f"       BD  : {db_name!r}")
                warnings.append("nombres de evento distintos")

        # --- B. Diff de cartelera en los dos sentidos ---
        print("\nB. CARTELERA: ESPN vs NUESTRA BD")
        cur.execute(
            "SELECT id, fighter_red_name, fighter_blue_name, status"
            " FROM fights WHERE event_id = %s",
            (args.event_id,),
        )
        db_fights = cur.fetchall()
        db_ids = {r[0] for r in db_fights}
        print(f"  ESPN lista {len(target.fights) if target else 0} combates;"
              f" nosotros tenemos {len(db_fights)}")

        mapped: set[int] = set()
        if target:
            fighters = get_all_fighters(conn)
            exact = _build_exact_name_index(fighters)
            norm = _build_normalized_name_index(fighters)
            for f in target.fights:
                red = _resolve_fighter(conn, f.red_espn_id, f.red_name, exact, norm, Counter())
                blue = _resolve_fighter(conn, f.blue_espn_id, f.blue_name, exact, norm, Counter())
                fid = (
                    find_fight_id_by_fighters(conn, args.event_id, red, blue)
                    if red and blue else None
                )
                if fid:
                    mapped.add(fid)
                else:
                    reason = "sin luchador en BD" if not (red and blue) else "sin pelea en BD"
                    print(f"{WARN} ESPN {f.red_name} vs {f.blue_name}: {reason}")
                    warnings.append(f"ESPN sin mapear: {f.red_name} vs {f.blue_name}")
            print(f"  Mapeados {len(mapped)}/{len(target.fights)}")

        huerfanas = db_ids - mapped
        if huerfanas:
            for fid, red, blue, status in db_fights:
                if fid in huerfanas:
                    print(f"{WARN} Tenemos {fid} {red} vs {blue} y ESPN NO lo lista"
                          f" (status={status})")
            warnings.append(f"{len(huerfanas)} pelea(s) nuestras no listadas por ESPN")

        # --- C. Duplicados y canceladas ---
        print("\nC. FILAS DUPLICADAS O CANCELADAS")
        cancelled = [r for r in db_fights if (r[3] or "") == "cancelled"]
        if cancelled:
            for fid, red, blue, _ in cancelled:
                print(f"{WARN} cancelada: {fid} {red} vs {blue}")
            warnings.append(f"{len(cancelled)} pelea(s) cancelada(s)")
        else:
            print(f"{OK} 0 canceladas")

        cur.execute(
            """
            SELECT LEAST(fighter_red_id, fighter_blue_id),
                   GREATEST(fighter_red_id, fighter_blue_id), COUNT(*), ARRAY_AGG(id)
            FROM fights
            WHERE event_id = %s AND fighter_red_id IS NOT NULL
              AND fighter_blue_id IS NOT NULL
            GROUP BY 1, 2 HAVING COUNT(*) > 1
            """,
            (args.event_id,),
        )
        dupes = cur.fetchall()
        if dupes:
            for _, _, count, ids in dupes:
                print(f"{BAD} {count} filas para el mismo emparejamiento: {ids}")
            problems.append("emparejamientos duplicados")
        else:
            print(f"{OK} 0 emparejamientos duplicados")

        # --- D. Linea base de la serie ---
        print("\nD. LINEA BASE DE LA PELICULA")
        cur.execute(
            "SELECT COUNT(*) FROM live_fight_stat_samples s"
            " JOIN fights f ON f.id = s.fight_id WHERE f.event_id = %s",
            (args.event_id,),
        )
        del_evento = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM live_fight_stat_samples")
        total = cur.fetchone()[0]
        print(f"  Muestras de este evento: {del_evento} · en toda la tabla: {total}")
        print("  Durante la velada, la primera cifra TIENE que crecer:")
        print(f"    python -m scripts.live_watch --event-id {args.event_id} --watch")

    print("\n" + "=" * 74)
    if problems:
        print("  VEREDICTO: NO-GO -- hace falta un humano")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)
    if warnings:
        print("  VEREDICTO: GO con avisos (revisalos, pueden ser esperados)")
        for w in warnings:
            print(f"    - {w}")
        raise SystemExit(0)
    print("  VEREDICTO: GO -- todo alineado")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
