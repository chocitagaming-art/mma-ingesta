"""Read-only data-quality invariants for the weekly qa-data-coverage workflow.

Unlike photo_coverage's structural gaps (debut fighters legitimately lack a
photo), these are invariants that should be EMPTY in a healthy DB — a hit means
something is actually wrong:

  - CRITICAL: rank_position duplicated within a ranking snapshot. The slot UNIQUE
    of migration 007 (promotion_id, division, rank_position, snapshot_date)
    should already forbid it, so a hit means the constraint is gone or a snapshot
    was written around it.
  - CRITICAL: an 'upcoming' event with ZERO bouts — a scrape that created the
    event row but never its card (users would see an empty upcoming event).
  - INFO: fighter names shared by 2+ rows. Usually real homonyms, occasionally a
    dedup miss (merge_duplicate_fighters territory) — surfaced, not alarmed on.

Read-only: only SELECTs, never writes. In markdown mode exit code 1 when a
CRITICAL check has rows (so the workflow opens/comments a 'data-quality' Issue),
exit 0 otherwise — the monitor.yml anti-spam pattern.

Usage (read-only):
    PYTHONUTF8=1 python -m src.scrapers.data_quality_checks           # markdown
    PYTHONUTF8=1 python -m src.scrapers.data_quality_checks --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import get_settings
from .db import connect

# How soon an upcoming event must have its card. UFC announces events months
# ahead as placeholders ("TBD vs. TBD", no bouts) — normal, not a gap — so only
# flag an event whose date is within this window yet still has zero fights.
IMMINENT_UPCOMING_DAYS = 21


def _duplicate_rank_positions(connection) -> list[dict]:
    """(promotion, division, rank, snapshot) slots held by more than one row."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT promotion_id, division, rank_position,
                   snapshot_date::text, count(*) AS n
            FROM rankings
            GROUP BY promotion_id, division, rank_position, snapshot_date
            HAVING count(*) > 1
            ORDER BY snapshot_date DESC, division, rank_position
            """
        )
        return [
            {
                "promotion_id": r[0],
                "division": r[1],
                "rank_position": r[2],
                "snapshot_date": r[3],
                "count": int(r[4]),
            }
            for r in cursor.fetchall()
        ]


def _upcoming_without_bouts(connection) -> list[dict]:
    """IMMINENT upcoming events (within IMMINENT_UPCOMING_DAYS) that have no fights.

    Far-future placeholders ('TBD vs. TBD', card not announced) legitimately have
    no bouts, so the window excludes them; an imminent event with an empty card is
    a real scrape gap.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name, e.event_date::text
            FROM events e
            WHERE e.status = 'upcoming'
              AND e.event_date IS NOT NULL
              AND e.event_date >= CURRENT_DATE
              AND e.event_date <= CURRENT_DATE + (%s * INTERVAL '1 day')
              AND NOT EXISTS (SELECT 1 FROM fights f WHERE f.event_id = e.id)
            ORDER BY e.event_date, e.id
            """,
            (IMMINENT_UPCOMING_DAYS,),
        )
        return [
            {"id": int(r[0]), "name": str(r[1]), "event_date": r[2]}
            for r in cursor.fetchall()
        ]


def _futuros_sin_clasificar(connection) -> list[dict]:
    """Eventos FUTUROS cuyo formato no reconoce la regla de `events.tier`.

    LA DECISIÓN QUE ESTO VIGILA. La regla de la migración 028 es una lista negra:
    lo que no reconoce cae en 'unknown' y sale DESTACABLE. Es deliberado — un
    formato nuevo que se cuele en la portada es molesto, visible y de un renglón,
    mientras que el fallo contrario (un UFC Fight Night que desaparece del hero,
    de /en-vivo y del centinela, en silencio, un sábado por la noche) es mucho
    peor. Pero esa decisión solo es segura si alguien AVISA, y este es el aviso.

    ⚠️ Solo mira los FUTUROS. Los 8 'unknown' que hay en la base son veladas UFC
    completas y todas pasadas (Ultimate Japan, UFC Macao, UFC Freedom 250...):
    incluirlas dejaría la alarma sonando para siempre y nadie volvería a mirarla.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name, e.event_date::text, e.source_id
            FROM events e
            WHERE e.tier = 'unknown'
              AND e.event_date IS NOT NULL
              AND e.event_date >= CURRENT_DATE
            ORDER BY e.event_date, e.id
            """
        )
        return [
            {
                "id": int(r[0]),
                "name": str(r[1]),
                "event_date": r[2],
                "source_id": r[3],
            }
            for r in cursor.fetchall()
        ]


def _tier_forzado_a_mano(connection) -> list[dict]:
    """Eventos con `tier_override` puesto. En régimen normal no debe haber ninguno.

    `tier_override` es la válvula de escape de la migración 028: una columna
    generada no se puede UPDATEar, y la noche de una velada no se aplica una
    migración, así que existe un camino para forzar el tipo de UN evento en cinco
    segundos y sin desplegar. El precio es que ESE es el único camino que le
    queda a este diseño para volver a desincronizarse del dato — por eso lleva
    alarma desde el mismo commit que lo creó.

    Que salga una fila aquí no es un error: es un recordatorio de que hay una
    excepción viva y de que lo permanente se arregla cambiando `event_tier()` en
    la migración 029, no dejando el parche puesto para siempre.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name, e.event_date::text, e.tier_override
            FROM events e
            WHERE e.tier_override IS NOT NULL
            ORDER BY e.event_date DESC NULLS LAST, e.id DESC
            """
        )
        return [
            {
                "id": int(r[0]),
                "name": str(r[1]),
                "event_date": r[2],
                "tier_override": str(r[3]),
            }
            for r in cursor.fetchall()
        ]


def _duplicate_fighter_names(connection) -> list[dict]:
    """Fighter names shared by 2+ rows (usually homonyms; sometimes a dedup miss)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT lower(btrim(regexp_replace(name, '\\s+', ' ', 'g'))) AS norm,
                   count(*) AS n, array_agg(id ORDER BY id) AS ids
            FROM fighters
            WHERE name IS NOT NULL AND btrim(name) <> ''
            GROUP BY norm
            HAVING count(*) > 1
            ORDER BY count(*) DESC, norm
            """
        )
        return [
            {"name": r[0], "count": int(r[1]), "ids": list(r[2])}
            for r in cursor.fetchall()
        ]


def _mirrored_scorecards(connection) -> list[dict]:
    """Peleas donde la MAYORÍA de las tarjetas le da el combate al que perdió.

    EL FALLO QUE ESTO VIGILA. Durante meses `fight_scorecards` guardó las notas
    con las esquinas intercambiadas en **2412 peleas de 4020 (60 %)**, y la web
    las pintaba EN COLOR: afirmaba, en la ficha pública, que un juez le había
    dado la pelea al perdedor. La causa era un docstring que decía que ufcstats
    imprime las notas en el orden de los bloques de persona; las imprime como
    (perdedor, ganador), medido sobre 367 páginas de 1995 a 2026 sin una sola
    excepción.

    Es un invariante duro y barato: si un juez pone 30-27 y el ganador oficial
    es el otro, o la tarjeta está del revés o el ganador está mal. Una sola fila
    aquí ya es motivo de alarma — por eso es CRITICAL.

    ⚠️ Se excluyen a propósito las peleas SIN ganador (empates y resultados
    anulados): ufcstats marca 'D'/'D' o 'NC'/'NC' en los dos bloques, no hay a
    qué anclar el par y su orden en la fuente es arbitrario. Son 82 peleas, y la
    web las pinta sin color y con un aviso en vez de inventarse el lado.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH t AS (
              SELECT fight_id,
                     count(*) FILTER (WHERE red_score  > blue_score) AS n_red,
                     count(*) FILTER (WHERE blue_score > red_score)  AS n_blue
              FROM fight_scorecards
              GROUP BY fight_id)
            SELECT f.id,
                   -- fighter_red_name está a NULL en buena parte del histórico:
                   -- sin el COALESCE el informe sale con los nombres en blanco y
                   -- no se puede ir a mirar la ficha.
                   COALESCE(f.fighter_red_name, fr.name),
                   COALESCE(f.fighter_blue_name, fb.name),
                   f.method, t.n_red, t.n_blue
            FROM t
            JOIN fights f ON f.id = t.fight_id
            LEFT JOIN fighters fr ON fr.id = f.fighter_red_id
            LEFT JOIN fighters fb ON fb.id = f.fighter_blue_id
            WHERE f.winner_id IS NOT NULL
              AND t.n_red <> t.n_blue
              AND ( (t.n_red  > t.n_blue AND f.winner_id = f.fighter_blue_id)
                 OR (t.n_blue > t.n_red  AND f.winner_id = f.fighter_red_id) )
            ORDER BY f.id
            """
        )
        return [
            {
                "fight_id": int(r[0]), "red": r[1], "blue": r[2], "method": r[3],
                "cards_for_red": int(r[4]), "cards_for_blue": int(r[5]),
            }
            for r in cursor.fetchall()
        ]


def _collect_raw(connection) -> dict:
    return {
        "duplicate_rank_positions": _duplicate_rank_positions(connection),
        "upcoming_without_bouts": _upcoming_without_bouts(connection),
        "futuros_sin_clasificar": _futuros_sin_clasificar(connection),
        "tier_forzado_a_mano": _tier_forzado_a_mano(connection),
        "duplicate_fighter_names": _duplicate_fighter_names(connection),
        "mirrored_scorecards": _mirrored_scorecards(connection),
    }


def collect(connection=None) -> dict:
    """Run the checks. Pass ``connection`` to inject a fake DB in tests."""
    if connection is None:
        settings = get_settings()
        with connect(settings.database_url) as conn:
            return _collect_raw(conn)
    return _collect_raw(connection)


def has_critical(data: dict) -> bool:
    """Whether a CRITICAL invariant was violated (drives the workflow's exit code)."""
    return (
        bool(data["duplicate_rank_positions"])
        or bool(data["upcoming_without_bouts"])
        # Una sola pelea con la mayoría de tarjetas apuntando al perdedor ya es
        # una mentira publicada en color. No hay umbral que valga.
        or bool(data.get("mirrored_scorecards"))
        # Un formato de evento que la regla no reconoce puede acabar de hero en
        # la portada, que es exactamente lo que pasó el 26-ago-2026.
        or bool(data.get("futuros_sin_clasificar"))
    )


def _render_markdown(data: dict) -> str:
    dup_rank = data["duplicate_rank_positions"]
    no_bouts = data["upcoming_without_bouts"]
    dup_names = data["duplicate_fighter_names"]
    lines = [
        "# QA de calidad de datos",
        "",
        "> `python -m src.scrapers.data_quality_checks` (solo-lectura).",
        "",
        f"## 🏆 rank_position duplicado en un snapshot ({len(dup_rank)}) — CRÍTICO",
        "",
    ]
    if not dup_rank:
        lines.append("_Ninguno: cada (promoción, división, puesto, fecha) es único._")
    else:
        for r in dup_rank:
            lines.append(
                f"- promo={r['promotion_id']} div={r['division']} "
                f"puesto #{r['rank_position']} [{r['snapshot_date']}] → {r['count']} filas"
            )
    lines += [
        "",
        f"## 📅 Eventos 'upcoming' SIN peleas ({len(no_bouts)}) — CRÍTICO",
        "",
    ]
    if not no_bouts:
        lines.append("_Ninguno: toda cartelera próxima tiene al menos una pelea._")
    else:
        for e in no_bouts:
            lines.append(f"- **{e['name']}** (id={e['id']}) [{e['event_date']}]")
    sin_clasificar = data.get("futuros_sin_clasificar", [])
    lines += [
        "",
        f"## 🏷️ Eventos futuros con formato sin reconocer ({len(sin_clasificar)}) — CRÍTICO",
        "",
        "> `events.tier = 'unknown'`: la regla de la migración 028 no sabe qué es",
        "> este evento, así que puede acabar de destacado en la portada. Si es una",
        "> velada UFC de verdad, no hay nada que hacer. Si es un formato nuevo",
        "> (otro Road To UFC, un Contender Series), hay que añadir su rama a",
        "> `public.event_tier()` en una migración 029.",
        "",
    ]
    if not sin_clasificar:
        lines.append("_Ninguno: toda velada futura tiene su formato reconocido._")
    else:
        for e in sin_clasificar:
            lines.append(
                f"- **{e['name']}** (id={e['id']}) [{e['event_date']}] "
                f"slug=`{e['source_id'] or '—'}`"
            )
    forzados = data.get("tier_forzado_a_mano", [])
    lines += [
        "",
        f"## 🔧 Eventos con `tier_override` puesto a mano ({len(forzados)})",
        "",
        "> La válvula de escape de la 028, para una urgencia en directo. Es el",
        "> único camino que le queda a `tier` para desincronizarse del dato, así",
        "> que no debe quedarse puesta: lo permanente se arregla cambiando",
        "> `public.event_tier()` en una migración 029.",
        "",
    ]
    if not forzados:
        lines.append("_Ninguno: el tipo de todas las veladas sale de la regla._")
    else:
        for e in forzados:
            lines.append(
                f"- **{e['name']}** (id={e['id']}) [{e['event_date']}] "
                f"→ forzado a `{e['tier_override']}`"
            )
    espejadas = data.get("mirrored_scorecards", [])
    lines += [
        "",
        f"## ⚖️ Tarjetas de jueces con la orientación invertida ({len(espejadas)}) — CRÍTICO",
        "",
        "> La mayoría de las tarjetas le da el combate a quien lo PERDIÓ. O están",
        "> del revés o el ganador está mal. La ficha pública lo pinta en color, así",
        "> que una sola fila aquí ya es una mentira publicada.",
        "",
    ]
    if not espejadas:
        lines.append("_Ninguna: toda tarjeta apunta al ganador oficial._")
    else:
        for e in espejadas[:50]:
            lines.append(
                f"- fight **{e['fight_id']}** · {e['red']} vs {e['blue']} ({e['method']}) "
                f"→ {e['cards_for_red']}-{e['cards_for_blue']} a favor del perdedor"
            )
        if len(espejadas) > 50:
            lines.append(f"- … y {len(espejadas) - 50} más")
    lines += [
        "",
        f"## 👥 Nombres de luchador duplicados ({len(dup_names)}) — informativo",
        "",
        "> Suelen ser homónimos reales; revisar con `merge_duplicate_fighters` solo",
        "> si son la misma persona.",
        "",
    ]
    if not dup_names:
        lines.append("_Ninguno._")
    else:
        for d in dup_names[:50]:
            lines.append(f"- {d['name']} → {d['count']} filas (ids={d['ids']})")
        if len(dup_names) > 50:
            lines.append(f"- … y {len(dup_names) - 50} más")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only data-quality invariants (rank dup, empty upcoming, dup names)."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    args = parser.parse_args()
    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(_render_markdown(data), end="")
    # Exit 1 on a CRITICAL violation so the workflow raises a data-quality Issue.
    if has_critical(data):
        sys.exit(1)


if __name__ == "__main__":
    main()
