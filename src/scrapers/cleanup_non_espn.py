from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from .config import get_settings
from .db import connect


KEEPER_WHERE = "(headshot_url IS NOT NULL OR nationality IS NOT NULL)"
DELETABLE_WHERE = f"NOT {KEEPER_WHERE}"

# Deletable fighters who share a fight with a *keeper* must be retained: purging
# them (and their fights) would drag away a kept fighter's fight history. This
# subquery is the set of "other corner" ids that appear opposite a keeper.
_SHARED_WITH_KEEPER = f"""
    SELECT fighter_red_id AS fid FROM fights
    WHERE fighter_blue_id IN (SELECT id FROM fighters WHERE {KEEPER_WHERE})
    UNION
    SELECT fighter_blue_id AS fid FROM fights
    WHERE fighter_red_id IN (SELECT id FROM fighters WHERE {KEEPER_WHERE})
"""

# A fight is purgeable only when BOTH corners are deletable (no keeper involved).
_PURGEABLE_FIGHTS = f"""
    SELECT id FROM fights
    WHERE fighter_red_id IN (SELECT id FROM fighters WHERE {DELETABLE_WHERE})
      AND fighter_blue_id IN (SELECT id FROM fighters WHERE {DELETABLE_WHERE})
"""

# Purgeable fighters: deletable AND not retained by a shared fight with a keeper.
_PURGEABLE_FIGHTERS = f"""
    SELECT id FROM fighters
    WHERE {DELETABLE_WHERE}
      AND id NOT IN ({_SHARED_WITH_KEEPER})
"""


@dataclass(frozen=True)
class CleanupSummary:
    dry_run: bool
    rankings_table_exists: bool
    fighters_keep_before: int
    fighters_delete_before: int
    fighters_protected_shared: int
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


def cleanup_non_espn_fighters(apply: bool = False) -> CleanupSummary:
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
                f"SELECT COUNT(*) FROM fighters WHERE {DELETABLE_WHERE}",
            )

            # Purgeable counts respect the shared-fight guard: only fights/fighters
            # that do NOT touch a keeper are eligible for deletion.
            fighters_to_delete = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM ({_PURGEABLE_FIGHTERS}) p",
            )
            fighters_protected_shared = fighters_delete_before - fighters_to_delete

            fight_stats_to_delete = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM fight_stats WHERE fight_id IN ({_PURGEABLE_FIGHTS})",
            )

            fights_to_delete = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM ({_PURGEABLE_FIGHTS}) p",
            )

            news_to_delete = _fetch_count(
                cursor,
                f"SELECT COUNT(*) FROM news WHERE fighter_id IN ({_PURGEABLE_FIGHTERS})",
            )

            rankings_to_delete = 0
            if rankings_exists:
                rankings_to_delete = _fetch_count(
                    cursor,
                    f"SELECT COUNT(*) FROM rankings WHERE fighter_id IN ({_PURGEABLE_FIGHTERS})",
                )

            fighters_total_before = _fetch_count(cursor, "SELECT COUNT(*) FROM fighters")
            fights_total_before = _fetch_count(cursor, "SELECT COUNT(*) FROM fights")
            news_total_before = _fetch_count(cursor, "SELECT COUNT(*) FROM news")

            if apply:
                # Materialize the purgeable fighter ids before deleting fights, so the
                # set is stable across the cascade.
                cursor.execute(_PURGEABLE_FIGHTERS)
                purgeable_fighter_ids = [int(row[0]) for row in cursor.fetchall()]

                cursor.execute(
                    f"DELETE FROM fight_stats WHERE fight_id IN ({_PURGEABLE_FIGHTS})"
                )
                fight_stats_deleted = cursor.rowcount

                if purgeable_fighter_ids:
                    cursor.execute(
                        "DELETE FROM fight_stats WHERE fighter_id = ANY(%s)",
                        (purgeable_fighter_ids,),
                    )
                    fight_stats_deleted += cursor.rowcount

                cursor.execute(f"DELETE FROM fights WHERE id IN ({_PURGEABLE_FIGHTS})")
                fights_deleted = cursor.rowcount

                news_deleted = 0
                rankings_deleted = 0
                fighters_deleted = 0
                if purgeable_fighter_ids:
                    cursor.execute(
                        "DELETE FROM news WHERE fighter_id = ANY(%s)",
                        (purgeable_fighter_ids,),
                    )
                    news_deleted = cursor.rowcount
                    if rankings_exists:
                        cursor.execute(
                            "DELETE FROM rankings WHERE fighter_id = ANY(%s)",
                            (purgeable_fighter_ids,),
                        )
                        rankings_deleted = cursor.rowcount
                    cursor.execute(
                        "DELETE FROM fighters WHERE id = ANY(%s)",
                        (purgeable_fighter_ids,),
                    )
                    fighters_deleted = cursor.rowcount
            else:
                # Dry-run: report what would be deleted, touch nothing.
                fight_stats_deleted = fight_stats_to_delete
                fights_deleted = fights_to_delete
                news_deleted = news_to_delete
                rankings_deleted = rankings_to_delete
                fighters_deleted = fighters_to_delete

            fighters_remaining = fighters_total_before - fighters_deleted
            fights_remaining = fights_total_before - fights_deleted
            news_remaining = news_total_before - news_deleted

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

        if apply:
            connection.commit()
        else:
            connection.rollback()

    return CleanupSummary(
        dry_run=not apply,
        rankings_table_exists=rankings_exists,
        fighters_keep_before=fighters_keep_before,
        fighters_delete_before=fighters_delete_before,
        fighters_protected_shared=fighters_protected_shared,
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
    parser.add_argument("--apply", action="store_true", help="Write deletions to the DB (default: dry-run preview).")
    args = parser.parse_args()
    summary = cleanup_non_espn_fighters(apply=args.apply)
    print(json.dumps(summary.__dict__, indent=2))


if __name__ == "__main__":
    main()