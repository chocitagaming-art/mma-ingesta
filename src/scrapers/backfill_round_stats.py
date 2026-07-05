"""Backfill fight_stats_rounds for historical fights (migration 012 / BE4).

fight_stats has always stored the fight TOTALS (per-round rows summed, see
005); the per-round breakdown was parsed and thrown away. This module walks
completed ufcstats fights that already have fight_stats but no
fight_stats_rounds rows, re-downloads each fight-details page (UfcStatsClient,
rate-limited ~1.25s/req with the anti-bot challenge solved) and persists one
row per (fighter, round) via parse_fight_stats_rounds.

Scope: fights with source='ufcstats' (their source_id IS the fight-details
URL). Bouts from the ufc.com upcoming pipeline get their round rows from the
daily backfill_results step instead, which re-qualifies any bout missing them
within its 60-day window — so only pre-migration ufc.com bouts older than that
window stay uncovered (residual, and shrinking to zero going forward).

Idempotent (upsert ON CONFLICT (fight_id, fighter_id, round) with COALESCE)
and resumable: already-backfilled fights stop qualifying, so re-dispatching
continues where the last batch ended. Volume is large (~8k fights, ~3h at the
rate limit) — run in batches with --limit (workflow backfill-round-stats.yml,
dispatch only).

REQUIRES migration db/migrations/012_fase6.sql applied first.

Usage:
    python -m src.scrapers.backfill_round_stats --dry-run --limit 5   # preview
    python -m src.scrapers.backfill_round_stats --limit 2000          # one batch
    python -m src.scrapers.backfill_round_stats --lookback-days 365   # recent year
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .logging_config import configure_logging
from .parsers.fights import build_fight_stats_round_record, parse_fight_stats_rounds
from .repositories.fighters import get_fighter_id_by_source
from .repositories.fights import upsert_fight_stats_rounds

LOGGER = logging.getLogger(__name__)

BASE_URL = "http://ufcstats.com"
# Whole history by default; batches are cut with --limit, newest events first.
DEFAULT_LOOKBACK_DAYS = 36500


def _fight_detail_url(source_id: str) -> str:
    if source_id.startswith("http"):
        return source_id
    if source_id.startswith("/"):
        return f"{BASE_URL}{source_id}"
    return f"{BASE_URL}/fight-details/{source_id}"


def list_target_fights(
    connection, lookback_days: int, limit: int | None
) -> list[tuple[int, str]]:
    """(fight_id, source_id) of completed ufcstats fights that have totals in
    fight_stats but no per-round rows yet, newest events first."""
    sql = """
        SELECT f.id, f.source_id
        FROM fights f
        JOIN events e ON e.id = f.event_id
        WHERE f.source = 'ufcstats'
          AND f.source_id IS NOT NULL
          AND (f.winner_id IS NOT NULL OR f.method IS NOT NULL)
          AND EXISTS (SELECT 1 FROM fight_stats fs WHERE fs.fight_id = f.id)
          AND NOT EXISTS (SELECT 1 FROM fight_stats_rounds fsr WHERE fsr.fight_id = f.id)
          AND e.event_date IS NOT NULL
          AND e.event_date >= CURRENT_DATE - %s
        ORDER BY e.event_date DESC, f.id
    """
    params: list = [lookback_days]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def _process_fight(
    connection,
    client: UfcStatsClient,
    counts: Counter,
    fighter_id_cache: dict[str, int | None],
    fight_id: int,
    source_id: str,
    *,
    dry_run: bool,
) -> None:
    page = client.fetch(_fight_detail_url(source_id))
    parsed = parse_fight_stats_rounds(page.soup)
    if not parsed:
        # Very old fights have no per-round table; they stop qualifying only
        # once rows exist, so count them for visibility instead of retry-loops.
        counts["fights_without_rounds"] += 1
        LOGGER.info("No per-round table for fight %s (%s)", fight_id, source_id)
        return
    rows_this_fight = 0
    for round_stats in parsed:
        if round_stats.stats.get("sig_strikes_attempted") is None:
            counts["rounds_empty"] += 1
            continue
        source = round_stats.fighter_source_id
        if source not in fighter_id_cache:
            fighter_id_cache[source] = (
                get_fighter_id_by_source(connection, "ufcstats", source) if source else None
            )
        fighter_id = fighter_id_cache.get(source)
        if fighter_id is None:
            counts["rounds_unresolved_fighter"] += 1
            LOGGER.warning("Unresolved fighter %s on fight %s", source, fight_id)
            continue
        if not dry_run:
            upsert_fight_stats_rounds(
                connection, build_fight_stats_round_record(fight_id, fighter_id, round_stats)
            )
        rows_this_fight += 1
    counts["round_rows"] += rows_this_fight
    counts["fights_processed"] += 1
    LOGGER.info("Fight %s: %d round rows (dry_run=%s)", fight_id, rows_this_fight, dry_run)


def backfill(
    connection,
    client: UfcStatsClient,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int | None = None,
    dry_run: bool = False,
) -> Counter:
    counts: Counter = Counter()
    fighter_id_cache: dict[str, int | None] = {}
    fights = list_target_fights(connection, lookback_days, limit)
    counts["fights_targeted"] = len(fights)
    LOGGER.info("Completed fights with stats but no round rows: %d (dry_run=%s)", len(fights), dry_run)
    total = len(fights)
    for processed, (fight_id, source_id) in enumerate(fights, start=1):
        try:
            _process_fight(
                connection, client, counts, fighter_id_cache, fight_id, source_id,
                dry_run=dry_run,
            )
            # Release the per-fight transaction every iteration (long runs must
            # not sit idle-in-transaction between rate-limited fetches).
            if dry_run:
                connection.rollback()
            else:
                connection.commit()
        except Exception:  # noqa: BLE001 - isolate per-fight failures, keep going
            connection.rollback()
            counts["fight_errors"] += 1
            LOGGER.exception("Failed to backfill rounds for fight %s (%s)", fight_id, source_id)
        if processed % 50 == 0 or processed == total:
            LOGGER.info("Progress %s/%s | %s", processed, total, dict(counts))
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill fight_stats_rounds from ufcstats fight-details pages (BE4)."
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help="Only fights of events within this window (default: whole history).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max fights to fetch this batch.")
    parser.add_argument("--dry-run", action="store_true", help="Parse + count but do not write.")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            client,
            lookback_days=args.lookback_days,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    keys = [
        "fights_targeted", "fights_processed", "fights_without_rounds",
        "round_rows", "rounds_empty", "rounds_unresolved_fighter", "fight_errors",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
