from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from .config import get_settings
from .db import connect


KEEPER_WHERE = "(headshot_url IS NOT NULL OR nationality IS NOT NULL)"


@dataclass(frozen=True)
class CleanupSummary:
    dry_run: bool
    rankings_table_exists: bool
    fighters_keep_before: int
    fighters_delete_before: int
    fight_stats_deleted: int
    fights_deleted: int
    news_deleted: int
    rankings_deleted: int
    fighters_deleted: int
    fighters_remaining: int
    fights_remaining: int
    news_remaining: int
    null_height_cm_remaining: int
    null_reach_cm_remaining: int
    null_weight_grams_remaining: int
    null_nationality_remaining: int
    null_headshot_url_remaining: int


def _fetch_count(cursor, query: str, params: tuple[Any, ...] = ()) -> int:
    cursor.execute(query, params)
    return int(cursor.fetchone()[0])


def _rankings_table_exists(cursor) -> bool:
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'rankings'
        )
        """
    )
    return bool(cursor.fetchone()[0])


def cleanup_non_espn_fighters(dry_run: bool = False) -> CleanupSummary:
    settings = get_settings()

    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            rankings_exists = _rankings_table_exists(cursor)

            fighters_keep_before = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE {KEEPER_WHERE}",
            )
            fighters_delete_before = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE NOT {KEEPER_WHERE}",
            )

            fight_stats_to_delete = _fetch_count(
                cursor,
                f"""
                SELECT COUNT(*)
                FROM fight_stats
                WHERE fight_id IN (
                    SELECT id
                    FROM fights
                    WHERE fighter_red_id IN (
                        SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                    )
                    OR fighter_blue_id IN (
                        SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                    )
                )
                """,
            )

            fights_to_delete = _fetch_count(
                cursor,
                f"""
                SELECT COUNT(*)
                FROM fights
                WHERE fighter_red_id IN (
                    SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                )
                OR fighter_blue_id IN (
                    SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                )
                """,
            )

            news_to_delete = _fetch_count(
                cursor,
                f"""
                SELECT COUNT(*)
                FROM news
                WHERE fighter_id IN (
                    SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                )
                """,
            )

            rankings_to_delete = 0
            if rankings_exists:
                rankings_to_delete = _fetch_count(
                    cursor,
                    f"""
                    SELECT COUNT(*)
                    FROM rankings
                    WHERE fighter_id IN (
                        SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                    )
                    """,
                )

            cursor.execute(
                f"""
                DELETE FROM fight_stats
                WHERE fight_id IN (
                    SELECT id
                    FROM fights
                    WHERE fighter_red_id IN (
                        SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                    )
                    OR fighter_blue_id IN (
                        SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                    )
                )
                """
            )
            fight_stats_deleted = cursor.rowcount

            cursor.execute(
                f"""
                DELETE FROM fights
                WHERE fighter_red_id IN (
                    SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                )
                OR fighter_blue_id IN (
                    SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                )
                """
            )
            fights_deleted = cursor.rowcount

            cursor.execute(
                f"""
                DELETE FROM news
                WHERE fighter_id IN (
                    SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                )
                """
            )
            news_deleted = cursor.rowcount

            rankings_deleted = 0
            if rankings_exists:
                cursor.execute(
                    f"""
                    DELETE FROM rankings
                    WHERE fighter_id IN (
                        SELECT id FROM fighters WHERE NOT {KEEPER_WHERE}
                    )
                    """
                )
                rankings_deleted = cursor.rowcount

            cursor.execute(
                f"""
                DELETE FROM fighters
                WHERE NOT {KEEPER_WHERE}
                """
            )
            fighters_deleted = cursor.rowcount

            fighters_remaining = _fetch_count(cursor, "SELECT COUNT(*) FROM fighters")
            fights_remaining = _fetch_count(cursor, "SELECT COUNT(*) FROM fights")
            news_remaining = _fetch_count(cursor, "SELECT COUNT(*) FROM news")

            null_height_cm_remaining = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE {KEEPER_WHERE} AND height_cm IS NULL",
            )
            null_reach_cm_remaining = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE {KEEPER_WHERE} AND reach_cm IS NULL",
            )
            null_weight_grams_remaining = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE {KEEPER_WHERE} AND weight_grams IS NULL",
            )
            null_nationality_remaining = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE {KEEPER_WHERE} AND nationality IS NULL",
            )
            null_headshot_url_remaining = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fighters WHERE {KEEPER_WHERE} AND headshot_url IS NULL",
            )

        if dry_run:
            connection.rollback()
        else:
            connection.commit()

    return CleanupSummary(
        dry_run=dry_run,
        rankings_table_exists=rankings_exists,
        fighters_keep_before=fighters_keep_before,
        fighters_delete_before=fighters_delete_before,
        fight_stats_deleted=fight_stats_deleted,
        fights_deleted=fights_deleted,
        news_deleted=news_deleted,
        rankings_deleted=rankings_deleted,
        fighters_deleted=fighters_deleted,
        fighters_remaining=fighters_remaining,
        fights_remaining=fights_remaining,
        news_remaining=news_remaining,
        null_height_cm_remaining=null_height_cm_remaining,
        null_reach_cm_remaining=null_reach_cm_remaining,
        null_weight_grams_remaining=null_weight_grams_remaining,
        null_nationality_remaining=null_nationality_remaining,
        null_headshot_url_remaining=null_headshot_url_remaining,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete fighters not enriched from ESPN and dependent rows.")
    parser.add_argument("--dry-run", action="store_true", help="Preview cleanup without committing.")
    args = parser.parse_args()
    summary = cleanup_non_espn_fighters(dry_run=args.dry_run)
    print(json.dumps(summary.__dict__, indent=2))


if __name__ == "__main__":
    main()