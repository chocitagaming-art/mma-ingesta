"""Revisor post-evento: caza el dato que se quedó mal SIN que nada fallara.

POR QUE EXISTE
--------------
El 25-jul-2026, durante el evento 1062, ESPN retiró `Dulatov vs Turman` (12866)
de su cartelera EN MITAD de la velada. El bucle se comportó de forma impecable
--no la busca porque ESPN no la sirve: cero errores, cero unresolved, verde-- y
`consolidate-results` tampoco la resolvería nunca, porque ufcstats no lista lo
que no se celebró. La pelea se habría quedado PARA SIEMPRE con `winner_id NULL`
y sin marcar como cancelada, y la web la pintaba "EN CURSO".

Es el tercer caso del mismo patrón en un mes (las noticias de ESPN bloqueadas
desde el 30-jun, los pesajes escribiendo cero en verde, y esto): **nada peta,
todo verde, y el dato mal**. Los crons vigilan que los procesos terminen; nadie
vigilaba que el RESULTADO fuera coherente. Eso es lo que hace este script.

QUE COMPRUEBA
-------------
  A. PELEA FANTASMA. Evento ya celebrado (su estelar está resuelto) con peleas
     sin ganador y sin marcar 'cancelled'. Es el caso de arriba, y el único que
     de verdad exige una decisión humana: solo una persona puede decidir si esa
     pelea se cayó o si es nuestro pipeline el que la perdió.
  B. FALTA EL DATO. Peleas ya resueltas sin árbitro o sin stats pasado el plazo
     en el que la consolidación ya ha tenido todas sus oportunidades.
  C. EVENTO SIN CERRAR. Fecha pasada pero `status` todavía 'upcoming'.
  D. METODO PROVISIONAL. Sigue el genérico de ESPN ('Submission' a secas) en vez
     del que afina ufcstats ('SUB - Heel Hook').

POR QUE NO FALLA CON TODO LO QUE ENCUENTRA
------------------------------------------
Esta alerta cuelga de `notify-on-failure`, que REUTILIZA un único Issue y no lo
cierra solo. Un script que salga en rojo todos los domingos por 34 peleas de
2025 que nadie va a arreglar convierte esa alerta en ruido, y entonces deja de
avisar del caso que sí importa. Así que:

  - Solo A y los huecos DENTRO de la ventana de backfill hacen fallar.
  - Lo anterior a esa ventana se informa y no ensucia: es material de la fase 4
    ("consolidación por falta el dato"), no una incidencia de esta semana.

STRICTLY READ-ONLY: solo SELECTs, en transacción de solo lectura.

    python -m scripts.post_event_review               # últimos 10 días
    python -m scripts.post_event_review --event-id 1062
    python -m scripts.post_event_review --days 30
"""

from __future__ import annotations

import argparse
import sys

from src.scrapers.config import get_settings
from src.scrapers.db import connect
from src.scrapers.espn_live_results import ESPN_PROVISIONAL_METHODS

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

OK = "  [ OK ]"
WARN = "  [WARN]"
BAD = "  [FAIL]"

# Margen antes de exigir árbitro y stats. `consolidate-results` corre dom
# 08/11/15 UTC y llega con 1-2 h de retraso medido, así que un evento de sábado
# noche no tiene todas sus oportunidades hasta bien entrado el lunes. Con menos
# de esto estaríamos avisando de algo que aún puede llegar solo.
GRACE_DAYS = 2

# Más allá de esto la consolidación ya NO lo reintenta (backfill_results filtra
# por BACKFILL_WINDOW_DAYS = 60): no es una incidencia, es deuda conocida.
BACKFILL_WINDOW_DAYS = 60

# Genéricos de ESPN que ufcstats NUNCA escribe: él pone 'U-DEC'/'S-DEC'/'M-DEC'
# y 'SUB - Rear Naked Choke'. Verificado contra la BD: 0 peleas consolidadas
# tienen 'Decision' o 'Submission' a secas, así que verlos es señal limpia de
# que el afinado no llegó.
# ⚠️ 'KO/TKO' NO entra, aunque también sea un genérico de ESPN
# (ESPN_PROVISIONAL_METHODS lo incluye): ufcstats SÍ lo escribe tal cual cuando
# no detalla el golpe — hay 6 peleas consolidadas así. Meterlo daría un falso
# positivo permanente en cada KO, y una alerta que grita siempre no avisa nunca.
NEVER_FROM_UFCSTATS = tuple(
    m for m in ESPN_PROVISIONAL_METHODS if m in ("Decision", "Submission")
)

