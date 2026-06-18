from __future__ import annotations

import json

from .config import get_settings
from .db import connect


def cleanup_orphan_fights() -> dict[str, int]:
    settings = get_settings()
    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM fights
                """
            )
            total_fights = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM fights f
                JOIN fighters red ON red.id = f.fighter_red_id
                JOIN fighters blue ON blue.id = f.fighter_blue_id
                """
            )
            fights_with_both_fighters = int(cursor.fetchone()[0])
            cursor.execute(
                """
                DELETE FROM fight_stats fs
                USING fights f
                LEFT JOIN fighters red ON red.id = f.fighter_red_id
                LEFT JOIN fighters blue ON blue.id = f.fighter_blue_id
                WHERE fs.fight_id = f.id
                  AND (red.id IS NULL OR blue.id IS NULL)
                """
            )
            deleted_fight_stats = cursor.rowcount
            cursor.execute(
                """
                DELETE FROM fights f
                USING fights doomed
                LEFT JOIN fighters red ON red.id = doomed.fighter_red_id
                LEFT JOIN fighters blue ON blue.id = doomed.fighter_blue_id
                WHERE f.id = doomed.id
                  AND (red.id IS NULL OR blue.id IS NULL)
                """
            )
            deleted_fights = cursor.rowcount
            connection.commit()
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM events
                """
            )
            total_events = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM fights
                """
            )
            remaining_fights = int(cursor.fetchone()[0])
    return {
        "total_events": total_events,
        "total_fights_before_cleanup": total_fights,
        "fights_with_both_fighters_before_cleanup": fights_with_both_fighters,
        "deleted_fights": deleted_fights,
        "deleted_fight_stats": deleted_fight_stats,
        "remaining_fights": remaining_fights,
    }


def main() -> None:
    print(json.dumps(cleanup_orphan_fights(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()