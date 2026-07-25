"""Read-only CONTROL PANEL for fight night: is the live loop actually capturing?

The live loop always exits 0 (a bad pass is swallowed on purpose so one hiccup
does not kill a 4h window), and `notify-on-failure` only fires on conclusion
'failure' -- so a job that runs green for four hours while writing NOTHING is a
real, silent failure mode. The only trustworthy signal is the database itself.

This prints, in one screen, the four things worth knowing during an event:

  1. THE KEY SIGNAL -- rows landing in `live_fight_stat_samples` per bout (the
     time series behind "la pelicula del combate"). It must GROW.
  2. Snapshot freshness -- seconds since the loop last touched
     `live_fight_stats`. Distinguishes "loop is dead" from "loop is alive but
     the sample writer is broken", which need opposite responses.
  3. Card state -- what ESPN says about each bout right now.
  4. Verdict lines -- plain-language alarms instead of raw numbers.

STRICTLY READ-ONLY (read-only transaction; only SELECTs). Run with the ingesta
venv and DATABASE_URL (.env loaded):

    python -m scripts.live_watch                       # next/current event
    python -m scripts.live_watch --event-id 1062
    python -m scripts.live_watch --event-id 1062 --watch      # refresh every 30s
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from src.scrapers.config import get_settings
from src.scrapers.db import connect

# La consola de Windows abre en cp1252 y parte los nombres acentuados
# ("Benoit", "Loneier"...). Este panel se lee a toda prisa en mitad de una
# velada: un nombre roto hace dudar del dato que hay al lado.
# line_buffering: con --watch canalizado a un fichero o a grep, la salida se
# quedaba en el buffer y el panel parecia colgado.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Sin muestras nuevas en este tiempo, con la velada en marcha, algo va mal.
STALE_SAMPLE_SECONDS = 180
# El bucle pasa cada ~20 s; 25-35 s reales es lo normal (el sleep no descuenta
# la pasada). Por encima de esto la cadencia se ha degradado de verdad.
STALE_SNAPSHOT_SECONDS = 120


def _age(seconds) -> str:
    """"hace 40s" durante la velada; "hace 8d" en una serie vieja (un crudo
    752578s se lee como un fallo del panel, no como un dato de hace 9 dias)."""
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}min"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _resolve_event_id(cur, event_id: int | None) -> tuple[int | None, str, object]:
    if event_id:
        cur.execute("SELECT id, name, event_date FROM events WHERE id = %s", (event_id,))
    else:
        # El evento en curso o el siguiente: el que menos dista de hoy.
        cur.execute(
            """
            SELECT id, name, event_date FROM events
            WHERE event_date BETWEEN CURRENT_DATE - 1 AND CURRENT_DATE + 7
            ORDER BY event_date LIMIT 1
            """
        )
    row = cur.fetchone()
    return (None, "", None) if row is None else row


def _report(cur, event_id: int) -> None:
    print("-" * 78)
    print(f"  {datetime.now(timezone.utc):%H:%M:%S} UTC")

    # 1. LA SENAL CLAVE: la serie temporal creciendo.
    cur.execute(
        """
        SELECT s.fight_id, f.fighter_red_name, f.fighter_blue_name, COUNT(*),
               ROUND(EXTRACT(EPOCH FROM now() - MAX(s.sampled_at)))
        FROM live_fight_stat_samples s
        JOIN fights f ON f.id = s.fight_id
        WHERE f.event_id = %s
        GROUP BY s.fight_id, f.fighter_red_name, f.fighter_blue_name
        ORDER BY MAX(s.sampled_at) DESC
        """,
        (event_id,),
    )
    series = cur.fetchall()
    total_samples = sum(r[3] for r in series)
    print(f"\n  PELICULA -- {len(series)} pelea(s), {total_samples} muestras")
    for fight_id, red, blue, count, age in series:
        flag = "  <-- ESTANCADA" if age is not None and age > STALE_SAMPLE_SECONDS else ""
        print(f"    {fight_id}  {red} vs {blue}: {count} muestras, hace {_age(age)}{flag}")
    if not series:
        print("    (ninguna todavia)")

    # 2. Frescura del snapshot: separa "bucle muerto" de "escritor roto".
    cur.execute(
        """
        SELECT ROUND(EXTRACT(EPOCH FROM now() - MAX(l.updated_at)))
        FROM live_fight_stats l JOIN fights f ON f.id = l.fight_id
        WHERE f.event_id = %s
        """,
        (event_id,),
    )
    snap_age = cur.fetchone()[0]

    # 3. Estado de la cartelera segun ESPN.
    cur.execute(
        """
        SELECT f.id, f.card_segment, f.bout_order, f.fighter_red_name,
               f.fighter_blue_name, l.state, l.period, l.display_clock, l.is_final
        FROM fights f LEFT JOIN live_fight_stats l ON l.fight_id = f.id
        WHERE f.event_id = %s ORDER BY f.bout_order NULLS LAST
        """,
        (event_id,),
    )
    card = cur.fetchall()
    print("\n  CARTELERA")
    for fid, seg, order, red, blue, state, period, clock, final in card:
        if state is None:
            status = "sin datos"
        else:
            status = f"{state} R{period} {clock}" + (" FINAL" if final else "")
        print(f"    #{order or '?':>2} [{seg or '?':>7}] {fid}  {red} vs {blue}: {status}")

    # Ha empezado a pelearse alguien de verdad? Ojo: state='in' NO basta, porque
    # ESPN ya lo marca en los paseillos, con todas las stats a 0. Hay que exigir
    # asalto >= 1. Sin esta distincion el veredicto de abajo acusaba al escritor
    # de muestras durante los paseillos, cuando lo correcto es que aun no hay
    # nada que grabar (mismo despiste que tuvieron los dos vigias del 25-jul).
    fighting = any(
        (state == "in" and (period or 0) >= 1) or final for *_, state, period, _, final in card
    )

    # 4. Veredicto en cristiano.
    print("\n  VEREDICTO")
    if snap_age is None:
        print("    ! El bucle no ha tocado este evento todavia.")
        print("      Normal antes de empezar. Con la velada en marcha = revisar el job.")
    else:
        print(f"    Ultima pasada del bucle: hace {_age(snap_age)}")
        if snap_age > STALE_SNAPSHOT_SECONDS:
            print("    ! EL BUCLE PARECE MUERTO -> gh run list --workflow=live-event-loop.yml")
        elif not series and not fighting:
            print("    OK: el bucle esta vivo. Aun no hay muestras porque todavia no se")
            print("      pelea nadie (paseillos o hueco entre combates). No es un fallo.")
        elif not series:
            print("    ! El bucle VIVE, HAY UN COMBATE EN MARCHA y no entra ni una muestra:")
            print("      el fallo esta en el escritor de muestras, NO en el bucle.")
            print("      Relanzarlo no arregla nada.")
        else:
            print("    OK: el bucle esta vivo y la pelicula se esta grabando.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", type=int, default=None)
    ap.add_argument("--watch", action="store_true", help="refresh until Ctrl-C")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    settings = get_settings()
    with connect(settings.database_url) as conn:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        event_id, name, date = _resolve_event_id(cur, args.event_id)
        if event_id is None:
            print("No event found. Pass --event-id.")
            raise SystemExit(1)
        print(f"EVENTO {event_id}: {name} ({date})")

        while True:
            _report(cur, event_id)
            if not args.watch:
                return
            # El SELECT siguiente tiene que ver datos nuevos: en una transaccion
            # read-only de larga vida los veria congelados.
            conn.rollback()
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
