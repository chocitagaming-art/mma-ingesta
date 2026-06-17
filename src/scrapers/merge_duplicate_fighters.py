from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from .config import get_settings
from .db import connect


@dataclass(frozen=True)
class FighterRow:
    id: int
    name: str
    nickname: str | None
    headshot_url: str | None
    nationality: str | None
    birth_date: Any
    height_cm: float | None
    reach_cm: float | None
    stance: str | None
    weight_grams: int | None
    wins: int
    losses: int
    draws: int
    source: str | None
    source_id: str | None


def _normalize_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _score(row: FighterRow) -> tuple[int, int, int, int, int]:
    populated_fields = sum(
        value not in (None, "")
        for value in (
            row.nickname,
            row.headshot_url,
            row.nationality,
            row.birth_date,
            row.height_cm,
            row.reach_cm,
            row.stance,
            row.weight_grams,
        )
    )
    return (
        1 if row.headshot_url else 0,
        populated_fields,
        row.wins + row.losses + row.draws,
        1 if row.source == "espn" else 0,
        -row.id,
    )


def _choose_keeper(rows: list[FighterRow]) -> FighterRow:
    return max(rows, key=_score)


def _merge_group(connection, rows: list[FighterRow]) -> dict[str, Any]:
    keeper = _choose_keeper(rows)
    duplicates = [row for row in rows if row.id != keeper.id]

    if not duplicates:
        return {"kept_id": keeper.id, "deleted_ids": []}

    deleted_ids = [row.id for row in duplicates]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fighters
            SET
                nickname = COALESCE(NULLIF(fighters.nickname, ''), NULLIF(%s, '')),
                headshot_url = COALESCE(NULLIF(fighters.headshot_url, ''), NULLIF(%s, '')),
                nationality = COALESCE(NULLIF(fighters.nationality, ''), NULLIF(%s, '')),
                birth_date = COALESCE(fighters.birth_date, %s),
                height_cm = COALESCE(fighters.height_cm, %s),
                reach_cm = COALESCE(fighters.reach_cm, %s),
                stance = COALESCE(NULLIF(fighters.stance, ''), NULLIF(%s, '')),
                weight_grams = COALESCE(fighters.weight_grams, %s),
                wins = GREATEST(fighters.wins, %s),
                losses = GREATEST(fighters.losses, %s),
                draws = GREATEST(fighters.draws, %s),
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                keeper.nickname,
                keeper.headshot_url,
                keeper.nationality,
                keeper.birth_date,
                keeper.height_cm,
                keeper.reach_cm,
                keeper.stance,
                keeper.weight_grams,
                keeper.wins,
                keeper.losses,
                keeper.draws,
                keeper.id,
            ),
        )

        for duplicate_id in deleted_ids:
            cursor.execute(
                "UPDATE fights SET fighter_red_id = %s WHERE fighter_red_id = %s",
                (keeper.id, duplicate_id),
            )
            cursor.execute(
                "UPDATE fights SET fighter_blue_id = %s WHERE fighter_blue_id = %s",
                (keeper.id, duplicate_id),
            )
            cursor.execute(
                "UPDATE fights SET winner_id = %s WHERE winner_id = %s",
                (keeper.id, duplicate_id),
            )
            cursor.execute(
                """
                DELETE FROM fight_stats
                WHERE fighter_id = %s
                  AND EXISTS (
                    SELECT 1
                    FROM fight_stats existing
                    WHERE existing.fight_id = fight_stats.fight_id
                      AND existing.fighter_id = %s
                  )
                """,
                (duplicate_id, keeper.id),
            )
            cursor.execute(
                "UPDATE fight_stats SET fighter_id = %s WHERE fighter_id = %s",
                (keeper.id, duplicate_id),
            )
            cursor.execute(
                "UPDATE rankings SET fighter_id = %s WHERE fighter_id = %s",
                (keeper.id, duplicate_id),
            )
            cursor.execute(
                "UPDATE news SET fighter_id = %s WHERE fighter_id = %s",
                (keeper.id, duplicate_id),
            )
            cursor.execute("DELETE FROM fighters WHERE id = %s", (duplicate_id,))

    return {"kept_id": keeper.id, "deleted_ids": deleted_ids}


def merge_duplicates(dry_run: bool = False) -> dict[str, Any]:
    settings = get_settings()
    summary: Counter = Counter()
    merged_groups: list[dict[str, Any]] = []

    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    name,
                    nickname,
                    headshot_url,
                    nationality,
                    birth_date,
                    height_cm,
                    reach_cm,
                    stance,
                    weight_grams,
                    wins,
                    losses,
                    draws,
                    source,
                    source_id
                FROM fighters
                ORDER BY lower(name), id
                """
            )
            rows = [
                FighterRow(
                    id=int(row[0]),
                    name=row[1],
                    nickname=row[2],
                    headshot_url=row[3],
                    nationality=row[4],
                    birth_date=row[5],
                    height_cm=float(row[6]) if row[6] is not None else None,
                    reach_cm=float(row[7]) if row[7] is not None else None,
                    stance=row[8],
                    weight_grams=int(row[9]) if row[9] is not None else None,
                    wins=int(row[10] or 0),
                    losses=int(row[11] or 0),
                    draws=int(row[12] or 0),
                    source=row[13],
                    source_id=row[14],
                )
                for row in cursor.fetchall()
            ]

        grouped: dict[str, list[FighterRow]] = defaultdict(list)
        for row in rows:
            grouped[_normalize_name(row.name)].append(row)

        for normalized_name, group in grouped.items():
            if len(group) < 2:
                continue
            result = _merge_group(connection, group)
            summary["groups_merged"] += 1
            summary["fighters_deleted"] += len(result["deleted_ids"])
            merged_groups.append(
                {
                    "name": group[0].name,
                    "normalized_name": normalized_name,
                    "kept_id": result["kept_id"],
                    "deleted_ids": result["deleted_ids"],
                }
            )

        if dry_run:
            connection.rollback()
        else:
            connection.commit()

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM fighters")
            summary["fighters_total"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM fighters WHERE headshot_url IS NOT NULL")
            summary["fighters_with_headshots"] = int(cursor.fetchone()[0])

    return {
        "dry_run": dry_run,
        "groups_merged": summary["groups_merged"],
        "fighters_deleted": summary["fighters_deleted"],
        "fighters_total": summary["fighters_total"],
        "fighters_with_headshots": summary["fighters_with_headshots"],
        "merged_groups": merged_groups,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge duplicate fighters by normalized name.")
    parser.add_argument("--dry-run", action="store_true", help="Preview merges without committing.")
    args = parser.parse_args()
    print(json.dumps(merge_duplicates(dry_run=args.dry_run), indent=2))


if __name__ == "__main__":
    main()