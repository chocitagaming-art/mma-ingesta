"""Report fighters that still have no headshot, so the gaps can be filled by hand.

Run this AFTER the automated photo enrichment (UFC/ESPN). Whatever remains are
fighters those sources don't cover — regional debutants — which need a manual
Tapology photo via add_manual_fighter. The report focuses on UPCOMING-event
fighters (the ones users actually see on cards) and cross-references the frontend's
local-headshots.ts, because a manual fighter has headshot_url NULL in the DB yet
still renders a photo on the site via that fallback — so it must NOT be re-listed
as missing.

Usage (read-only). On Windows force UTF-8 so accented names don't crash the console:
    PYTHONUTF8=1 python -m src.scrapers.photo_coverage           # markdown report
    PYTHONUTF8=1 python -m src.scrapers.photo_coverage --json    # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .config import get_settings
from .db import connect

# Frontend fallback map (sibling repo): names already mapped to a /public photo.
_LOCAL_HEADSHOTS_TS = (
    Path(__file__).resolve().parents[2].parent / "mma-app" / "src" / "lib" / "local-headshots.ts"
)


def _upcoming_without_photo(connection) -> list[tuple[int, str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, e.name AS event_name, e.event_date::text
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (f.headshot_url IS NULL OR f.headshot_url = '')
            ORDER BY e.event_date::text, f.name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


def _upcoming_zero_record(connection) -> list[tuple[int, str, str, str]]:
    """Upcoming-event fighters whose record is 0-0-0 (wins=losses=draws=0)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, e.name AS event_name, e.event_date::text
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND COALESCE(f.wins, 0) = 0
              AND COALESCE(f.losses, 0) = 0
              AND COALESCE(f.draws, 0) = 0
            ORDER BY e.event_date::text, f.name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


def _upcoming_missing_nationality(connection) -> list[tuple[int, str, str, str]]:
    """Upcoming-event fighters with nationality NULL or empty string."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name, e.name AS event_name, e.event_date::text
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (f.nationality IS NULL OR f.nationality = '')
            ORDER BY e.event_date::text, f.name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in cursor.fetchall()]


def _total_without_photo(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM fighters WHERE headshot_url IS NULL OR headshot_url = ''"
        )
        return int(cursor.fetchone()[0])


def _local_headshot_names() -> set[str]:
    """Lowercase names already mapped to a manual photo in local-headshots.ts."""
    try:
        text = _LOCAL_HEADSHOTS_TS.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {name.lower() for name in re.findall(r'"([^"]+)":\s*"/fighters/', text)}


def collect() -> dict:
    settings = get_settings()
    with connect(settings.database_url) as connection:
        upcoming = _upcoming_without_photo(connection)
        total = _total_without_photo(connection)
        zero_record = _upcoming_zero_record(connection)
        missing_nationality = _upcoming_missing_nationality(connection)
    local = _local_headshot_names()
    marked = [
        {"id": fid, "name": name, "event": event, "date": date, "has_local": name.lower() in local}
        for fid, name, event, date in upcoming
    ]
    zero_record_list = [
        {"id": fid, "name": name, "event": event, "date": date}
        for fid, name, event, date in zero_record
    ]
    missing_nationality_list = [
        {"id": fid, "name": name, "event": event, "date": date}
        for fid, name, event, date in missing_nationality
    ]
    return {
        "total_without_photo": total,
        "upcoming_without_photo": marked,
        "upcoming_zero_record": zero_record_list,
        "upcoming_missing_nationality": missing_nationality_list,
    }


def _render_markdown(data: dict) -> str:
    upcoming = data["upcoming_without_photo"]
    missing = [u for u in upcoming if not u["has_local"]]
    has_local = [u for u in upcoming if u["has_local"]]
    lines = [
        "# Luchadores SIN foto",
        "",
        "> Generado por `python -m src.scrapers.photo_coverage`.",
        "> **FALTAN** = sin foto en ningún sitio → foto manual de Tapology con `add_manual_fighter --photo`.",
        "> **Ya con foto manual** = `headshot_url` NULL en la BD pero YA se ven en la web (local-headshots.ts).",
        "",
        f"**Total en la BD sin `headshot_url`:** {data['total_without_photo']} "
        "(la mayoría son históricos oscuros, no accionables).",
        "",
        f"## ⚠️ FALTAN de verdad — carteleras próximas ({len(missing)})",
        "",
    ]
    if not missing:
        lines.append("_Ninguno: toda cartelera próxima tiene foto (oficial o manual)._")
    else:
        for u in missing:
            lines.append(f"- **{u['name']}** (id={u['id']}) — {u['event']} [{u['date']}]")
    lines += ["", f"## Ya con foto manual local ({len(has_local)}) — no tocar", ""]
    for u in has_local:
        lines.append(f"- {u['name']} (id={u['id']})")

    zero_record = data["upcoming_zero_record"]
    lines += [
        "",
        f"## 🥋 Record 0-0-0 — carteleras próximas ({len(zero_record)})",
        "",
        "> Record vacío (`wins=0 AND losses=0 AND draws=0`) → completar con `add_manual_fighter` / scrape.",
        "",
    ]
    if not zero_record:
        lines.append("_Ninguno: toda cartelera próxima tiene record._")
    else:
        for u in zero_record:
            lines.append(f"- **{u['name']}** (id={u['id']}) — {u['event']} [{u['date']}]")

    missing_nationality = data["upcoming_missing_nationality"]
    lines += [
        "",
        f"## 🌍 Sin nacionalidad — carteleras próximas ({len(missing_nationality)})",
        "",
        "> `nationality` NULL o vacía → completar con `add_manual_fighter` / scrape.",
        "",
    ]
    if not missing_nationality:
        lines.append("_Ninguno: toda cartelera próxima tiene nacionalidad._")
    else:
        for u in missing_nationality:
            lines.append(f"- **{u['name']}** (id={u['id']}) — {u['event']} [{u['date']}]")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Report fighters without a headshot.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    args = parser.parse_args()
    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_render_markdown(data), end="")


if __name__ == "__main__":
    main()