# El estelar es la pelea de MENOR bout_order (1 = cierra la noche). Si está
# resuelto, la velada se celebró de verdad: sin esto, un evento aplazado en
# bloque saldría como 12 peleas fantasma.
MAIN_EVENT_RESOLVED = """
  EXISTS (
    SELECT 1 FROM fights mf
    WHERE mf.event_id = e.id
      AND mf.status IS DISTINCT FROM 'cancelled'
      AND mf.bout_order IS NOT NULL
      AND (mf.winner_id IS NOT NULL OR mf.method IS NOT NULL)
      AND mf.bout_order = (
        SELECT MIN(mf2.bout_order) FROM fights mf2
        WHERE mf2.event_id = e.id
          AND mf2.status IS DISTINCT FROM 'cancelled'
          AND mf2.bout_order IS NOT NULL
      )
  )
"""


def _scope(event_id: int | None, days: int) -> tuple[str, list]:
    """Filtro de eventos y sus parámetros. Un --event-id explícito manda."""
    if event_id is not None:
        return "e.id = %s", [event_id]
    return (
        "e.event_date < CURRENT_DATE"
        " AND e.event_date >= CURRENT_DATE - (%s * INTERVAL '1 day')",
        [days],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event-id", type=int, default=None, help="Revisar solo este evento.")
    ap.add_argument(
        "--days", type=int, default=10,
        help="Ventana de eventos recientes a revisar (por defecto 10).",
    )
    args = ap.parse_args()

    where, params = _scope(args.event_id, args.days)
    problems: list[str] = []
    warnings: list[str] = []

    settings = get_settings()
    with connect(settings.database_url) as conn:
        conn.set_session(readonly=True)
        cur = conn.cursor()

        print("=" * 74)
        ambito = f"evento {args.event_id}" if args.event_id else f"últimos {args.days} días"
        print(f"  REVISOR POST-EVENTO — {ambito}")
        print("=" * 74)

        # --- A. Peleas fantasma -------------------------------------------
        print("\nA. PELEAS FANTASMA (celebrado, pero sin resolver ni cancelar)")
        cur.execute(
            f"""
            SELECT e.id, e.name, e.event_date, f.id, f.bout_order,
                   f.fighter_red_name, f.fighter_blue_name
            FROM fights f JOIN events e ON e.id = f.event_id
            WHERE {where}
              AND f.status IS DISTINCT FROM 'cancelled'
              AND f.winner_id IS NULL
              AND f.method IS NULL
              AND {MAIN_EVENT_RESOLVED}
            ORDER BY e.event_date DESC, f.bout_order
            """,
            params,
        )
        fantasmas = cur.fetchall()
        if fantasmas:
            for ev_id, ev_name, ev_date, fid, order, red, blue in fantasmas:
                print(f"{BAD} {ev_date} [{ev_id}] #{order or '?'} {fid}: {red} vs {blue}")
            print("\n  Una pelea que se cayó DURANTE el evento no la resuelve nadie:")
            print("  ESPN deja de servirla y ufcstats no lista lo que no se celebró.")
            print("  Si de verdad se cayó:  UPDATE fights SET status='cancelled' WHERE id=<id>;")
            print("  Si se celebró, es que el pipeline la perdió: hay que investigar.")
            problems.append(f"{len(fantasmas)} pelea(s) sin resolver ni cancelar")
        else:
            print(f"{OK} ninguna")

        # --- B. Falta el dato ---------------------------------------------
        print("\nB. FALTA EL DATO (resueltas, pero sin árbitro o sin stats)")
        cur.execute(
            f"""
            SELECT e.id, e.name, e.event_date,
                   CURRENT_DATE - e.event_date AS antiguedad,
                   count(*) FILTER (WHERE f.referee IS NULL) AS sin_arbitro,
                   count(*) FILTER (
                     WHERE NOT EXISTS (
                       SELECT 1 FROM fight_stats fs WHERE fs.fight_id = f.id)
                   ) AS sin_stats
            FROM fights f JOIN events e ON e.id = f.event_id
            WHERE {where}
              AND f.status IS DISTINCT FROM 'cancelled'
              AND (f.winner_id IS NOT NULL OR f.method IS NOT NULL)
              AND (
                f.referee IS NULL
                OR NOT EXISTS (SELECT 1 FROM fight_stats fs WHERE fs.fight_id = f.id)
              )
            GROUP BY e.id, e.name, e.event_date
            ORDER BY e.event_date DESC
            """,
            params,
        )
        huecos = cur.fetchall()
        if not huecos:
            print(f"{OK} ninguno")
        for ev_id, ev_name, ev_date, antiguedad, sin_arb, sin_stats in huecos:
            dias = int(antiguedad)
            detalle = f"{ev_date} [{ev_id}] {ev_name}: {sin_arb} sin árbitro, {sin_stats} sin stats"
            if dias < GRACE_DAYS:
                print(f"{OK} {detalle} — solo {dias}d, la consolidación aún no ha terminado")
            elif dias > BACKFILL_WINDOW_DAYS:
                # Fuera de la ventana de backfill: no se reintenta nunca más.
                print(f"{WARN} {detalle} — {dias}d, FUERA de la ventana de {BACKFILL_WINDOW_DAYS}d")
                print("         La consolidación ya no lo reintenta. Es la fase 4, no una incidencia.")
            else:
                print(f"{BAD} {detalle} — {dias}d, dentro de la ventana y sin llegar")
                problems.append(f"evento {ev_id} lleva {dias}d con datos incompletos")

        # --- C. Eventos sin cerrar ----------------------------------------
        print("\nC. EVENTOS PASADOS QUE SIGUEN EN 'upcoming'")
        cur.execute(
            f"""
            SELECT e.id, e.name, e.event_date, CURRENT_DATE - e.event_date
            FROM events e
            WHERE {where} AND e.status = 'upcoming'
            ORDER BY e.event_date DESC
            """,
            params,
        )
        abiertos = cur.fetchall()
        if abiertos:
            for ev_id, ev_name, ev_date, dias in abiertos:
                # refresh-upcoming (06:00 UTC) lo cierra, pero SOLO cuando
                # ufc.com ya lo ha sacado de su lista de próximos.
                nivel = WARN if int(dias) <= 1 else BAD
                print(f"{nivel} {ev_date} [{ev_id}] {ev_name} — {int(dias)}d en 'upcoming'")
                if int(dias) > 1:
                    problems.append(f"evento {ev_id} sigue 'upcoming' {int(dias)}d después")
            print("         Lo cierra refresh-upcoming (06:00 UTC), y solo cuando")
            print("         ufc.com ya lo ha quitado de su lista de próximos.")
        else:
            print(f"{OK} ninguno")

        # --- D. Método provisional ----------------------------------------
        print("\nD. MÉTODO SIN AFINAR ('Decision'/'Submission' a secas)")
        cur.execute(
            f"""
            SELECT e.id, e.event_date, CURRENT_DATE - e.event_date,
                   f.id, f.fighter_red_name, f.fighter_blue_name, f.method
            FROM fights f JOIN events e ON e.id = f.event_id
            WHERE {where}
              AND f.status IS DISTINCT FROM 'cancelled'
              AND f.method = ANY(%s)
              -- Mismo margen que el apartado B: recién terminada la velada TODAS
              -- llevan el genérico de ESPN, y eso es lo normal. Avisar de las 12
              -- cada domingo seria ruido que tapa la senal.
              AND CURRENT_DATE - e.event_date >= %s
            ORDER BY e.event_date DESC, f.bout_order
            """,
            [*params, list(NEVER_FROM_UFCSTATS), GRACE_DAYS],
        )
        provisionales = cur.fetchall()
        if provisionales:
            for ev_id, ev_date, dias, fid, red, blue, method in provisionales:
                print(f"{WARN} {ev_date} [{ev_id}] {fid}: {red} vs {blue} — '{method}' ({int(dias)}d)")
            warnings.append(f"{len(provisionales)} pelea(s) con método genérico de ESPN")
        else:
            print(f"{OK} ninguna sin afinar (pasado el margen de {GRACE_DAYS}d)")

    # --- Veredicto --------------------------------------------------------
    print("\n" + "=" * 74)
    if problems:
        print("  VEREDICTO: HAY QUE MIRARLO")
        for p in problems:
            print(f"    - {p}")
        raise SystemExit(1)
    if warnings:
        print("  VEREDICTO: OK con avisos")
        for w in warnings:
            print(f"    - {w}")
        raise SystemExit(0)
    print("  VEREDICTO: OK — el dato del evento está completo y es coherente")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
