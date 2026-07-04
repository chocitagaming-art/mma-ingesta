"""Backfill full-body photo, leg reach and gym for UFC-confirmed fighters.

Phase 2 (MMA STATUS redesign): fighters whose ``headshot_url`` already points at
ufc.com had their athlete page resolved before (~1,468 confirmed pages), so the
same page reliably yields the ``athlete_bio_full_body`` hero image plus the
"Leg reach" and "Trains at" bio fields. This pass re-fetches each of those pages
(reusing enrich_photos_ufc.resolve_athlete: browser UA + 0.4s delay) and fills
``full_body_url`` / ``leg_reach_cm`` / ``trains_at`` (migration 008).

WRITE POLICY — writes by default, like the other enrich_* passes
(enrich_photos_ufc, enrich_ranked): every UPDATE goes through
update_fighter_enrichment, which COALESCEs, so it only fills NULL/empty columns
and can never overwrite existing data with NULL or anything else. The
dry-run-by-default pattern (backfill_fight_videos, cleanup_*) is reserved for
passes that write judgement calls or delete rows; this one only copies facts
from the fighter's own confirmed page. --dry-run is still available for preview.

The selection is idempotent/resumable: only fighters still missing at least one
of the three columns are targeted, so partial passes with --limit resume the gap.

Usage:
    python -m src.scrapers.enrich_fullbody --dry-run --limit 5   # preview, no writes
    python -m src.scrapers.enrich_fullbody --limit 200           # partial pass
    python -m src.scrapers.enrich_fullbody                       # full pass (~10-15 min)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable

import requests

from .config import get_settings
from .db import connect
from .enrich_photos_ufc import REQUEST_DELAY_SECONDS, AthleteData, resolve_athlete
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_enrichment

LOGGER = logging.getLogger(__name__)

PROGRESS_EVERY = 25

# resolver(session, name) -> AthleteData | None. Injected in tests.
Resolver = Callable[[requests.Session, str], AthleteData | None]


def _get_target_fighters(connection, limit: int | None = None) -> list[tuple[int, str]]:
    """Fighters with a confirmed ufc.com page (headshot already from ufc.com)
    still missing at least one of the three Phase-2 columns."""
    sql = """
        SELECT id, name
        FROM fighters
        WHERE headshot_url ILIKE %s
          AND (full_body_url IS NULL OR leg_reach_cm IS NULL OR trains_at IS NULL)
        ORDER BY name
    """
    params: list = ["%ufc.com%"]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def backfill(
    connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    resolver: Resolver = resolve_athlete,
    sleeper: Callable[[float], None] = time.sleep,
) -> Counter:
    session = requests.Session()
    counts: Counter = Counter()
    targets = _get_target_fighters(connection, limit=limit)
    total = len(targets)
    counts["targets"] = total
    LOGGER.info("Fighters with a ufc.com page missing full-body/leg-reach/gym: %d", total)

    for idx, (fighter_id, name) in enumerate(targets, 1):
        data = resolver(session, name)
        sleeper(REQUEST_DELAY_SECONDS)
        if data is None:
            counts["unresolved"] += 1
        else:
            counts["resolved"] += 1
            if data.full_body_url:
                counts["with_full_body"] += 1
            if data.leg_reach_cm:
                counts["with_leg_reach"] += 1
            if data.trains_at:
                counts["with_trains_at"] += 1
            has_new_data = bool(data.full_body_url or data.leg_reach_cm or data.trains_at)
            if not dry_run and has_new_data:
                # Additive-only write: COALESCE fills NULL/empty columns and a
                # NULL argument never overwrites an existing value.
                updated = update_fighter_enrichment(
                    connection,
                    fighter_id,
                    full_body_url=data.full_body_url,
                    leg_reach_cm=data.leg_reach_cm,
                    trains_at=data.trains_at,
                )
                if updated:
                    connection.commit()
                    counts["updated"] += 1

        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — resolved=%d full_body=%d leg_reach=%d trains_at=%d updated=%d",
                idx, total, counts["resolved"], counts["with_full_body"],
                counts["with_leg_reach"], counts["with_trains_at"], counts["updated"],
            )
    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill fighters.full_body_url + leg_reach_cm + trains_at from ufc.com athlete pages."
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters (re-runnable; resumes the gap).")
    args = parser.parse_args()
    configure_logging()

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = backfill(connection, dry_run=args.dry_run, limit=args.limit)

    keys = ["targets", "resolved", "with_full_body", "with_leg_reach", "with_trains_at", "updated", "unresolved"]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
