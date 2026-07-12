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


def _collect_raw(connection) -> dict:
    return {
        "duplicate_rank_positions": _duplicate_rank_positions(connection),
        "upcoming_without_bouts": _upcoming_without_bouts(connection),
        "duplicate_fighter_names": _duplicate_fighter_names(connection),
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
    return bool(data["duplicate_rank_positions"]) or bool(data["upcoming_without_bouts"])


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
