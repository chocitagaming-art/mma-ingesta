"""Fill empty 0-0-0 fighter records from ESPN.

Fighters imported from ESPN were stored with wins=losses=draws=0 (the importer
never pulled the record), and UFC athlete pages can't resolve many of them
because the names differ (e.g. "Levan Chokheli" vs UFC's "Dejar Chokheli").
ESPN, however, keys on the exact names we store and exposes W-L-D at
`athletes/{id}/records`. This resolves each 0-0-0 fighter on ESPN and fills the
record — only when the stored record is still 0-0-0 (never overwrites).

Usage:
    python -m src.scrapers.enrich_records_espn --probe "Leon Shahbazyan"  # no DB
    python -m src.scrapers.enrich_records_espn --dry-run                  # report, no writes
    python -m src.scrapers.enrich_records_espn                            # fill records (writes)
    python -m src.scrapers.enrich_records_espn --limit 50                 # partial pass
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time

from .config import get_settings
from .db import connect
from .enrich_ranked import _build_session, _search_espn_athlete
from .espn import _get_json
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_record

LOGGER = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 0.35
_RECORD_RE = re.compile(r"(\d+)-(\d+)-(\d+)")
_ATHLETE_DETAIL_URLS = (
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/athletes/{id}",
    "https://sports.core.api.espn.com/v2/sports/mma/athletes/{id}",
)


def _fetch_espn_record(session, athlete_id: str) -> tuple[int, int, int] | None:
    """Return (wins, losses, draws) from ESPN's athlete records, or None."""
    for template in _ATHLETE_DETAIL_URLS:
        try:
            detail = _get_json(session, template.format(id=athlete_id))
        except Exception:  # noqa: BLE001 - try the next endpoint
            continue
        ref = (detail.get("records") or {}).get("$ref")
        if not ref:
            continue
        try:
            records = _get_json(session, ref)
        except Exception:  # noqa: BLE001 - records unavailable
            return None
        for item in records.get("items", []):
            if item.get("name") == "overall" or item.get("type") == "total":
                text = str(item.get("summary") or item.get("displayValue") or "")
                match = _RECORD_RE.search(text)
                if match:
                    return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def resolve_record(session, name: str) -> tuple[int, int, int] | None:
    found = _search_espn_athlete(session, name)
    if found is None:
        return None
    athlete_id, _espn_name = found
    return _fetch_espn_record(session, athlete_id)


def _get_zero_record_fighters(connection, limit: int | None) -> list[tuple[int, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name
            FROM fighters
            WHERE name IS NOT NULL AND name <> ''
              AND COALESCE(wins, 0) = 0 AND COALESCE(losses, 0) = 0 AND COALESCE(draws, 0) = 0
            ORDER BY name
            """
        )
        rows = [(int(r[0]), str(r[1])) for r in cursor.fetchall()]
    return rows[:limit] if limit is not None else rows


def enrich_records(dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    settings = get_settings()
    session = _build_session(settings)
    counts = {"candidates": 0, "resolved": 0, "filled": 0, "unresolved": 0}
    with connect(settings.database_url) as connection:
        gaps = _get_zero_record_fighters(connection, limit)
        counts["candidates"] = len(gaps)
        LOGGER.info("Fighters with 0-0-0 record: %d", len(gaps))

        for idx, (fighter_id, name) in enumerate(gaps, 1):
            try:
                record = resolve_record(session, name)
            except Exception as exc:  # noqa: BLE001 - keep going on a single failure
                LOGGER.warning("ESPN lookup failed for %r: %s", name, exc)
                record = None
            time.sleep(REQUEST_DELAY_SECONDS)

            if not record or record == (0, 0, 0):
                counts["unresolved"] += 1
                continue
            counts["resolved"] += 1
            wins, losses, draws = record
            if dry_run:
                LOGGER.info("[dry-run] %s -> %s-%s-%s", name, wins, losses, draws)
                counts["filled"] += 1
                continue
            if update_fighter_record(connection, fighter_id, wins=wins, losses=losses, draws=draws):
                connection.commit()
                counts["filled"] += 1

            if idx % 25 == 0:
                LOGGER.info("Progress %d/%d — filled=%d", idx, len(gaps), counts["filled"])
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Fill 0-0-0 fighter records from ESPN.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not write.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters.")
    parser.add_argument("--probe", nargs="+", metavar="NAME", help="Test record resolution (no DB).")
    args = parser.parse_args()

    if args.probe:
        session = _build_session(get_settings())
        for name in args.probe:
            print(json.dumps({"name": name, "record": resolve_record(session, name)}, ensure_ascii=False))
        return

    counts = enrich_records(dry_run=args.dry_run, limit=args.limit)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
