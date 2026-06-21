"""Re-scrape fight_stats for existing ufcstats fights.

Fixes #44 (every existing row held only round-1 numbers) and populates the #45
target/position breakdown, using the round-summing parser in ``parsers.fights``.

The main scraper SKIPS events that already exist, so a normal re-run never
revisits historical bouts. This module instead walks every ``fights`` row
(source='ufcstats') and re-parses its detail page. Idempotent (ON CONFLICT),
resumable (--min-id), and safe to validate on a subset first (--fighter).

Examples (rate-limited ~1.25s/req via UfcStatsClient):
    python -m src.scrapers.backfill_fight_stats --fighter "Sean Strickland" --fighter "Islam Makhachev" --verify
    python -m src.scrapers.backfill_fight_stats --limit 50 --dry-run
    python -m src.scrapers.backfill_fight_stats                      # full backfill (~8.4k fights, ~3h)
    python -m src.scrapers.backfill_fight_stats --min-id 5000        # resume after interruption
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .logging_config import configure_logging
from .parsers.fights import build_fight_stats_record, parse_fight_stats
from .repositories.fighters import get_fighter_id_by_source
from .repositories.fights import upsert_fight_stats

LOGGER = logging.getLogger(__name__)
BASE_URL = "http://ufcstats.com"


def _fight_detail_url(source_id: str) -> str:
    if source_id.startswith("http"):
        return source_id
    if source_id.startswith("/"):
        return f"{BASE_URL}{source_id}"
    return f"{BASE_URL}/fight-details/{source_id}"


def list_fights(
    connection,
    fighter_names: list[str] | None,
    min_id: int,
    limit: int | None,
) -> list[tuple[int, str]]:
    """(fight_id, source_id) for ufcstats fights, optionally filtered by name."""
    clauses = [
        "f.source = 'ufcstats'",
        "f.source_id IS NOT NULL",
        "f.id > %s",
    ]
    params: list = [min_id]
    if fighter_names:
        name_predicates = []
        for name in fighter_names:
            name_predicates.append(
                "EXISTS (SELECT 1 FROM fighters x "
                "WHERE x.id IN (f.fighter_red_id, f.fighter_blue_id) AND x.name ILIKE %s)"
            )
            params.append(f"%{name}%")
        clauses.append("(" + " OR ".join(name_predicates) + ")")
    sql = (
        "SELECT f.id, f.source_id FROM fights f WHERE "
        + " AND ".join(clauses)
        + " ORDER BY f.id"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def backfill(
    connection,
    client: UfcStatsClient,
    fighter_names: list[str] | None = None,
    min_id: int = 0,
    limit: int | None = None,
    dry_run: bool = False,
) -> Counter:
    counts: Counter = Counter()
    fighter_id_cache: dict[str, int | None] = {}
    fights = list_fights(connection, fighter_names, min_id, limit)
    total = len(fights)
    LOGGER.info("Backfilling fight_stats for %s fights (dry_run=%s)", total, dry_run)
    for processed, (fight_id, source_id) in enumerate(fights, start=1):
        url = _fight_detail_url(source_id)
        try:
            page = client.fetch(url)
            parsed = parse_fight_stats(page.soup)
            if not parsed:
                counts["fights_without_stats"] += 1
            rows_this_fight = 0
            for fighter_stats in parsed:
                source = fighter_stats.fighter_source_id
                if source not in fighter_id_cache:
                    fighter_id_cache[source] = get_fighter_id_by_source(connection, "ufcstats", source)
                fighter_id = fighter_id_cache.get(source)
                if fighter_id is None:
                    counts["stats_unresolved_fighter"] += 1
                    LOGGER.warning("Unresolved fighter %s on fight %s", source, fight_id)
                    continue
                if not dry_run:
                    upsert_fight_stats(
                        connection, build_fight_stats_record(fight_id, fighter_id, fighter_stats)
                    )
                rows_this_fight += 1
            # Release the per-fight transaction every iteration: commit the writes,
            # or roll back the read-only snapshot in dry-run (the SELECTs above open
            # a transaction too -> avoids a long idle-in-transaction session).
            if dry_run:
                connection.rollback()
            else:
                connection.commit()
            counts["stats_rows"] += rows_this_fight  # only count rows that survived the commit
            counts["fights"] += 1
        except Exception as exc:  # noqa: BLE001 - isolate per-fight failures, keep going
            connection.rollback()  # unconditional: a failed read also aborts the transaction
            counts["errors"] += 1
            LOGGER.exception("Failed to backfill fight %s (%s): %s", fight_id, url, exc)
        if processed % 50 == 0 or processed == total:
            LOGGER.info("Progress %s/%s | %s", processed, total, dict(counts))
    return counts


def verify_fighter(connection, name: str) -> None:
    """Aggregate a fighter's fight_stats and print sig-strike totals + accuracy."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fr.id, fr.name,
                   COUNT(*) AS fights_with_stats,
                   COALESCE(SUM(fs.sig_strikes_landed), 0) AS landed,
                   COALESCE(SUM(fs.sig_strikes_attempted), 0) AS attempted,
                   COALESCE(SUM(fs.sig_str_head_landed), 0) AS head,
                   COALESCE(SUM(fs.sig_str_body_landed), 0) AS body,
                   COALESCE(SUM(fs.sig_str_leg_landed), 0) AS leg
            FROM fighters fr
            JOIN fight_stats fs ON fs.fighter_id = fr.id
            WHERE fr.name ILIKE %s
            GROUP BY fr.id, fr.name
            ORDER BY attempted DESC
            """,
            (f"%{name}%",),
        )
        rows = cursor.fetchall()
    if not rows:
        LOGGER.info("verify: no fight_stats for '%s'", name)
        return
    for fid, fname, n, landed, attempted, head, body, leg in rows:
        pct = (landed / attempted * 100) if attempted else 0
        LOGGER.info(
            "verify %s (id=%s): %s fights | sig %s/%s (%.1f%%) | head %s body %s leg %s",
            fname, fid, n, landed, attempted, pct, head, body, leg,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-scrape fight_stats (fix #44 + populate #45).")
    parser.add_argument("--fighter", action="append", help="Only fights involving a fighter whose name matches (repeatable).")
    parser.add_argument("--min-id", type=int, default=0, help="Resume: only fights with id greater than this.")
    parser.add_argument("--limit", type=int, default=None, help="Max number of fights to process.")
    parser.add_argument("--dry-run", action="store_true", help="Parse + count but do not write.")
    parser.add_argument("--verify", action="store_true", help="After backfill, print aggregated totals for each --fighter.")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            client,
            fighter_names=args.fighter,
            min_id=args.min_id,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        LOGGER.info("Backfill complete: %s", dict(counts))
        # In dry-run nothing was written, so aggregates would reflect the OLD
        # (round-1-only) values -> only verify after a real run.
        if args.verify and args.fighter and not args.dry_run:
            for name in args.fighter:
                verify_fighter(connection, name)


if __name__ == "__main__":
    main()
