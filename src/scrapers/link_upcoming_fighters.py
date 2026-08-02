"""Create + link fighter records for upcoming-event bouts stored as name-only.

`upcoming_events` stored some bout slots with only fighter_red_name /
fighter_blue_name and no fighter_red_id / fighter_blue_id (fighters new to the
DB). Those render as initials + 0-0-0 because there is no fighter row behind
them. This resolves each unlinked name on ESPN, imports a fighter row (record +
photo + measures + nationality), and links the bout slot to it.

Usage:
    python -m src.scrapers.link_upcoming_fighters --dry-run   # resolve + report, no writes
    python -m src.scrapers.link_upcoming_fighters             # import + link (writes)
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from .config import get_settings
from .db import connect
from .enrich_ranked import (
    ESPN_SOURCE,
    REQUEST_DELAY_SECONDS,
    _build_session,
    _fetch_athlete_by_id,
    search_espn_athlete_relaxed,
)
from .enrich_records_espn import _fetch_espn_record
from .espn import EspnAthlete
from .logging_config import configure_logging
from .models import FighterRecord
from .repositories.fighters import upsert_fighter

LOGGER = logging.getLogger(__name__)


def _resolve(session, name: str) -> tuple[EspnAthlete, tuple[int, int, int]] | None:
    """Resolve an ESPN athlete + W-L-D for a name. No DB writes."""
    found = search_espn_athlete_relaxed(session, name)
    if found is None:
        return None
    athlete_id, _espn_name = found
    athlete = _fetch_athlete_by_id(session, athlete_id)
    if athlete is None:
        return None
    record = _fetch_espn_record(session, athlete_id) or (0, 0, 0)
    return athlete, record


# ESPN publica huecos como si fueran datos: un alcance de 0 cm y una postura
# "--". Hoy hay 27 fichas con reach_cm = 0 y 26 con stance = '--', y el alcance
# alimenta el modelo de predicción, donde un cero no es "no lo sé" sino un
# luchador sin brazos. Aquí solo se crean fichas NUEVAS, así que convertir el
# hueco en NULL no puede pisar un dato bueno: o llega vacío de ESPN o no llega.
_ESPN_EMPTY_STANCE = {"--", "-", "n/a", ""}


def _clean_measure(value: float | None) -> float | None:
    """Un 0 de ESPN es un hueco, no una medida."""
    return value if value else None


def _clean_stance(value: str | None) -> str | None:
    """Convierte los marcadores de hueco de ESPN ('--') en NULL."""
    if value is None:
        return None
    return None if value.strip().lower() in _ESPN_EMPTY_STANCE else value


def _get_unlinked_slots(connection) -> list[tuple[int, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fi.id, 'red' AS corner, fi.fighter_red_name AS name
            FROM events e JOIN fights fi ON fi.event_id = e.id
            WHERE e.status = 'upcoming'
              AND fi.fighter_red_id IS NULL AND fi.fighter_red_name IS NOT NULL
            UNION ALL
            SELECT fi.id, 'blue', fi.fighter_blue_name
            FROM events e JOIN fights fi ON fi.event_id = e.id
            WHERE e.status = 'upcoming'
              AND fi.fighter_blue_id IS NULL AND fi.fighter_blue_name IS NOT NULL
            ORDER BY name
            """
        )
        return [(int(r[0]), str(r[1]), str(r[2])) for r in cursor.fetchall()]


def link_upcoming(dry_run: bool = False) -> dict[str, int]:
    settings = get_settings()
    session = _build_session(settings)
    counts = {"slots": 0, "resolved": 0, "linked": 0, "unresolved": 0}
    with connect(settings.database_url) as connection:
        slots = _get_unlinked_slots(connection)
        counts["slots"] = len(slots)
        LOGGER.info("Unlinked upcoming bout slots: %d", len(slots))

        for bout_id, corner, name in slots:
            try:
                resolved = _resolve(session, name)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:  # noqa: BLE001 - keep going on a single failure
                LOGGER.warning("Resolve failed for %r: %s", name, exc)
                counts["unresolved"] += 1
                continue
            if resolved is None:
                counts["unresolved"] += 1
                LOGGER.info("No ESPN match for %r", name)
                continue

            athlete, record = resolved
            counts["resolved"] += 1
            LOGGER.info("%s -> %s  %s-%s-%s  photo=%s", name, athlete.full_name, *record, bool(athlete.headshot_url))
            if dry_run:
                continue

            fighter = FighterRecord(
                name=athlete.full_name,
                nickname=athlete.nickname,
                headshot_url=athlete.headshot_url,
                nationality=athlete.nationality,
                birth_date=athlete.birth_date,
                height_cm=_clean_measure(athlete.height_cm),
                reach_cm=_clean_measure(athlete.reach_cm),
                stance=_clean_stance(athlete.stance),
                weight_grams=athlete.weight_grams,
                wins=record[0],
                losses=record[1],
                draws=record[2],
                source=ESPN_SOURCE,
                source_id=athlete.athlete_id,
            )
            fighter_id = upsert_fighter(connection, fighter)
            column = "fighter_red_id" if corner == "red" else "fighter_blue_id"
            with connection.cursor() as cursor:
                cursor.execute(f"UPDATE fights SET {column} = %s WHERE id = %s", (fighter_id, bout_id))
            connection.commit()
            counts["linked"] += 1
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Import + link unlinked upcoming-event fighters from ESPN.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    args = parser.parse_args()
    counts = link_upcoming(dry_run=args.dry_run)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
